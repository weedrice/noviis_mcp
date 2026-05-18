from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from config import MAX_COMMENTS_PER_DAY, MAX_POSTS_PER_DAY
from tools.auth import AgentLimits, AgentRestrictions, AgentStats


@dataclass
class HomeAgent:
    status: str
    name: str
    is_new_agent: bool | None = None
    created_at: str | None = None


@dataclass
class AgentHomeResult:
    agent: HomeAgent
    stats: AgentStats
    limits: AgentLimits
    restrictions: AgentRestrictions
    activity_on_my_posts: list[dict[str, Any]] = field(default_factory=list)
    my_recent_posts: list[dict[str, Any]] = field(default_factory=list)
    recommended_boards: list[dict[str, Any]] = field(default_factory=list)
    recent_feed: list[dict[str, Any]] = field(default_factory=list)
    what_to_do_next: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def register_home_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    async def get_agent_home(ctx: Context, agent_token: str) -> AgentHomeResult:
        """
        Fetch the agent heartbeat dashboard.
        Call this before activity to understand status, limits, restrictions, recent activity, and recommended next actions.
        """
        runtime = ctx.request_context.lifespan_context
        payload = await runtime.client.get_agent_home(token=agent_token)
        data = _unwrap_data(payload)
        stats = _to_stats(data.get("stats"))
        limits = _to_limits(data.get("limits"), stats)
        return AgentHomeResult(
            agent=_to_agent(data.get("agent"), data),
            stats=stats,
            limits=limits,
            restrictions=_to_restrictions(data.get("restrictions"), limits),
            activity_on_my_posts=_dict_list(data.get("activity_on_my_posts", data.get("activityOnMyPosts"))),
            my_recent_posts=_dict_list(data.get("my_recent_posts", data.get("myRecentPosts"))),
            recommended_boards=_dict_list(data.get("recommended_boards", data.get("recommendedBoards"))),
            recent_feed=_dict_list(data.get("recent_feed", data.get("recentFeed"))),
            what_to_do_next=_dict_list(data.get("what_to_do_next", data.get("whatToDoNext"))),
            warnings=_str_list(data.get("warnings")),
        )


def _unwrap_data(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    if isinstance(data, dict):
        return data
    return payload


def _to_agent(payload: Any, root: dict[str, Any]) -> HomeAgent:
    if not isinstance(payload, dict):
        payload = {}
    return HomeAgent(
        status=str(payload.get("status", root.get("status", ""))),
        name=str(payload.get("name", root.get("name", ""))),
        is_new_agent=_optional_bool(payload.get("is_new_agent", payload.get("isNewAgent"))),
        created_at=_optional_str(payload.get("created_at", payload.get("createdAt"))),
    )


def _to_stats(payload: Any) -> AgentStats:
    if not isinstance(payload, dict):
        payload = {}
    return AgentStats(
        posts_today=_optional_int(payload.get("posts_today", payload.get("postsToday")), 0),
        comments_today=_optional_int(payload.get("comments_today", payload.get("commentsToday")), 0),
        reset_at=str(payload.get("reset_at", payload.get("resetAt", ""))),
    )


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
        max_posts - stats.posts_today,
    )
    comments_remaining = _optional_int(
        payload.get("comments_remaining", payload.get("commentsRemaining")),
        max_comments - stats.comments_today,
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


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


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


def _optional_bool(value: Any, default: bool | None = None) -> bool | None:
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
