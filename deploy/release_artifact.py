"""Verify portable application sources and install hash-locked native dependencies."""
from __future__ import annotations

import ast
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import re
import tomllib


@dataclass(frozen=True)
class ReleaseIdentity:
    package_version: str
    build_revision: str


def read_release_identity(root: Path, *, manifest: dict | None = None,
                          allow_legacy: bool = False) -> ReleaseIdentity | None:
    """Read checksummed identity without executing staged or previous source code.

    An installed release also contains its venv and shared links, so rollback
    verifies the identity files individually instead of the pristine inventory.
    Legacy fallback is allowed only when build metadata was never included.
    """
    metadata_path = root / "release-manifest.json"
    if manifest is None:
        if not metadata_path.exists() and allow_legacy and not (root / "tools/_build_info.py").exists():
            return None
        manifest = json.loads(metadata_path.read_text(encoding="utf-8"))
    if manifest.get("format") != 2 or not isinstance(manifest.get("files"), dict):
        raise ValueError("Invalid release identity manifest")
    files = manifest["files"]
    build_name = "tools/_build_info.py"
    if build_name not in files:
        if allow_legacy and not (root / build_name).exists():
            return None
        raise ValueError("Release is missing checksummed build identity")

    def verified_text(name: str) -> str:
        path = root / name
        if (name not in files or path.is_symlink() or not path.is_file()
                or not path.resolve().is_relative_to(root.resolve()) or digest(path) != files[name]):
            raise ValueError("Release identity checksum mismatch: " + name)
        return path.read_text(encoding="utf-8")

    project = tomllib.loads(verified_text("pyproject.toml")).get("project", {})
    embedded = {}
    for node in ast.parse(verified_text(build_name)).body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            raise ValueError("Invalid embedded release identity")
        name = node.targets[0].id
        if name not in {"PACKAGE_VERSION", "BUILD_REVISION"} or name in embedded:
            raise ValueError("Invalid embedded release identity")
        embedded[name] = ast.literal_eval(node.value)
    version, revision = embedded.get("PACKAGE_VERSION"), embedded.get("BUILD_REVISION")
    if (project.get("name") != "noviis-mcp" or not isinstance(version, str) or not version.strip()
            or version != project.get("version") or not isinstance(revision, str)
            or not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", revision)
            or revision != manifest.get("revision")):
        raise ValueError("Release identity does not match the verified artifact")
    return ReleaseIdentity(version, revision)


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
