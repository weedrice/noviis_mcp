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

from deploy.deploy_release import activate, deploy, extract_release, validate_production
from deploy.release_artifact import verification_environment


def bundle(archive, files, *, tamper=False, include_installer=True):
    files = {**files, "requirements.lock": b"# locked test fixture",
             "wheelhouse/noviis_mcp-0.1.0-py3-none-any.whl": b"synthetic wheel"}
    if include_installer:
        files["requirements-installer.lock"] = b"# locked installer fixture"
    manifest = {"format": 2, "revision": "test", "minimum_python": [3, 11],
                "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}
    files["release-manifest.json"] = json.dumps(manifest).encode()
    if tamper:
        files[tamper if isinstance(tamper, str) else "main.py"] = b"changed after CI"
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
            if "deploy.check_runtime" in args:
                self.assertEqual(kwargs["env"]["NOVIIS_ENV"], "development")
                self.assertFalse((kwargs["cwd"] / ".env").exists())
                self.assertFalse((kwargs["cwd"] / "logs").exists())
                self.assertEqual(kwargs["timeout"], 30)
        def switch(current, target):
            events.append(("switch", target))
            self.assertTrue((target / "main.py").is_file())
        release = self.run_deploy(run=run, switch=switch, smoke=lambda _: events.append(("smoke", None)))
        self.assertEqual([event[0] for event in events], ["run"] * 8 + ["switch", "run", "smoke", "run"])
        bootstrap = events[1][1]
        self.assertEqual(bootstrap[1:], [
            "-m", "pip", "install", "--require-hashes", "--only-binary=:all:", "--no-deps",
            "-r", str(release / "requirements-installer.lock"),
        ])
        self.assertEqual(events[2][1], [bootstrap[0], "-m", "pip", "--version"])
        install = events[3][1]
        for flag in ("--require-hashes", "--only-binary=:all:"):
            self.assertIn(flag, install)
        self.assertNotIn("--no-index", install)
        self.assertEqual(install[0], bootstrap[0])
        self.assertEqual(install[-2:], ["-r", str(release / "requirements.lock")])
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

    def test_installer_bootstrap_failure_prevents_dependency_install_and_service_changes(self):
        entry = self.app / "main.py"
        entry.write_bytes(b"# running release")
        calls = []

        def run(args, **kwargs):
            calls.append(args)
            if args[-1].endswith("requirements-installer.lock"):
                raise subprocess.CalledProcessError(1, args)

        switch, smoke = Mock(), Mock()
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_deploy(run=run, switch=switch, smoke=smoke)
        self.assertEqual(len(calls), 2)
        self.assertIn("venv", calls[0])
        self.assertTrue(calls[1][-1].endswith("requirements-installer.lock"))
        switch.assert_not_called()
        smoke.assert_not_called()
        self.assertEqual(entry.read_bytes(), b"# running release")

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

    def test_missing_or_modified_installer_lock_never_installs_or_activates(self):
        files = {"main.py": b"", "pyproject.toml": b"", "tools/__init__.py": b""}
        for kwargs in ({"include_installer": False}, {"tamper": "requirements-installer.lock"}):
            with self.subTest(**kwargs):
                bundle(self.archive, files, **kwargs)
                run, switch = Mock(), Mock()
                with self.assertRaises(ValueError):
                    self.run_deploy(run=run, switch=switch)
                run.assert_not_called()
                switch.assert_not_called()

    def test_runtime_failure_preserves_current_service_and_shared_config(self):
        entry = self.app / "main.py"
        entry.write_bytes(b"# running release")
        (self.app / ".env").write_bytes(b"# production settings")
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            if "deploy.check_runtime" in args:
                self.assertFalse((kwargs["cwd"] / ".env").exists())
                raise subprocess.CalledProcessError(1, args)
        switch = Mock()
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_deploy(run=run, switch=switch)
        switch.assert_not_called()
        self.assertFalse(any("systemctl" in args for args in calls))
        self.assertEqual(entry.read_bytes(), b"# running release")
        self.assertEqual((self.app / ".env").read_bytes(), b"# production settings")

    def test_runtime_timeout_does_not_switch_or_restart_service(self):
        switch = Mock()
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            if "deploy.check_runtime" in args:
                raise subprocess.TimeoutExpired(args, kwargs["timeout"])
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_deploy(run=run, switch=switch)
        switch.assert_not_called()
        self.assertFalse(any("systemctl" in args for args in calls))

    def test_production_validation_failure_does_not_switch_or_restart_service(self):
        entry = self.app / "main.py"
        entry.write_bytes(b"# running release")
        calls = []

        def run(args, **kwargs):
            calls.append(args)
            if "-c" in args:
                raise subprocess.CalledProcessError(1, args)

        switch = Mock()
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_deploy(run=run, switch=switch)
        switch.assert_not_called()
        self.assertFalse(any("systemctl" in args for args in calls))
        self.assertEqual(entry.read_bytes(), b"# running release")
        self.assertFalse((self.app / "logs").exists())

    def test_runtime_environment_drops_production_limits_and_credentials(self):
        with patch.dict(os.environ, {
            "MCP_REQUEST_LIMIT": "10", "CHALLENGE_MAX_PENDING": "0", "NOVIIS_ENV": "production",
            "NOVIIS_AGENT_INTERNAL_SECRET": "synthetic-production-secret", "PYTHONPATH": "/unexpected",
            "HTTPS_PROXY": "http://synthetic-proxy", "AWS_SECRET_ACCESS_KEY": "synthetic-credential",
            "PATH": "/python/bin", "SYSTEMROOT": "C:/Windows",
        }, clear=True):
            env = verification_environment(self.root)
        self.assertEqual(env["PATH"], "/python/bin")
        self.assertEqual(env["SYSTEMROOT"], "C:/Windows")
        self.assertEqual(env["NOVIIS_ENV"], "development")
        self.assertEqual(env["NOVIIS_AGENT_INTERNAL_SECRET"], "runtime-check-synthetic-secret")
        for key in ("MCP_REQUEST_LIMIT", "CHALLENGE_MAX_PENDING", "PYTHONPATH", "HTTPS_PROXY", "AWS_SECRET_ACCESS_KEY"):
            self.assertNotIn(key, env)

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


class ProductionValidationTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.release = self.root / "release"
        self.release.mkdir()
        self.logs = self.root / "custom-production-logs"
        # A module-level value models config.py overriding the inherited LOG_DIR
        # while loading .env.local. Validation must redirect after that import.
        (self.release / "config.py").write_text(
            "from pathlib import Path\n"
            f"LOG_DIR = Path({str(self.logs)!r})\n",
            encoding="utf-8",
        )
        (self.release / "main.py").write_text(
            "from pathlib import Path\n"
            "import os\n"
            "import config\n"
            "assert os.environ['NOVIIS_ENV'] == 'production'\n"
            "Path('imported-log-dir.txt').write_text(str(config.LOG_DIR), encoding='utf-8')\n"
            "config.LOG_DIR.mkdir(parents=True, exist_ok=True)\n"
            "for name in ('app.log', 'access.log'):\n"
            "    with (config.LOG_DIR / name).open('a', encoding='utf-8') as output:\n"
            "        output.write('validation log\\n')\n"
            "def create_app():\n"
            "    Path('app-created.txt').write_text('created', encoding='utf-8')\n",
            encoding="utf-8",
        )

    def test_custom_production_logs_are_untouched_and_temporary_logs_are_removed(self):
        self.logs.mkdir()
        for name in ("app.log", "access.log", "app.log.1"):
            (self.logs / name).write_bytes(b"existing production log\n")
        before = {
            path.name: (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode)
            for path in self.logs.iterdir()
        }
        directory_mtime = self.logs.stat().st_mtime_ns

        validate_production(self.release, Path(sys.executable))

        after = {
            path.name: (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode)
            for path in self.logs.iterdir()
        }
        self.assertEqual(before, after)
        self.assertEqual(self.logs.stat().st_mtime_ns, directory_mtime)
        temporary_logs = Path((self.release / "imported-log-dir.txt").read_text(encoding="utf-8"))
        self.assertNotEqual(temporary_logs, self.logs)
        self.assertTrue(temporary_logs.parent.name.startswith("noviis-production-check-"))
        self.assertFalse(temporary_logs.exists())
        self.assertFalse(temporary_logs.parent.exists())
        self.assertTrue((self.release / "app-created.txt").is_file())
        self.assertFalse((self.release / "logs").exists())

    def test_unprovisioned_production_logs_fail_before_importing_application(self):
        def run(args, **kwargs):
            return subprocess.run(args, **kwargs, capture_output=True, text=True)

        with self.assertRaises(subprocess.CalledProcessError) as failure:
            validate_production(self.release, Path(sys.executable), run=run)

        self.assertIn("Provision production LOG_DIR", failure.exception.stderr)
        self.assertFalse(self.logs.exists())
        self.assertFalse((self.release / "imported-log-dir.txt").exists())
        self.assertFalse((self.release / "app-created.txt").exists())


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
