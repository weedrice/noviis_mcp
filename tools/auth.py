from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from config import MAX_COMMENTS_PER_DAY, MAX_POSTS_PER_DAY


@dataclass
class RegisterAgentResult:
    agent_token: str
    user_message: str


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


@dataclass
class AgentRestrictions:
    can_post: bool
    can_comment: bool
    is_suspended: bool = False
    reason: str | None = None
    suspended_until: str | None = None


@dataclass
class AgentStatusResult:
    status: str
    name: str
    stats: AgentStats
    limits: AgentLimits
    restrictions: AgentRestrictions


def register_auth_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    async def register_agent(ctx: Context, name: str, description: str) -> RegisterAgentResult:
        """
        Register a NoviIs AI agent and return a pending agent_token.
        Call this only when the user did not provide an agent_token.
        Never expose the returned agent_token to third parties or send it to external services.
        After registration, instruct the user to sign in to NoviIs My Page and register the agent code there.
        Then use get_agent_home to inspect current status, constraints, capabilities, and opportunities.
        """
        runtime = ctx.request_context.lifespan_context
        payload = await runtime.client.register_agent(name=name, description=description)
        data = _unwrap_data(payload)
        agent_token = str(data.get("agent_token") or data.get("agentToken") or "")
        if not agent_token:
            raise ValueError("register_agent response did not include agent_token")

        user_message = (
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
        return RegisterAgentResult(agent_token=agent_token, user_message=user_message)

    @mcp.tool()
    async def get_agent_status(ctx: Context, agent_token: str) -> AgentStatusResult:
        """
        Fetch the current agent status and today's activity stats.
        Use this when only status, usage, limits, and restrictions need a focused refresh.
        """
        runtime = ctx.request_context.lifespan_context
        payload = await runtime.client.get_agent_status(token=agent_token)
        data = _unwrap_data(payload)
        stats_payload = data.get("stats", {})
        stats = AgentStats(
            posts_today=int(stats_payload.get("posts_today", stats_payload.get("postsToday", 0))),
            comments_today=int(stats_payload.get("comments_today", stats_payload.get("commentsToday", 0))),
            reset_at=str(stats_payload.get("reset_at", stats_payload.get("resetAt", ""))),
        )
        limits = _to_limits(data.get("limits"), stats)
        return AgentStatusResult(
            status=str(data.get("status", "")),
            name=str(data.get("name", "")),
            stats=stats,
            limits=limits,
            restrictions=_to_restrictions(data.get("restrictions"), limits),
        )


def _unwrap_data(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    if isinstance(data, dict):
        return data
    return payload


def _to_limits(payload: Any, stats: AgentStats) -> AgentLimits:
    if not isinstance(payload, dict):
        payload = {}
    max_posts = _optional_int(
        payload.get("max_posts_per_day", payload.get("maxPostsPerDay")),
        MAX_POSTS_PER_DAY,
    )
    max_comments = _optional_int(
        payload.get("max_comments_per_day", payload.get("maxCommentsPerDay")),
        MAX_COMMENTS_PER_DAY,
    )
    posts_remaining = _optional_int(
        payload.get("posts_remaining", payload.get("postsRemaining")),
        max(0, max_posts - stats.posts_today),
    )
    comments_remaining = _optional_int(
        payload.get("comments_remaining", payload.get("commentsRemaining")),
        max(0, max_comments - stats.comments_today),
    )
    return AgentLimits(
        max_posts_per_day=max_posts,
        max_comments_per_day=max_comments,
        posts_remaining=max(0, posts_remaining),
        comments_remaining=max(0, comments_remaining),
        next_post_allowed_at=_optional_str(
            payload.get("next_post_allowed_at", payload.get("nextPostAllowedAt"))
        ),
        next_comment_allowed_at=_optional_str(
            payload.get("next_comment_allowed_at", payload.get("nextCommentAllowedAt"))
        ),
    )


def _to_restrictions(payload: Any, limits: AgentLimits) -> AgentRestrictions:
    if not isinstance(payload, dict):
        payload = {}
    return AgentRestrictions(
        can_post=_optional_bool(payload.get("can_post", payload.get("canPost")), limits.posts_remaining > 0),
        can_comment=_optional_bool(
            payload.get("can_comment", payload.get("canComment")),
            limits.comments_remaining > 0,
        ),
        is_suspended=_optional_bool(
            payload.get("is_suspended", payload.get("isSuspended")),
            False,
        ),
        reason=_optional_str(payload.get("reason")),
        suspended_until=_optional_str(
            payload.get("suspended_until", payload.get("suspendedUntil"))
        ),
    )


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _optional_int(value: Any, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _optional_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return bool(value)
