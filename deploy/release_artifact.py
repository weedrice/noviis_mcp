"""Verify and install the exact offline bundle exercised by CI."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys


def digest(path: Path) -> str:
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def write_manifest(root: Path, revision: str) -> None:
    files = {path.relative_to(root).as_posix(): digest(path)
             for path in sorted(root.rglob("*")) if path.is_file() and path.name != "release-manifest.json"}
    manifest = {"format": 1, "revision": revision, "python": "3.11", "system": platform.system(),
                "machine": platform.machine(), "files": files}
    (root / "release-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def verify_release(root: Path) -> dict:
    manifest = json.loads((root / "release-manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format") != 1 or not isinstance(manifest.get("files"), dict):
        raise ValueError("Invalid release manifest")
    if (manifest.get("python") != f"{sys.version_info.major}.{sys.version_info.minor}"
            or manifest.get("system") != platform.system() or manifest.get("machine") != platform.machine()):
        raise ValueError("Release platform does not match this Python/OS/architecture")
    actual = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    if actual != set(manifest["files"]) | {"release-manifest.json"}:
        raise ValueError("Release file inventory differs from the tested artifact")
    for name, expected in manifest["files"].items():
        path = root / name
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()) or digest(path) != expected:
            raise ValueError(f"Release checksum mismatch: {name}")
    if "requirements.install.lock" not in manifest["files"] or not list((root / "wheelhouse").glob("noviis_mcp-*.whl")):
        raise ValueError("Release is missing locked dependencies or application wheel")
    return manifest


def install_release(root: Path, *, run=subprocess.run, python=sys.executable) -> Path:
    interpreter = root / (".venv/Scripts/python.exe" if sys.platform == "win32" else ".venv/bin/python")
    run([python, "-m", "venv", str(root / ".venv")], check=True)
    run([str(interpreter), "-m", "pip", "install", "--no-index", "--find-links", str(root / "wheelhouse"),
         "--require-hashes", "--only-binary=:all:", "-r", str(root / "requirements.install.lock")], check=True, cwd=root)
    run([str(interpreter), "-m", "pip", "check"], check=True, cwd=root)
    return interpreter
