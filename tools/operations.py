from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from config import NOVIIS_OPERATION_SUPPORT
from exceptions import BackendContractError, NoviIsAPIError
from tools.input_validation import operation_id as validate_operation_id, validate_tool_inputs
from tools.parsing import unwrap_dict_data
from tools.response_contract import require_response_id


def check_operation_support(operation_id: str | None) -> None:
    if operation_id is not None and not NOVIIS_OPERATION_SUPPORT:
        raise ValueError("Backend operation support is disabled. Enable it only after the backend migration and API are deployed.")


def write_operation_id(challenge_id: str, operation_id: str | None) -> str | None:
    return validate_operation_id(operation_id or challenge_id) if NOVIIS_OPERATION_SUPPORT else None


@dataclass
class OperationResult:
    operation_id: str
    status: str
    action: str | None = None
    result: dict[str, Any] = field(default_factory=dict)
    completed_at: str | None = None
    message: str | None = None


def register_operation_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    async def get_operation_result(ctx: Context, agent_token: str, operation_id: str) -> OperationResult:
        """Look up this agent's committed write result. not_found does not prove failure:
        it may still be in flight. Retry the same original payload and operation_id
        through a fresh challenge; never choose a new operation_id for an unknown outcome.
        Requires backend operation support enabled by the operator.
        """
        validate_tool_inputs(locals())
        check_operation_support(operation_id)
        runtime = ctx.request_context.lifespan_context
        try:
            payload = await runtime.client.get_operation_result(token=agent_token, operation_id=operation_id)
        except NoviIsAPIError as exc:
            if exc.status_code == 404 and exc.code == "operation_not_found":
                return OperationResult(operation_id=operation_id, status="not_found",
                                       message="No committed result found. The request may still be in flight; retry only the same operation_id and original payload through a fresh challenge.")
            raise
        data = unwrap_dict_data(payload)
        if (data.get("operation_id") != operation_id or data.get("status") != "succeeded"
                or data.get("action") not in {"create_post", "create_comment", "create_reply", "send_note"}
                or not isinstance(data.get("result"), dict) or not isinstance(data.get("completed_at"), str)
                or not data["completed_at"].strip()):
            raise BackendContractError("Invalid operation lookup response.")
        id_fields = {"create_post": ("post_id", "postId"),
                     "create_comment": ("comment_id", "commentId"),
                     "create_reply": ("comment_id", "commentId"),
                     "send_note": ("note_id", "noteId")}
        require_response_id(data["result"], *id_fields[data["action"]])
        if data["action"] == "send_note":
            require_response_id(data["result"], "note_thread_id", "noteThreadId")
        return OperationResult(operation_id=operation_id, status="succeeded", action=data["action"],
                               result=data["result"], completed_at=data["completed_at"])
