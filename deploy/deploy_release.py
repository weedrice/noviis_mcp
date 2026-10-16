"""Isolated releases with rollback after failed restart or MCP smoke check."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
import uuid


if __package__:
    from .release_artifact import install_release, verify_release
else:
    from release_artifact import install_release, verify_release


def extract_release(archive: Path, destination: Path) -> None:
    with tarfile.open(archive, "r:gz") as package:
        for member in package.getmembers():
            target = (destination / member.name).resolve()
            if not target.is_relative_to(destination.resolve()) or member.issym() or member.islnk():
                raise ValueError("Archive contains an unsafe path or link")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                with package.extractfile(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
            else:
                raise ValueError("Archive contains an unsupported entry")


def activate(current: Path, target: Path | None) -> None:
    if target is None:
        current.unlink(missing_ok=True)
        return
    temporary = current.with_name(".current-" + uuid.uuid4().hex)
    try:
        temporary.symlink_to(target, target_is_directory=True)
        os.replace(temporary, current)
    finally:
        temporary.unlink(missing_ok=True)


def smoke_check(url: str, timeout: float = 5) -> None:
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "deployment-check", "version": "1"}}}
    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        result = json.load(response)
        session = response.headers.get("Mcp-Session-Id")
    protocol = result.get("result", {}).get("protocolVersion")
    if not isinstance(protocol, str):
        raise RuntimeError("MCP initialization smoke check failed")
    headers["MCP-Protocol-Version"] = protocol
    if session:
        headers["Mcp-Session-Id"] = session
    try:
        notification = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        with urllib.request.urlopen(urllib.request.Request(url, data=json.dumps(notification).encode(), headers=headers), timeout=timeout):
            pass
        listing = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        with urllib.request.urlopen(urllib.request.Request(url, data=json.dumps(listing).encode(), headers=headers), timeout=timeout) as response:
            result = json.load(response)
        names = {tool["name"] for tool in result.get("result", {}).get("tools", [])}
        if not {"get_agent_home", "create_post", "send_note"}.issubset(names):
            raise RuntimeError("MCP tool registration smoke check failed")
    finally:
        if session:
            with urllib.request.urlopen(urllib.request.Request(url, method="DELETE", headers=headers), timeout=timeout):
                pass


def wait_for_health(smoke, url: str) -> None:
    for attempt in range(10):
        try:
            smoke(url)
            return
        except Exception:
            if attempt == 9:
                raise
            time.sleep(1)


def deploy(app: Path, archive: Path, service: str, health_url: str, *, run=subprocess.run,
           smoke=smoke_check, switch=activate, python=sys.executable) -> Path:
    app = app.resolve()
    if app == Path(app.anchor) or not archive.is_file() or not re.fullmatch(r"[A-Za-z0-9_.@-]+\.service", service):
        raise ValueError("Invalid deployment target, archive, or service")
    app.mkdir(parents=True, exist_ok=True)
    current = app / "current"
    if current.exists() and not current.is_symlink():
        raise ValueError("current must be a release symlink")
    previous = current.resolve() if current.is_symlink() else None
    if previous is not None and not previous.is_relative_to(app / "releases"):
        raise ValueError("Previous release is outside this application's releases directory")
    release = app / "releases" / (time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + "-" + uuid.uuid4().hex[:12])
    release.mkdir(parents=True)
    extract_release(archive, release)
    verify_release(release)
    for required in ("main.py", "pyproject.toml", "tools/__init__.py"):
        if not (release / required).is_file():
            raise ValueError("Release is missing required application files")
    for name in (".env", ".env.local", "logs"):
        shared = app / name
        if name == "logs":
            shared.mkdir(exist_ok=True)
        destination = release / name
        if destination.exists():
            raise ValueError("Release archive must exclude config and logs")
        if shared.exists():
            destination.symlink_to(shared, target_is_directory=shared.is_dir())
    interpreter = install_release(release, run=run, python=python)
    run([str(interpreter), "-c", "import main; main.create_app()"], check=True, cwd=release,
        env={**os.environ, "NOVIIS_ENV": "production"})
    command = (["sudo"] if shutil.which("sudo") else []) + ["systemctl"]

    # Keep existing root-based units working; preserve the entry point for rollback.
    entry = app / "main.py"
    old_entry = entry.read_bytes() if entry.is_file() else None
    dispatcher = b'import os\nfrom pathlib import Path\np = Path(__file__).resolve().parent / "current"\nos.chdir(p)\nos.execv(str(p / ".venv/bin/python"), [str(p / ".venv/bin/python"), str(p / "main.py")])\n'
    activated = False
    try:
        switch(current, release)
        activated = True
        temporary_entry = app / ".main-release.py"
        temporary_entry.write_bytes(dispatcher)
        os.replace(temporary_entry, entry)
        run(command + ["restart", service], check=True)
        wait_for_health(smoke, health_url)
        run(command + ["is-active", "--quiet", service], check=True)
    except BaseException:
        if activated:
            switch(current, previous)
            if old_entry is None:
                entry.unlink(missing_ok=True)
            else:
                temporary_entry = app / ".main-release.py"
                temporary_entry.write_bytes(old_entry)
                os.replace(temporary_entry, entry)
            if previous is not None or old_entry is not None:
                run(command + ["restart", service], check=True)
                wait_for_health(smoke, health_url)
            else:
                run(command + ["stop", service], check=True)
        raise
    return release


def main() -> None:
    import fcntl  # Production deployment runs on Linux.
    app = Path(os.environ["APP_DIR"]).resolve()
    if app == Path(app.anchor):
        raise ValueError("APP_DIR must not be a filesystem root")
    app.mkdir(parents=True, exist_ok=True)
    with (app / ".deploy.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        release = deploy(app, Path(os.environ.get("ARCHIVE_PATH", "/tmp/noviis-mcp-release.tgz")),
                         os.environ.get("SERVICE_NAME", "noviis-mcp.service"),
                         os.environ.get("HEALTH_URL", "http://127.0.0.1:8001/mcp"))
        print("Activated release:", release.name)


if __name__ == "__main__":
    main()
