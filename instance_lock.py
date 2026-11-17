"""Process lifetime locking independent of MCP server initialization."""
from __future__ import annotations

import errno
import os
from pathlib import Path
from typing import BinaryIO

from exceptions import DuplicateInstanceError


class PIDLock:
    # Windows locks a byte range, including ranges beyond EOF. Keep that byte
    # separate from the PID text so another process can read the diagnostic.
    _LOCK_OFFSET = 64

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.acquired = False
        self._file: BinaryIO | None = None

    def acquire(self) -> None:
        if self.acquired:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        # Buffered reads can cross the Windows lock byte while reading the PID.
        lock_file = os.fdopen(descriptor, "r+b", buffering=0)
        try:
            try:
                self._lock(lock_file)
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                lock_file.seek(0)
                try:
                    existing_pid = int(lock_file.read(self._LOCK_OFFSET).decode("utf-8").strip())
                except (ValueError, UnicodeError, OSError):
                    existing_pid = None
                diagnostic = f" with PID {existing_pid}" if existing_pid and existing_pid > 0 else ""
                raise DuplicateInstanceError(
                    f"NoviIs MCP server already holds the instance lock{diagnostic}"
                ) from None
            lock_file.seek(0)
            lock_file.write(f"{os.getpid()}\n".encode("utf-8"))
            lock_file.truncate()
            lock_file.flush()
        except BaseException:
            lock_file.close()  # Closing also releases any successfully acquired OS lock.
            raise
        self._file = lock_file
        self.acquired = True

    @classmethod
    def _lock(cls, lock_file: BinaryIO) -> None:
        if os.name == "nt":
            import msvcrt

            lock_file.seek(cls._LOCK_OFFSET)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def release(self) -> None:
        lock_file = self._file
        if lock_file is None:
            return
        self._file = None
        self.acquired = False
        try:
            if os.name == "nt":
                import msvcrt

                lock_file.seek(self._LOCK_OFFSET)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            # Retain the inode: unlinking would let a successor lock a different
            # file while another contender still holds this one open.
            lock_file.close()

