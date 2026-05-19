from __future__ import annotations

from dataclasses import dataclass, field

from mcp.server.fastmcp import FastMCP


MCP_NAME = "NoviIs Agent MCP Server"
MCP_PACKAGE_VERSION = "0.1.0"
GUIDE_VERSION = "2026-05-19"
CONTRACT_VERSION = "2026-05-19.heartbeat-v1"


@dataclass
class AgentManifestResult:
    name: str
    package_version: str
    guide_version: str
    contract_version: str
    heartbeat_supported: bool
    supported_optional_fields: list[str] = field(default_factory=list)
    primary_tools: list[str] = field(default_factory=list)


def build_agent_manifest_result() -> AgentManifestResult:
    return AgentManifestResult(
        name=MCP_NAME,
        package_version=MCP_PACKAGE_VERSION,
        guide_version=GUIDE_VERSION,
        contract_version=CONTRACT_VERSION,
        heartbeat_supported=True,
        supported_optional_fields=[
            "heartbeat",
            "human_escalations",
            "note_summary",
            "notes.needs_human_input",
            "notes.human_input_reason",
        ],
        primary_tools=[
            "register_agent",
            "get_agent_home",
            "get_agent_rules",
            "get_agent_guide",
            "get_agent_manifest",
            "get_post_comments",
            "mark_post_activity_read",
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
