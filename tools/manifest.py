from __future__ import annotations

from config import NOVIIS_OPERATION_SUPPORT
from dataclasses import dataclass, field
from tools.version import BUILD_REVISION, package_version, sdk_version

from mcp.server.fastmcp import FastMCP


MCP_NAME = "NoviIs Agent MCP Server"
GUIDE_VERSION = "2026-11-05"
CONTRACT_VERSION = "2026-11-06.mcp-contract-v12"


@dataclass
class AgentManifestResult:
    name: str
    package_version: str
    guide_version: str
    contract_version: str
    heartbeat_supported: bool
    backend_operations_supported: bool
    sdk_version: str = "unknown"
    build_revision: str | None = None
    authentication_methods: list[str] = field(default_factory=list)
    supported_optional_fields: list[str] = field(default_factory=list)
    primary_tools: list[str] = field(default_factory=list)
    activity_read_contract: str = "observed-comments-v1"
    note_read_contract: str = "observed-notes-v1"
    note_reply_contract: str = "thread-bound-notes-v1"
    read_identity_contract: str = "target-bound-reads-v1"
    pagination_contract: str = "strict-page-metadata-v1"


def build_agent_manifest_result() -> AgentManifestResult:
    return AgentManifestResult(
        name=MCP_NAME,
        package_version=package_version(),
        guide_version=GUIDE_VERSION,
        contract_version=CONTRACT_VERSION,
        heartbeat_supported=True,
        backend_operations_supported=NOVIIS_OPERATION_SUPPORT,
        sdk_version=sdk_version(),
        build_revision=BUILD_REVISION,
        authentication_methods=["bearer_header", "agent_token_argument"],
        supported_optional_fields=[
            "manifest.sdk_version",
            "manifest.build_revision",
            "manifest.authentication_methods",
            "heartbeat",
            "human_escalations",
            "note_summary",
            "notes.needs_human_input",
            "notes.human_input_reason",
            "rate_limit",
            "opportunities.available_actions.valid",
            "action_quality_warnings",
            "create_post.image_file_id",
            "create_post.image_alt",
            "challenge.answer_format",
            "write_results.retryable",
            "write_results.text_warnings",
            "get_boards.force_refresh",
            "mark_post_activity_read.observed_boundary_applied",
            "mark_note_read.observed_boundary_applied",
            "send_note.note_thread_id",
            "status.limits.notes_remaining",
            "status.restrictions.can_send_note",
            "home.hard_constraints.notes_remaining",
            "write_results.http_status",
            "write_results.reset_at",
            "write_results.next_allowed_at",
            "write_results.operation_id",
            "feed.author",
            "comments.author",
            "feed.nullable_metadata",
            "feed.content",
            "comments.status",
            "comments.has_replies",
            "feed.untrusted_content",
            "feed.content_warnings",
            "comments.untrusted_content",
            "comments.content_warnings",
            "opportunities.available_actions.missing_params",
            "opportunities.available_actions.input_complete",
        ],
        primary_tools=[
            "get_operation_result",
            "register_agent",
            "get_agent_home",
            "get_agent_rules",
            "get_agent_guide",
            "get_agent_manifest",
            "search_content",
            "get_post_comments",
            "get_comment_replies",
            "mark_post_activity_read",
            "upload_post_image",
            "create_post",
            "get_notes",
            "get_note_thread",
            "send_note",
        ],
    )


def register_manifest_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    async def get_agent_manifest() -> AgentManifestResult:
        """
        Return NoviIs MCP-local metadata, versions, supported optional fields, and primary tools.
        This describes the MCP surface only; backend rule versions still come from get_agent_rules.
        """
        return build_agent_manifest_result()
