from __future__ import annotations

import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

with patch("logging_utils.configure_logging"), patch("mcp.server.fastmcp.server.configure_logging"):
    from main import PIDLock
from exceptions import DuplicateInstanceError


class PIDLockTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "instance.lock"

    def lock(self):
        lock = PIDLock(str(self.path))
        self.addCleanup(lock.release)
        return lock

    def test_stale_pid_is_diagnostic_only_and_no_process_probe_is_used(self):
        self.path.write_text("999999999\n", encoding="utf-8")
        owner = self.lock()
        with patch("os.kill", side_effect=AssertionError("Must not probe or signal processes")):
            owner.acquire()
            owner.acquire()  # Reentrant startup must not discard its owned handle.
            self.assertEqual(int(self.path.read_text(encoding="utf-8")), os.getpid())
            with self.assertRaisesRegex(DuplicateInstanceError, f"PID {os.getpid()}"):
                self.lock().acquire()
        self.assertTrue(owner.acquired)

    def test_release_keeps_file_and_cannot_remove_successors_lock(self):
        first, successor, contender = self.lock(), self.lock(), self.lock()
        first.acquire()
        first.release()
        self.assertTrue(self.path.exists())
        successor.acquire()
        first.release()
        contender.release()  # A never-acquired instance has no authority to release.
        self.assertTrue(self.path.exists())
        with self.assertRaises(DuplicateInstanceError):
            contender.acquire()
        successor.release()
        contender.acquire()
        self.assertTrue(contender.acquired)

    def test_simultaneous_acquisition_has_exactly_one_owner(self):
        barrier = threading.Barrier(4)
        attempted = threading.Event()
        release = threading.Event()
        guard = threading.Lock()
        outcomes = []

        def contender():
            lock = PIDLock(str(self.path))
            try:
                barrier.wait(timeout=5)
                try:
                    lock.acquire()
                    outcome = "owner"
                except DuplicateInstanceError:
                    outcome = "blocked"
                except Exception as exc:
                    outcome = repr(exc)
                with guard:
                    outcomes.append(outcome)
                    if len(outcomes) == 4:
                        attempted.set()
                if lock.acquired:
                    release.wait(timeout=10)
            finally:
                lock.release()

        threads = [threading.Thread(target=contender) for _ in range(4)]
        for thread in threads:
            thread.start()
        try:
            self.assertTrue(attempted.wait(timeout=5))
            self.assertCountEqual(outcomes, ["owner", "blocked", "blocked", "blocked"])
        finally:
            release.set()
            for thread in threads:
                thread.join(timeout=5)
        self.lock().acquire()

    def test_os_releases_lock_when_isolated_owner_exits_without_cleanup(self):
        script = """
import os, sys
from unittest.mock import patch
with patch('logging_utils.configure_logging'), patch('mcp.server.fastmcp.server.configure_logging'):
    from main import PIDLock
lock = PIDLock(sys.argv[1])
lock.acquire()
print('locked', flush=True)
sys.stdin.readline()
os._exit(0)
"""
        child = subprocess.Popen([sys.executable, "-u", "-c", script, str(self.path)],
                                 cwd=Path(__file__).resolve().parents[1], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
        ready = queue.Queue()
        reader = threading.Thread(target=lambda: ready.put(child.stdout.readline()), daemon=True)
        reader.start()
        try:
            self.assertEqual(ready.get(timeout=20).strip(), "locked")
            contender = self.lock()
            with self.assertRaisesRegex(DuplicateInstanceError, f"PID {child.pid}"):
                contender.acquire()
        finally:
            # Only this test child is controlled, through its cooperative stdin
            # protocol; never send OS signals or touch a running server.
            if child.poll() is None:
                child.stdin.write("exit\n")
                child.stdin.flush()
            child.wait(timeout=20)
            reader.join(timeout=5)
            child.stdin.close()
            child.stdout.close()
            child.stderr.close()
        self.assertEqual(child.returncode, 0)
        self.assertTrue(self.path.exists())
        self.lock().acquire()


if __name__ == "__main__":
    unittest.main()
