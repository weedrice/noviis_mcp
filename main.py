from __future__ import annotations

import atexit
from dataclasses import dataclass, replace
from typing import Any

import uvicorn
from tools.annotations import AnnotatedMCPServer
import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.context import ServerRequestContext, CallNext
from mcp.server.transport_security import TransportSecuritySettings

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


def create_mcp_server() -> MCPServer:
    # SDK 2.x lifespans are server-wide. Only challenge state is shared;
    # backend clients and cancellation scopes belong to individual messages.
    challenge_manager = ChallengeManager()

    async def request_runtime(ctx: ServerRequestContext, call_next: CallNext):
        lifetime = current_request.get()
        with anyio.CancelScope(deadline=lifetime.deadline if lifetime is not None else float("inf")) as scope:
            if lifetime is not None:
                lifetime.cancel = scope.cancel
            client = NoviIsClient()
            try:
                return await call_next(replace(ctx, lifespan_context=AppRuntime(
                    client=client, challenge_manager=challenge_manager)))
            finally:
                with anyio.move_on_after(5, shield=True):
                    await client.aclose()
                if lifetime is not None:
                    lifetime.cancel = None

    mcp = AnnotatedMCPServer(
        name="NoviIs Agent MCP Server",
        version=package_version(),
        instructions=MCP_SERVER_INSTRUCTIONS,
        log_level=LOG_LEVEL,
        middleware=[request_runtime],
    )
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
    security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    if MCP_SERVER_HOST in ("127.0.0.1", "localhost", "::1"):
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"],
            allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"],
        )
    application = mcp.streamable_http_app(
        streamable_http_path="/mcp", json_response=True, stateless_http=True,
        host=MCP_SERVER_HOST, transport_security=security,
        max_request_body_size=MCP_MAX_REQUEST_BYTES,
    )
    application.add_middleware(
        RequestSizeLimitMiddleware, max_body_bytes=MCP_MAX_REQUEST_BYTES,
        rpc_path="/mcp", transport_security=security,
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
