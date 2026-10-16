"""Build, offline-install and test a release before producing its archive (Linux/Python 3.11)."""
from __future__ import annotations

import argparse
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from deploy.release_artifact import digest, install_release, verify_release, write_manifest


def build(output: Path, revision: str) -> None:
    if sys.version_info[:2] != (3, 11) or platform.system() != "Linux":
        raise ValueError("Build release artifacts on Linux with Python 3.11")
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise ValueError("Output already exists; choose a fresh archive path")
    with tempfile.TemporaryDirectory(prefix="noviis-release-") as temporary:
        root = Path(temporary) / "release"
        root.mkdir()
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        for module in project["tool"]["setuptools"]["py-modules"]:
            shutil.copy2(ROOT / (module + ".py"), root)
        for folder in ("tools", "tests", "deploy", "scripts"):
            shutil.copytree(ROOT / folder, root / folder,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for name in ("pyproject.toml", "README.md", ".env.example", "requirements.lock"):
            shutil.copy2(ROOT / name, root)
        wheels = root / "wheelhouse"
        wheels.mkdir()
        subprocess.run([sys.executable, "-m", "pip", "download", "--require-hashes", "--only-binary=:all:",
                        "-r", str(ROOT / "requirements.lock"), "--dest", str(wheels)], check=True)
        build_source = Path(temporary) / "build-source"
        shutil.copytree(root, build_source, ignore=shutil.ignore_patterns("wheelhouse"))
        subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
                        "--wheel-dir", str(wheels), str(build_source)], check=True)
        app_wheel, = wheels.glob("noviis_mcp-*.whl")
        install_lock = (ROOT / "requirements.lock").read_text(encoding="utf-8")
        install_lock += f'\nnoviis-mcp=={project["project"]["version"]} --hash=sha256:{digest(app_wheel)}\n'
        (root / "requirements.install.lock").write_text(install_lock, encoding="utf-8")
        write_manifest(root, revision)
        verify_release(root)
        # Archive before verification generates a virtualenv, logs or bytecode.
        candidate = Path(temporary) / "release.tgz"
        with tarfile.open(candidate, "w:gz") as archive:
            archive.add(root, arcname=".")
        python = install_release(root)
        subprocess.run([str(python), "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=root, check=True)
        shutil.copy2(candidate, output)
        checksum = digest(output)
        output.with_suffix(output.suffix + ".sha256").write_text(f"{checksum}  {output.name}\n", encoding="utf-8")
        print(f"Verified release: {output.name} (sha256 {checksum})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "dist/release.tgz")
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()
    build(args.output.resolve(), args.revision)
