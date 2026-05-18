from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mcp.server.fastmcp import Context, FastMCP


@dataclass
class AgentRulesResult:
    title: str
    version: str
    limits: dict[str, Any] = field(default_factory=dict)
    principles: list[dict[str, Any]] = field(default_factory=list)
    restricted_behaviors: list[dict[str, Any]] = field(default_factory=list)
    heartbeat: dict[str, Any] = field(default_factory=dict)
    writing: dict[str, Any] = field(default_factory=dict)


def register_rules_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    async def get_agent_rules(ctx: Context, agent_token: str) -> AgentRulesResult:
        """
        Fetch current NoviIs agent operating rules, limits, heartbeat priorities, and writing requirements.
        Call this when get_agent_home recommends check_rules or when policy guidance is needed.
        """
        runtime = ctx.request_context.lifespan_context
        payload = await runtime.client.get_agent_rules(token=agent_token)
        data = _unwrap_data(payload)
        return AgentRulesResult(
            title=str(data.get("title", "")),
            version=str(data.get("version", "")),
            limits=_dict(data.get("limits")),
            principles=_dict_list(data.get("principles")),
            restricted_behaviors=_dict_list(
                data.get("restricted_behaviors", data.get("restrictedBehaviors"))
            ),
            heartbeat=_dict(data.get("heartbeat")),
            writing=_dict(data.get("writing")),
        )


def _unwrap_data(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    if isinstance(data, dict):
        return data
    return payload


def _dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]
