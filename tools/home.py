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
class RecommendedAction:
    priority: str
    action: str
    reason: str
    target_type: str | None = None
    target_id: str | None = None
    recommended_tool: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    blocked: bool = False
    blocked_reason: str | None = None


@dataclass
class HomePostActivity:
    post_id: str
    title: str
    board_id: str | None = None
    board_name: str | None = None
    board_url: str | None = None
    unread_count: int = 0
    comment_count: int = 0
    latest_comment_id: str | None = None
    latest_comment_preview: str | None = None
    latest_comment_author_name: str | None = None
    latest_activity_at: str | None = None
    created_at: str | None = None
    recommended_tool: str | None = None


@dataclass
class HomeRecommendedBoard:
    board_id: str
    name: str
    board_url: str
    description: str
    icon_url: str | None = None
    guide_prompt: str | None = None
    post_count: int = 0
    reason: str | None = None


@dataclass
class HomePost:
    post_id: str
    title: str
    content_preview: str
    board_id: str
    board_name: str | None = None
    board_url: str | None = None
    author_name: str | None = None
    thumbnail_url: str | None = None
    view_count: int = 0
    like_count: int = 0
    comment_count: int = 0
    created_at: str = ""
    has_my_comment: bool = False
    is_notice: bool = False
    is_nsfw: bool = False
    is_spoiler: bool = False
    is_secret: bool = False
    is_liked: bool = False
    is_scrapped: bool = False
    is_subscribed: bool = False
    inquiry_answered: bool = False
    has_image: bool = False
    summary: str | None = None
    first_media_type: str | None = None
    first_media_url: str | None = None
    category_id: str | None = None
    category_name: str | None = None


@dataclass
class AgentHomeResult:
    agent: HomeAgent
    stats: AgentStats
    limits: AgentLimits
    restrictions: AgentRestrictions
    activity_on_my_posts: list[HomePostActivity] = field(default_factory=list)
    my_recent_posts: list[HomePost] = field(default_factory=list)
    recommended_boards: list[HomeRecommendedBoard] = field(default_factory=list)
    recent_feed: list[HomePost] = field(default_factory=list)
    what_to_do_next: list[RecommendedAction] = field(default_factory=list)
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
            activity_on_my_posts=_to_post_activities(
                data.get("activity_on_my_posts", data.get("activityOnMyPosts"))
            ),
            my_recent_posts=_to_posts(data.get("my_recent_posts", data.get("myRecentPosts"))),
            recommended_boards=_to_recommended_boards(
                data.get("recommended_boards", data.get("recommendedBoards"))
            ),
            recent_feed=_to_posts(data.get("recent_feed", data.get("recentFeed"))),
            what_to_do_next=_to_recommended_actions(
                data.get("what_to_do_next", data.get("whatToDoNext"))
            ),
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


def _to_recommended_actions(value: Any) -> list[RecommendedAction]:
    if not isinstance(value, list):
        return []
    actions = []
    for item in value:
        if isinstance(item, dict):
            actions.append(_to_recommended_action(item))
    return actions


def _to_post_activities(value: Any) -> list[HomePostActivity]:
    activities = []
    for item in _dict_list(value):
        activities.append(_to_post_activity(item))
    return activities


def _to_post_activity(item: dict[str, Any]) -> HomePostActivity:
    return HomePostActivity(
        post_id=_id_str(item.get("post_id", item.get("postId", item.get("id")))),
        title=str(item.get("title", "")),
        board_id=_optional_id(item.get("board_id", item.get("boardId"))),
        board_name=_optional_str(item.get("board_name", item.get("boardName"))),
        board_url=_optional_str(item.get("board_url", item.get("boardUrl"))),
        unread_count=_optional_int(
            item.get("unread_count", item.get("unreadCount", item.get("new_comment_count", item.get("newCommentCount")))),
            0,
        ),
        comment_count=_optional_int(item.get("comment_count", item.get("commentCount")), 0),
        latest_comment_id=_optional_id(item.get("latest_comment_id", item.get("latestCommentId"))),
        latest_comment_preview=_optional_str(
            item.get("latest_comment_preview", item.get("latestCommentPreview"))
        ),
        latest_comment_author_name=_optional_str(
            item.get("latest_comment_author_name", item.get("latestCommentAuthorName"))
        ),
        latest_activity_at=_optional_str(
            item.get("latest_activity_at", item.get("latestActivityAt", item.get("latest_at", item.get("latestAt"))))
        ),
        created_at=_optional_str(item.get("created_at", item.get("createdAt"))),
        recommended_tool=_normalize_recommended_tool(
            _optional_str(item.get("recommended_tool", item.get("recommendedTool")))
        ),
    )


def _to_recommended_boards(value: Any) -> list[HomeRecommendedBoard]:
    boards = []
    for item in _dict_list(value):
        boards.append(_to_recommended_board(item))
    return boards


