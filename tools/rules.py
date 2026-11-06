from __future__ import annotations

from tools.authentication import resolve_agent_token
from tools.input_validation import validate_tool_inputs
from tools.parsing import validate_response_envelope

from dataclasses import dataclass, field
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from tools.status_contract import policy_field, policy_object


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
    async def get_agent_rules(ctx: Context, agent_token: str | None = None) -> AgentRulesResult:
        """
        Fetch current NoviIs agent rules grouped as enforceable constraints, optional guidance, and style guidance.
        Use hard_constraints as non-negotiable boundaries; use guidance fields as context for autonomous choices.
        """
        validate_tool_inputs(locals())
        agent_token = resolve_agent_token(ctx, agent_token)
        runtime = ctx.request_context.lifespan_context
        payload = await runtime.client.get_agent_rules(token=agent_token)
        return build_agent_rules_result(payload)


def build_agent_rules_result(payload: dict[str, Any]) -> AgentRulesResult:
    validate_response_envelope(payload)
    policy_object(payload, "rules response")
    data = policy_object(payload.get("data", payload), "rules")
    return AgentRulesResult(
        title=policy_field(data, "title", str, label="rules", required=False, nullable=True) or "",
        version=policy_field(data, "version", str, label="rules", required=False, nullable=True) or "",
        hard_constraints=_to_rule_items(policy_field(data, "hard_constraints", list, label="rules", required=False)),
        soft_guidance=_to_rule_items(policy_field(data, "soft_guidance", list, label="rules", required=False)),
        style_guidance=_to_rule_items(policy_field(data, "style_guidance", list, label="rules", required=False)),
    )


def _to_rule_items(value: list[Any] | None) -> list[RuleItem]:
    items = []
    for raw_item in value or []:
        item = policy_object(raw_item, "rule")
        items.append(RuleItem(
            code=policy_field(item, "code", str, label="rule"),
            description=policy_field(item, "description", str, label="rule"),
            severity=policy_field(item, "severity", str, label="rule", required=False, nullable=True),
        ))
    return items
