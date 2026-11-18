from __future__ import annotations

import atexit
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator

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
from instance_lock import PIDLock
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
    application.add_middleware(
        RequestSizeLimitMiddleware, max_body_bytes=MCP_MAX_REQUEST_BYTES,
        rpc_path=mcp.settings.streamable_http_path, transport_security=mcp.settings.transport_security,
    )
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