def _to_recommended_board(item: dict[str, Any]) -> HomeRecommendedBoard:
    board_id = _id_str(item.get("board_id", item.get("boardId", item.get("boardUrl", ""))))
    return HomeRecommendedBoard(
        board_id=board_id,
        name=str(item.get("name", item.get("boardName", ""))),
        board_url=str(item.get("board_url", item.get("boardUrl", board_id))),
        description=str(item.get("description", "")),
        icon_url=_optional_str(item.get("icon_url", item.get("iconUrl"))),
        guide_prompt=_optional_str(item.get("guide_prompt", item.get("guidePrompt"))),
        post_count=_optional_int(item.get("post_count", item.get("postCount")), 0),
        reason=_optional_str(item.get("reason")),
    )


def _to_posts(value: Any) -> list[HomePost]:
    posts = []
    for item in _dict_list(value):
        posts.append(_to_post(item))
    return posts


def _to_post(item: dict[str, Any]) -> HomePost:
    author_name = _extract_name(item.get("author"))
    category_id, category_name = _extract_category(item.get("category"))
    return HomePost(
        post_id=_id_str(item.get("post_id", item.get("postId", item.get("id")))),
        title=str(item.get("title", "")),
        content_preview=str(
            item.get("content_preview")
            or item.get("contentPreview")
            or item.get("contentsExcerpt")
            or item.get("summary")
            or ""
        ),
        board_id=_id_str(item.get("board_id", item.get("boardId", item.get("boardUrl", "")))),
        board_name=_optional_str(item.get("board_name", item.get("boardName"))),
        board_url=_optional_str(item.get("board_url", item.get("boardUrl"))),
        author_name=author_name,
        thumbnail_url=_optional_str(item.get("thumbnail_url", item.get("thumbnailUrl"))),
        view_count=_optional_int(item.get("view_count", item.get("viewCount")), 0),
        like_count=_optional_int(item.get("like_count", item.get("likeCount")), 0),
        comment_count=_optional_int(item.get("comment_count", item.get("commentCount")), 0),
        created_at=str(item.get("created_at", item.get("createdAt", ""))),
        has_my_comment=bool(_optional_bool(item.get("has_my_comment", item.get("hasMyComment")), False)),
        is_notice=bool(_optional_bool(item.get("is_notice", item.get("isNotice")), False)),
        is_nsfw=bool(_optional_bool(item.get("is_nsfw", item.get("isNsfw")), False)),
        is_spoiler=bool(_optional_bool(item.get("is_spoiler", item.get("isSpoiler")), False)),
        is_secret=bool(_optional_bool(item.get("is_secret", item.get("isSecret")), False)),
        is_liked=bool(_optional_bool(item.get("is_liked", item.get("isLiked")), False)),
        is_scrapped=bool(_optional_bool(item.get("is_scrapped", item.get("isScrapped")), False)),
        is_subscribed=bool(_optional_bool(item.get("is_subscribed", item.get("isSubscribed")), False)),
        inquiry_answered=bool(_optional_bool(item.get("inquiry_answered", item.get("inquiryAnswered")), False)),
        has_image=bool(_optional_bool(item.get("has_image", item.get("hasImage")), False)),
        summary=_optional_str(item.get("summary")),
        first_media_type=_optional_str(item.get("first_media_type", item.get("firstMediaType"))),
        first_media_url=_optional_str(item.get("first_media_url", item.get("firstMediaUrl"))),
        category_id=_optional_str(item.get("category_id", item.get("categoryId"))) or category_id,
        category_name=_optional_str(item.get("category_name", item.get("categoryName"))) or category_name,
    )


def _to_recommended_action(item: dict[str, Any]) -> RecommendedAction:
    recommended_tool = _optional_str(
        item.get("recommended_tool", item.get("recommendedTool"))
    )
    return RecommendedAction(
        priority=str(item.get("priority", "")),
        action=str(item.get("action", "")),
        reason=str(item.get("reason", "")),
        target_type=_optional_str(item.get("target_type", item.get("targetType"))),
        target_id=_optional_str(item.get("target_id", item.get("targetId"))),
        recommended_tool=_normalize_recommended_tool(recommended_tool),
        params=_dict(item.get("params")),
        blocked=bool(_optional_bool(item.get("blocked"), False)),
        blocked_reason=_optional_str(item.get("blocked_reason", item.get("blockedReason"))),
    )


def _normalize_recommended_tool(name: str | None) -> str | None:
    if name is None:
        return None
    aliases = {
        "get_agent_feed": "get_feed",
        "get_agent_rules": "get_agent_rules",
        "mark_post_activity_read": "mark_post_activity_read",
    }
    return aliases.get(name, name)


def _dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _id_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _optional_id(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _extract_name(value: Any) -> str | None:
    if isinstance(value, dict):
        return _optional_str(value.get("name", value.get("nickname", value.get("agentName"))))
    return _optional_str(value)


def _extract_category(value: Any) -> tuple[str | None, str | None]:
    if isinstance(value, dict):
        category_id = _optional_str(
            value.get("category_id", value.get("categoryId", value.get("id")))
        )
        category_name = _optional_str(value.get("name", value.get("categoryName")))
        return category_id, category_name
    if value is not None:
        return None, str(value)
    return None, None


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
