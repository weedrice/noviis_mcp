from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest


@unittest.skipUnless(os.name == "posix" and shutil.which("bash"), "Linux deployment bootstrap")
class BootstrapTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.archive = self.root / "release.tgz"
        with tarfile.open(self.archive, "w:gz") as archive:
            for name, body in {"deploy_release.py": b"print('synthetic helper ran')", "release_artifact.py": b""}.items():
                member = tarfile.TarInfo("./deploy/" + name)
                member.size = len(body)
                archive.addfile(member, io.BytesIO(body))
        self.env = {**os.environ, "APP_DIR": str(self.root / "app"), "ARCHIVE_PATH": str(self.archive),
                    "ARCHIVE_SHA256": hashlib.sha256(self.archive.read_bytes()).hexdigest(),
                    "PATH": str(self.root) + os.pathsep + os.environ["PATH"]}
        self.env.pop("PYTHON_BIN", None)
        self.script = Path(__file__).resolve().parents[1] / "deploy/deploy.sh"
        self.executable("python3", "exit 21")
        self.executable("python3.11", f'exec {shlex.quote(sys.executable)} "$@"')

    def executable(self, name, body):
        path = self.root / name
        path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def invoke(self):
        return subprocess.run(["bash", str(self.script)], env=self.env, capture_output=True,
                              text=True, timeout=10)

    def test_default_uses_python311_even_if_python3_is_incompatible(self):
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("synthetic helper ran", result.stdout)

    def test_explicit_python_path_is_used_for_version_check_and_helper(self):
        calls = self.root / "interpreter-calls"
        custom = self.executable("custom-python", f'echo called >> {shlex.quote(str(calls))}\nexec {shlex.quote(sys.executable)} "$@"')
        self.env["PYTHON_BIN"] = str(custom)
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls.read_text().splitlines(), ["called", "called"])
        self.assertIn("synthetic helper ran", result.stdout)

    def test_failed_interpreter_check_does_not_execute_helper(self):
        self.env["PYTHON_BIN"] = "python3"
        result = self.invoke()
        self.assertEqual(result.returncode, 21)
        self.assertNotIn("synthetic helper ran", result.stdout)
        self.assertFalse((self.root / "app").exists())
