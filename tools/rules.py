from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mcp.server.fastmcp import Context, FastMCP


@dataclass
class RuleItem:
    code: str
    description: str
    severity: str | None = None


@dataclass
class AgentRulesResult:
    title: str
    version: str
    hard_constraints: list[RuleItem] = field(default_factory=list)
    soft_guidance: list[RuleItem] = field(default_factory=list)
    style_guidance: list[RuleItem] = field(default_factory=list)


def register_rules_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    async def get_agent_rules(ctx: Context, agent_token: str) -> AgentRulesResult:
        """
        Fetch current NoviIs agent rules grouped as enforceable constraints, optional guidance, and style guidance.
        Use hard_constraints as non-negotiable boundaries; use guidance fields as context for autonomous choices.
        """
        runtime = ctx.request_context.lifespan_context
        payload = await runtime.client.get_agent_rules(token=agent_token)
        return build_agent_rules_result(payload)


def build_agent_rules_result(payload: dict[str, Any]) -> AgentRulesResult:
    data = _unwrap_data(payload)
    return AgentRulesResult(
        title=str(data.get("title", "")),
        version=str(data.get("version", "")),
        hard_constraints=_to_rule_items(
            data.get("hard_constraints", data.get("hardConstraints"))
        ),
        soft_guidance=_to_rule_items(
            data.get("soft_guidance", data.get("softGuidance"))
        ),
        style_guidance=_to_rule_items(
            data.get("style_guidance", data.get("styleGuidance"))
        ),
    )


def _unwrap_data(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    if isinstance(data, dict):
        return data
    return payload


def _to_rule_items(value: Any) -> list[RuleItem]:
    if not isinstance(value, list):
        return []
    items = []
    for item in value:
        if isinstance(item, dict):
            items.append(
                RuleItem(
                    code=str(item.get("code", "")),
                    description=str(item.get("description", "")),
                    severity=_optional_str(item.get("severity")),
                )
            )
    return items


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)
