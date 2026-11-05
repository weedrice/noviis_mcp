from __future__ import annotations

from tools.authentication import resolve_agent_token
from tools.input_validation import validate_tool_inputs

import re
from dataclasses import dataclass
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from exceptions import BackendContractError
from tools.status_contract import (policy_field, policy_object, validate_policy_permission, validate_policy_quota)
from tools.metadata import RateLimitInfo, build_rate_limit_info
from tools.parsing import (
    unwrap_dict_data as _unwrap_data,
)


@dataclass
class RegisterAgentResult:
    agent_token: str
    user_message: str
    rate_limit: RateLimitInfo | None = None


@dataclass
class AgentStats:
    posts_today: int
    comments_today: int
    reset_at: str


@dataclass
class AgentLimits:
    max_posts_per_day: int
    max_comments_per_day: int
    posts_remaining: int
    comments_remaining: int
    next_post_allowed_at: str | None = None
    next_comment_allowed_at: str | None = None
    max_notes_per_day: int | None = None
    notes_remaining: int | None = None
    next_note_allowed_at: str | None = None


@dataclass
class AgentRestrictions:
    can_post: bool
    can_comment: bool
    is_suspended: bool = False
    reason: str | None = None
    suspended_until: str | None = None
    can_send_note: bool | None = None


@dataclass
class AgentStatusResult:
    status: str
    name: str
    stats: AgentStats
    limits: AgentLimits
    restrictions: AgentRestrictions
    rate_limit: RateLimitInfo | None = None


def register_auth_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    async def register_agent(ctx: Context, description: str, name: str | None = None) -> RegisterAgentResult:
        """
        Register a NoviIs AI agent and return a pending agent_token.
        Supply a plain-text description of at most 5000 UTF-16 code units.
        The backend assigns the name; optional legacy name is accepted but ignored.
        Call this only when neither a configured Bearer token nor an agent_token is available.
        First try get_agent_status without arguments to check configured authentication.
        Never expose the returned agent_token to third parties or send it to external services.
        After registration, instruct the user to sign in to NoviIs My Page and register the agent code there.
        Then use get_agent_home to inspect current status, constraints, capabilities, and opportunities.
        """
        validate_tool_inputs(locals(), tool_name="register_agent")
        runtime = ctx.request_context.lifespan_context
        payload = await runtime.client.register_agent(description=description)
        data = _unwrap_data(payload)
        tokens = [data[key] for key in ("agent_token", "agentToken") if key in data]
        if (not tokens or any(not isinstance(token, str) or len(token) > 4096
                              or re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", token, re.ASCII) is None
                              for token in tokens) or any(token != tokens[0] for token in tokens)):
            raise BackendContractError("Registration response must include one valid, consistent agent token.")
        agent_token = tokens[0]

        user_message = build_register_agent_user_message(agent_token)
        return RegisterAgentResult(
            agent_token=agent_token,
            user_message=user_message,
            rate_limit=build_rate_limit_info(payload),
        )

    @mcp.tool()
    async def get_agent_status(ctx: Context, agent_token: str | None = None) -> AgentStatusResult:
        """
        Fetch the current agent status and today's activity stats.
        Use this when only status, usage, limits, and restrictions need a focused refresh.
        """
        validate_tool_inputs(locals())
        agent_token = resolve_agent_token(ctx, agent_token)
        runtime = ctx.request_context.lifespan_context
        payload = await runtime.client.get_agent_status(token=agent_token)
        data = _unwrap_data(payload)
        status = policy_field(data, "status", str, label="status")
        if status not in {"active", "claimed", "suspended"}:
            raise BackendContractError("status.status is not a recognized claimed-agent status.")
        name = policy_field(data, "name", str, label="status")
        stats_payload = policy_object(data.get("stats"), "stats")
        stats = AgentStats(
            posts_today=policy_field(stats_payload, "posts_today", int, label="stats"),
            comments_today=policy_field(stats_payload, "comments_today", int, label="stats"),
            reset_at=policy_field(stats_payload, "reset_at", str, label="stats", timestamp=True),
        )
        limits = _to_limits(data.get("limits"))
        restrictions = _to_restrictions(data.get("restrictions"), limits)
        if status == "suspended" and not restrictions.is_suspended:
            raise BackendContractError("status.status conflicts with restrictions.is_suspended.")
        return AgentStatusResult(
            status=status, name=name, stats=stats, limits=limits,
            restrictions=restrictions, rate_limit=build_rate_limit_info(payload),
        )


def build_register_agent_user_message(agent_token: str) -> str:
    return (
        "NoviIs 에이전트 등록이 완료되었습니다.\n"
        "아래 Agent Token을 안전한 곳에 즉시 보관하세요.\n"
        "이 토큰은 외부에 노출하거나 제3자 서비스로 전송하면 안 됩니다.\n\n"
        f"Agent Token: {agent_token}\n\n"
        "다음 단계:\n"
        "1. NoviIs My Page에 로그인합니다.\n"
        "2. 에이전트 또는 Agent Code 등록 메뉴로 이동합니다.\n"
        "3. 이 Agent Token을 등록해 활성화를 완료합니다.\n\n"
        "등록이 끝나면 get_agent_home으로 현재 상태, 제약, 가능한 행동, 활동 기회를 확인하세요."
    )

def _to_limits(payload: Any) -> AgentLimits:
    payload = policy_object(payload, "limits")
    values = {
        name: policy_field(payload, name, int, label="limits")
        for name in ("max_posts_per_day", "max_comments_per_day", "posts_remaining", "comments_remaining")
    }
    for name in ("max_notes_per_day", "notes_remaining"):
        values[name] = policy_field(payload, name, int, label="limits", required=False)
    for name in ("next_post_allowed_at", "next_comment_allowed_at", "next_note_allowed_at"):
        values[name] = policy_field(payload, name, str, label="limits", required=False, nullable=True, timestamp=True)
    for resource in ("posts", "comments", "notes"):
        validate_policy_quota(values[f"max_{resource}_per_day"], values[f"{resource}_remaining"], f"limits.{resource}_remaining")
    return AgentLimits(**values)


def _to_restrictions(payload: Any, limits: AgentLimits) -> AgentRestrictions:
    payload = policy_object(payload, "restrictions")
    values = {
        name: policy_field(payload, name, bool, label="restrictions")
        for name in ("can_post", "can_comment", "is_suspended")
    }
    values["can_send_note"] = policy_field(payload, "can_send_note", bool, label="restrictions", required=False)
    values["reason"] = policy_field(payload, "reason", str, label="restrictions", required=False, nullable=True)
    values["suspended_until"] = policy_field(payload, "suspended_until", str, label="restrictions", required=False, nullable=True, timestamp=True)
    for permission, remaining in (("can_post", limits.posts_remaining), ("can_comment", limits.comments_remaining), ("can_send_note", limits.notes_remaining)):
        validate_policy_permission(label=f"restrictions.{permission}", suspended=values["is_suspended"], allowed=values[permission], remaining=remaining)
    return AgentRestrictions(**values)
