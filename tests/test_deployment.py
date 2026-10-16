from __future__ import annotations

import io
import hashlib
import json
import sys
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

from deploy.deploy_release import activate, deploy, extract_release


def bundle(archive, files, *, tamper=False):
    files = {**files, "requirements.lock": b"# locked test fixture",
             "wheelhouse/noviis_mcp-0.1.0-py3-none-any.whl": b"synthetic wheel"}
    manifest = {"format": 2, "revision": "test", "minimum_python": [3, 11],
                "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}
    files["release-manifest.json"] = json.dumps(manifest).encode()
    if tamper:
        files["main.py"] = b"changed after CI"
    with tarfile.open(archive, "w:gz") as package:
        for name, contents in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(contents)
            package.addfile(info, io.BytesIO(contents))


class DeploymentTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.app = self.root / "app"
        self.app.mkdir()
        self.archive = self.root / "release.tgz"
        self.make_archive({"main.py": b"# new release", "pyproject.toml": b"", "tools/__init__.py": b""})
        # Test orchestration on Windows too; real symlink switching has a Linux test below.
        mocked = patch.object(Path, "symlink_to")
        mocked.start()
        self.addCleanup(mocked.stop)
        sleeper = patch("deploy.deploy_release.time.sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def make_archive(self, files):
        bundle(self.archive, files)

    def run_deploy(self, **kwargs):
        return deploy(self.app, self.archive, "test.service", "http://127.0.0.1/mcp", **kwargs)

    def test_success_prepares_isolated_release_before_switching(self):
        events = []
        def run(args, **kwargs):
            events.append(("run", args))
            if "-c" in args:
                self.assertEqual(kwargs["env"]["NOVIIS_ENV"], "production")
            if "unittest" in args:
                self.assertEqual(kwargs["env"]["NOVIIS_ENV"], "development")
                self.assertFalse((kwargs["cwd"] / ".env").exists())
                self.assertFalse((kwargs["cwd"] / "logs").exists())
        def switch(current, target):
            events.append(("switch", target))
            self.assertTrue((target / "main.py").is_file())
        release = self.run_deploy(run=run, switch=switch, smoke=lambda _: events.append(("smoke", None)))
        self.assertEqual([event[0] for event in events], ["run"] * 6 + ["switch", "run", "smoke", "run"])
        install = events[1][1]
        for flag in ("--require-hashes", "--only-binary=:all:"):
            self.assertIn(flag, install)
        self.assertNotIn("--no-index", install)
        self.assertTrue(release.is_relative_to(self.app / "releases"))
        self.assertIn('"current"', (self.app / "main.py").read_text(encoding="utf-8"))

    def test_install_failure_does_not_touch_running_entrypoint(self):
        entry = self.app / "main.py"
        entry.write_bytes(b"# old")
        switch = Mock()
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_deploy(run=Mock(side_effect=subprocess.CalledProcessError(1, "pip")), switch=switch)
        switch.assert_not_called()
        self.assertEqual(entry.read_bytes(), b"# old")

    def test_failed_smoke_restores_legacy_entrypoint_and_checks_rollback(self):
        entry = self.app / "main.py"
        entry.write_bytes(b"# old")
        run, switch = Mock(), Mock()
        smoke = Mock(side_effect=[RuntimeError("bad release")] * 10 + [None])
        with self.assertRaisesRegex(RuntimeError, "bad release"):
            self.run_deploy(run=run, switch=switch, smoke=smoke)
        self.assertEqual(entry.read_bytes(), b"# old")
        self.assertEqual(switch.call_args.args, (self.app / "current", None))
        self.assertEqual(sum("restart" in call.args[0] for call in run.call_args_list), 2)
        self.assertEqual(smoke.call_count, 11)

    def test_first_deployment_failure_stops_service(self):
        run = Mock()
        with self.assertRaises(RuntimeError):
            self.run_deploy(run=run, switch=Mock(), smoke=Mock(side_effect=RuntimeError("bad")))
        self.assertIn("stop", run.call_args.args[0])
        self.assertFalse((self.app / "main.py").exists())

    def test_unsafe_archives_and_bundled_config_never_activate(self):
        for name in ("../outside.py", ".env", "logs/sensitive.log"):
            with self.subTest(name=name):
                self.make_archive({"main.py": b"", "pyproject.toml": b"", "tools/__init__.py": b"", name: b"bad"})
                switch, run = Mock(), Mock()
                with self.assertRaises(ValueError):
                    self.run_deploy(run=run, switch=switch)
                switch.assert_not_called()
                run.assert_not_called()
        self.assertFalse((self.root / "outside.py").exists())

    def test_modified_artifact_and_unsupported_python_never_install_or_activate(self):
        files = {"main.py": b"", "pyproject.toml": b"", "tools/__init__.py": b""}
        run, switch = Mock(), Mock()
        bundle(self.archive, files, tamper=True)
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            self.run_deploy(run=run, switch=switch)
        bundle(self.archive, files)
        with patch("deploy.release_artifact.sys.version_info", (3, 10)):
            with self.assertRaisesRegex(ValueError, "Python 3.11"):
                self.run_deploy(run=run, switch=switch)
        run.assert_not_called()
        switch.assert_not_called()

    def test_native_dependency_test_failure_preserves_current_service_and_shared_config(self):
        entry = self.app / "main.py"
        entry.write_bytes(b"# running release")
        (self.app / ".env").write_bytes(b"# production settings")
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            if "unittest" in args:
                self.assertFalse((kwargs["cwd"] / ".env").exists())
                raise subprocess.CalledProcessError(1, args)
        switch = Mock()
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_deploy(run=run, switch=switch)
        switch.assert_not_called()
        self.assertFalse(any("systemctl" in args for args in calls))
        self.assertEqual(entry.read_bytes(), b"# running release")
        self.assertEqual((self.app / ".env").read_bytes(), b"# production settings")

    def test_portable_release_accepts_newer_python_before_target_install_checks_compatibility(self):
        with patch("deploy.release_artifact.sys.version_info", (3, 12)):
            self.run_deploy(run=Mock(), switch=Mock(), smoke=Mock())

    def test_archive_symlink_is_rejected(self):
        with tarfile.open(self.archive, "w:gz") as package:
            link = tarfile.TarInfo("outside")
            link.type = tarfile.SYMTYPE
            link.linkname = "../outside"
            package.addfile(link)
        with self.assertRaises(ValueError):
            extract_release(self.archive, self.root / "extracted")


@unittest.skipIf(os.name == "nt", "POSIX deployment symlink semantics")
class AtomicSwitchTest(unittest.TestCase):
    def test_failed_release_restores_real_symlink_and_preserves_shared_files(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory)
            old = app / "releases/old"
            old.mkdir(parents=True)
            (old / "main.py").write_bytes(b"# old release")
            (app / ".env").write_bytes(b"# shared config")
            activate(app / "current", old)
            archive = app / "release.tgz"
            bundle(archive, {"main.py": b"", "pyproject.toml": b"", "tools/__init__.py": b""})
            smoke = Mock(side_effect=[RuntimeError("new release failed")] * 10 + [None])
            with patch("deploy.deploy_release.time.sleep"), self.assertRaisesRegex(RuntimeError, "new release failed"):
                deploy(app, archive, "test.service", "http://127.0.0.1/mcp", run=Mock(), smoke=smoke)
            self.assertEqual((app / "current").resolve(), old)
            self.assertEqual((old / "main.py").read_bytes(), b"# old release")
            self.assertEqual((app / ".env").read_bytes(), b"# shared config")
            new = next(path for path in (app / "releases").iterdir() if path != old)
            self.assertEqual((new / ".env").resolve(), app / ".env")

    def test_switch_and_restore_existing_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second, current = root / "first", root / "second", root / "current"
            first.mkdir()
            second.mkdir()
            activate(current, first)
            self.assertEqual(current.resolve(), first)
            activate(current, second)
            self.assertEqual(current.resolve(), second)
            activate(current, first)
            self.assertEqual(current.resolve(), first)
            activate(current, None)
            self.assertFalse(current.exists())
