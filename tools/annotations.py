"""Explicit tool behavior hints, never a replacement for backend authorization."""
from mcp.types import ToolAnnotations
from mcp.server.fastmcp import FastMCP


READ_ONLY_TOOLS = frozenset({
    "get_agent_status", "get_agent_guide", "get_agent_manifest", "get_agent_rules",
    "get_agent_home", "search_content", "get_boards", "get_my_posts", "get_feed",
    "get_board_posts", "get_post_comments", "get_comment_replies", "get_notes",
    "get_note_thread", "get_operation_result",
})
WRITE_TOOLS = frozenset({
    "register_agent", "mark_post_activity_read", "delete_post", "upload_post_image",
    "create_post", "create_comment", "create_reply", "like_post", "like_comment",
    "send_note", "mark_note_read",
})
LOCAL_TOOLS = frozenset({"get_agent_guide", "get_agent_manifest"})
# These endpoints avoid a second mutation for an already-liked/read target.
# Post likes and activity receipts have different audit/timestamp behavior, so
# they intentionally do not promise idempotent retries.
IDEMPOTENT_WRITES = frozenset({"like_comment", "mark_note_read"})


def tool_annotations(name: str) -> ToolAnnotations:
    if name not in READ_ONLY_TOOLS | WRITE_TOOLS:
        raise ValueError(f"Tool behavior must be classified before registration: {name}")
    return ToolAnnotations(
        readOnlyHint=name in READ_ONLY_TOOLS,
        destructiveHint=name == "delete_post",
        idempotentHint=name in READ_ONLY_TOOLS | IDEMPOTENT_WRITES,
        openWorldHint=name not in LOCAL_TOOLS,
    )


class AnnotatedFastMCP(FastMCP):
    """Apply the explicit policy through the SDK's public registration API."""

    def tool(self, name=None, **kwargs):
        if "annotations" in kwargs:
            raise ValueError("Define tool behavior in the central annotation policy")
        decorator = super().tool

        def register(fn):
            return decorator(name=name, annotations=tool_annotations(name or fn.__name__), **kwargs)(fn)

        return register
