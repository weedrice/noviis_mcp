from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from exceptions import DuplicateInstanceError
from instance_lock import PIDLock


class InstanceLockTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "instance.lock"

    def run_child(self, code):
        return subprocess.run(
            [sys.executable, "-c", code, str(self.path)],
            cwd=Path(__file__).resolve().parents[1], capture_output=True,
            text=True, encoding="utf-8", timeout=10, check=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        ).stdout.strip()

    def test_stale_or_malformed_file_never_blocks_startup(self):
        dead_pid = self.run_child("import os; print(os.getpid())")
        for contents in (dead_pid, str(os.getpid()), "", "not a pid"):
            with self.subTest(contents=contents):
                self.path.write_text(contents, encoding="utf-8")
                lock = PIDLock(str(self.path))
                try:
                    lock.acquire()
                    self.assertTrue(lock.acquired)
                finally:
                    lock.release()
                self.assertEqual(self.path.read_text(encoding="utf-8").strip(), str(os.getpid()))

    def test_second_process_is_blocked_without_affecting_owner(self):
        lock = PIDLock(str(self.path))
        lock.acquire()
        try:
            result = self.run_child(
                "import sys\nfrom instance_lock import PIDLock\n"
                "from exceptions import DuplicateInstanceError\n"
                "try:\n    PIDLock(sys.argv[1]).acquire()\n"
                "except DuplicateInstanceError:\n    print('blocked')\n"
                "else:\n    raise AssertionError('acquired occupied lock')\n"
            )
            self.assertEqual(result, "blocked")
            self.assertTrue(lock.acquired)
        finally:
            lock.release()
        self.assertEqual(self.run_child(
            "import sys\nfrom instance_lock import PIDLock\n"
            "lock = PIDLock(sys.argv[1]); lock.acquire(); print('acquired'); lock.release()"
        ), "acquired")

    def test_abrupt_owner_exit_releases_os_lock(self):
        self.run_child(
            "import os, sys\nfrom instance_lock import PIDLock\n"
            "lock = PIDLock(sys.argv[1]); lock.acquire(); os._exit(0)"
        )
        self.assertTrue(self.path.exists())
        lock = PIDLock(str(self.path))
        try:
            lock.acquire()
            self.assertTrue(lock.acquired)
        finally:
            lock.release()

    def test_same_process_contenders_and_repeated_release(self):
        first, second = PIDLock(str(self.path)), PIDLock(str(self.path))
        try:
            first.acquire()
            first.acquire()
            with self.assertRaises(DuplicateInstanceError):
                second.acquire()
            self.assertFalse(second.acquired)
            first.release()
            first.release()
            second.acquire()
            self.assertTrue(second.acquired)
            self.assertTrue(self.path.exists())
        finally:
            first.release()
            second.release()
