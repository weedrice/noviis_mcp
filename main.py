from __future__ import annotations

import atexit
import errno
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, BinaryIO

import uvicorn
from tools.annotations import AnnotatedFastMCP
import anyio
from mcp.server.fastmcp import FastMCP

from challenge import ChallengeManager
from client import NoviIsClient
from config import (
    LOCK_FILE_PATH, LOG_LEVEL, MCP_MAX_REQUEST_BYTES, MCP_SERVER_HOST, MCP_SERVER_PORT,
    MCP_MAX_CONCURRENT_REQUESTS, MCP_REQUEST_LIMIT, MCP_REQUEST_WINDOW_SECONDS, MCP_REQUEST_TIMEOUT_SECONDS,
)
from exceptions import DuplicateInstanceError
from logging_utils import build_uvicorn_log_config, configure_logging
from tools import (
    register_activity_tools,
    register_auth_tools,
    register_guide_tools,
    register_home_tools,
    register_manifest_tools,
    register_note_tools,
    register_operation_tools,
    register_rules_tools,
    register_search_tools,
)
from tools.instructions import MCP_SERVER_INSTRUCTIONS
from tools.version import package_version
from transport import RequestLimitMiddleware, RequestSizeLimitMiddleware, current_request

configure_logging()


@dataclass
class AppRuntime:
    client: NoviIsClient
    challenge_manager: ChallengeManager


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


pid_lock = PIDLock(LOCK_FILE_PATH)


def create_mcp_server() -> FastMCP:
    # Stateless HTTP creates a runtime per request; challenge state belongs to
    # the server instance so verification and limits survive HTTP reconnects.
    challenge_manager = ChallengeManager()

    @asynccontextmanager
    async def mcp_lifespan(_: FastMCP) -> AsyncIterator[AppRuntime]:
        lifetime = current_request.get()
        with anyio.CancelScope(deadline=lifetime.deadline if lifetime is not None else float("inf")) as scope:
            if lifetime is not None:
                lifetime.cancel = scope.cancel
            client = NoviIsClient()
            try:
                yield AppRuntime(client=client, challenge_manager=challenge_manager)
            finally:
                with anyio.move_on_after(5, shield=True):
                    await client.aclose()

    mcp = AnnotatedFastMCP(
        name="NoviIs Agent MCP Server",
        instructions=MCP_SERVER_INSTRUCTIONS,
        log_level=LOG_LEVEL,
        host=MCP_SERVER_HOST,
        port=MCP_SERVER_PORT,
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        lifespan=mcp_lifespan,
    )
    # FastMCP 1.26 does not expose a version constructor argument. Keep this
    # narrow SDK adapter covered by the protocol initialization smoke test.
    mcp._mcp_server.version = package_version()
    register_auth_tools(mcp)
    register_guide_tools(mcp)
    register_manifest_tools(mcp)
    register_rules_tools(mcp)
    register_home_tools(mcp)
    register_search_tools(mcp)
    register_activity_tools(mcp)
    register_note_tools(mcp)
    register_operation_tools(mcp)
    return mcp


def create_app() -> Any:
    mcp = create_mcp_server()
    application = mcp.streamable_http_app()
    application.add_middleware(RequestSizeLimitMiddleware, max_body_bytes=MCP_MAX_REQUEST_BYTES)
    application.add_middleware(
        RequestLimitMiddleware, max_concurrent=MCP_MAX_CONCURRENT_REQUESTS,
        request_limit=MCP_REQUEST_LIMIT, window_seconds=MCP_REQUEST_WINDOW_SECONDS,
        timeout_seconds=MCP_REQUEST_TIMEOUT_SECONDS,
    )
    return application


app = create_app()


def main() -> None:
    pid_lock.acquire()
    atexit.register(pid_lock.release)
    uvicorn.run(
        app,
        host=MCP_SERVER_HOST,
        port=MCP_SERVER_PORT,
        log_level=LOG_LEVEL.lower(),
        log_config=build_uvicorn_log_config(),
        access_log=True,
    )


if __name__ == "__main__":
    main()
