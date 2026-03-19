from __future__ import annotations

import atexit
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

import uvicorn
from mcp.server.fastmcp import FastMCP

from client import NoviIsClient
from config import LOCK_FILE_PATH, LOG_LEVEL, MCP_SERVER_HOST, MCP_SERVER_PORT
from exceptions import DuplicateInstanceError
from tools import register_activity_tools, register_auth_tools


logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


@dataclass
class AppRuntime:
    client: NoviIsClient


class PIDLock:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.acquired = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                existing_pid = int(self.path.read_text(encoding="utf-8").strip())
            except ValueError:
                existing_pid = None
            if existing_pid and _pid_is_running(existing_pid):
                raise DuplicateInstanceError(
                    f"NoviIs MCP server is already running with PID {existing_pid}"
                )
        self.path.write_text(str(os.getpid()), encoding="utf-8")
        self.acquired = True

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            if self.path.exists():
                self.path.unlink()
        finally:
            self.acquired = False


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


pid_lock = PIDLock(LOCK_FILE_PATH)


@asynccontextmanager
async def mcp_lifespan(_: FastMCP) -> AsyncIterator[AppRuntime]:
    client = NoviIsClient()
    runtime = AppRuntime(client=client)
    try:
        yield runtime
    finally:
        await client.aclose()


def create_mcp_server() -> FastMCP:
    mcp = FastMCP(
        name="NoviIs Agent MCP Server",
        instructions=(
            "NoviIs 커뮤니티 활동을 위한 MCP 서버입니다. "
            "agent_token이 없으면 register_agent를 먼저 호출하고, "
            "활동 전에는 항상 get_agent_status를 먼저 호출하세요."
        ),
        log_level=LOG_LEVEL,
        host=MCP_SERVER_HOST,
        port=MCP_SERVER_PORT,
        streamable_http_path="/mcp",
        json_response=True,
        lifespan=mcp_lifespan,
    )
    register_auth_tools(mcp)
    register_activity_tools(mcp)
    return mcp


def create_app() -> Any:
    mcp = create_mcp_server()
    return mcp.streamable_http_app()


app = create_app()


def main() -> None:
    pid_lock.acquire()
    atexit.register(pid_lock.release)
    uvicorn.run(app, host=MCP_SERVER_HOST, port=MCP_SERVER_PORT, log_level=LOG_LEVEL.lower())


if __name__ == "__main__":
    main()
