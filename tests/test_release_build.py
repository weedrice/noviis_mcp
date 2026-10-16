from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from scripts import build_release


class ReleaseBuildTest(unittest.TestCase):
    def test_installer_lock_is_packaged_with_verified_checksum(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            project.mkdir()
            (project / "pyproject.toml").write_text(
                '[tool.setuptools]\npy-modules = ["main"]\n', encoding="utf-8",
            )
            for name in ("main.py", "README.md", ".env.example", "requirements.lock"):
                (project / name).write_bytes(b"# fixture\n")
            installer = b"# hash-locked installer fixture\n"
            (project / "requirements-installer.lock").write_bytes(installer)
            for folder in ("tools", "tests", "deploy", "scripts"):
                (project / folder).mkdir()
            output = Path(directory) / "dist/release.tgz"

            def run(args, **kwargs):
                if "wheel" in args:
                    wheels = Path(args[args.index("--wheel-dir") + 1])
                    (wheels / "noviis_mcp-0.1.0-py3-none-any.whl").write_bytes(b"wheel fixture")

            with (patch.object(build_release, "ROOT", project),
                  patch.object(build_release.platform, "system", return_value="Linux"),
                  patch.object(build_release.subprocess, "run", side_effect=run),
                  patch.object(build_release, "install_release", return_value=Path("python")) as install,
                  patch.object(build_release, "check_runtime")):
                build_release.build(output, "test-revision")
            install.assert_called_once()
            with tarfile.open(output, "r:gz") as archive:
                self.assertEqual(archive.extractfile("./requirements-installer.lock").read(), installer)
                manifest = json.load(archive.extractfile("./release-manifest.json"))
            self.assertEqual(
                manifest["files"]["requirements-installer.lock"], hashlib.sha256(installer).hexdigest(),
            )
            self.assertEqual(manifest["revision"], "test-revision")
