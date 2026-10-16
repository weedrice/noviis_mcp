"""Verify portable application sources and install hash-locked native dependencies."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def verification_environment(work: Path) -> dict[str, str]:
    # Keep only interpreter/OS bootstrap settings. Never inherit application
    # settings, Python import overrides, proxies, or credentials for verification.
    bootstrap = {"PATH", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC", "PATHEXT",
                 "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "LD_LIBRARY_PATH"}
    env = {key: value for key, value in os.environ.items() if key.upper() in bootstrap}
    return {**env, "NOVIIS_ENV": "development", "LOG_DIR": str(work / "logs"),
            "LOCK_FILE_PATH": str(work / "mcp.lock"), "NOVIIS_API_BASE_URL": "http://127.0.0.1:9/api/v1",
            "NOVIIS_AGENT_INTERNAL_SECRET": "runtime-check-synthetic-secret"}


def check_runtime(root: Path, interpreter: Path, *, run=subprocess.run) -> None:
    with tempfile.TemporaryDirectory(prefix="noviis-runtime-check-") as directory:
        run([str(interpreter), "-m", "deploy.check_runtime"], check=True, cwd=root,
            env=verification_environment(Path(directory)), timeout=30)


def digest(path: Path) -> str:
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def write_manifest(root: Path, revision: str) -> None:
    files = {path.relative_to(root).as_posix(): digest(path)
             for path in sorted(root.rglob("*")) if path.is_file() and path.name != "release-manifest.json"}
    manifest = {"format": 2, "revision": revision, "minimum_python": [3, 11], "files": files}
    (root / "release-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def verify_release(root: Path) -> dict:
    manifest = json.loads((root / "release-manifest.json").read_text(encoding="utf-8"))
    if (manifest.get("format") != 2 or manifest.get("minimum_python") != [3, 11]
            or not isinstance(manifest.get("files"), dict)):
        raise ValueError("Invalid release manifest")
    if sys.version_info[:2] < tuple(manifest["minimum_python"]):
        raise ValueError("Release requires Python 3.11 or newer")
    actual = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    if actual != set(manifest["files"]) | {"release-manifest.json"}:
        raise ValueError("Release file inventory differs from the tested artifact")
    for name, expected in manifest["files"].items():
        path = root / name
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()) or digest(path) != expected:
            raise ValueError(f"Release checksum mismatch: {name}")
    wheels = list((root / "wheelhouse").glob("*.whl"))
    if (not {"requirements.lock", "requirements-installer.lock"}.issubset(manifest["files"]) or len(wheels) != 1
            or not wheels[0].name.startswith("noviis_mcp-") or not wheels[0].name.endswith("-py3-none-any.whl")):
        raise ValueError("Release must contain locked dependencies and one portable application wheel")
    return manifest


def install_release(root: Path, *, run=subprocess.run, python=sys.executable) -> Path:
    interpreter = root / (".venv/Scripts/python.exe" if sys.platform == "win32" else ".venv/bin/python")
    run([python, "-m", "venv", str(root / ".venv")], check=True)
    # ensurepip varies by host Python build. Older resolvers can select an
    # unpinned extras candidate even when its base package is hash-locked.
    run([str(interpreter), "-m", "pip", "install", "--require-hashes", "--only-binary=:all:",
         "--no-deps", "-r", str(root / "requirements-installer.lock")], check=True, cwd=root)
    run([str(interpreter), "-m", "pip", "--version"], check=True, cwd=root)
    # Resolve wheel compatibility on the target, while versions and hashes stay locked.
    run([str(interpreter), "-m", "pip", "install", "--require-hashes", "--only-binary=:all:",
         "-r", str(root / "requirements.lock")], check=True, cwd=root)
    wheel, = (root / "wheelhouse").glob("noviis_mcp-*.whl")
    run([str(interpreter), "-m", "pip", "install", "--no-index", "--no-deps", str(wheel)], check=True, cwd=root)
    run([str(interpreter), "-m", "pip", "check"], check=True, cwd=root)
    return interpreter
