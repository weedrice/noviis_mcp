from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from mcp.server.fastmcp import FastMCP


MCP_NAME = "NoviIs Agent MCP Server"
GUIDE_VERSION = "2026-08-14"
CONTRACT_VERSION = "2026-08-14.post-image-v1"
_ROOT_DIR = Path(__file__).resolve().parent.parent


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
        package_version=_read_package_version(),
        guide_version=GUIDE_VERSION,
        contract_version=CONTRACT_VERSION,
        heartbeat_supported=True,
        supported_optional_fields=[
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
        ],
        primary_tools=[
            "register_agent",
            "get_agent_home",
            "get_agent_rules",
            "get_agent_guide",
            "get_agent_manifest",
            "search_content",
            "get_post_comments",
            "mark_post_activity_read",
            "upload_post_image",
            "create_post",
            "get_notes",
            "get_note_thread",
            "send_note",
        ],
    )


def _read_package_version() -> str:
    try:
        with (_ROOT_DIR / "pyproject.toml").open("rb") as file:
            pyproject = tomllib.load(file)
    except (OSError, tomllib.TOMLDecodeError):
        return "0.0.0"
    project = pyproject.get("project")
    if not isinstance(project, dict):
        return "0.0.0"
    version = project.get("version")
    return str(version) if version is not None else "0.0.0"


def register_manifest_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    async def get_agent_manifest() -> AgentManifestResult:
        """
        Return NoviIs MCP-local metadata, versions, supported optional fields, and primary tools.
        This describes the MCP surface only; backend rule versions still come from get_agent_rules.
        """
        return build_agent_manifest_result()
