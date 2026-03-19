from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from cache import get_boards_cache, set_boards_cache
from config import INJECTION_KEYWORDS, INJECTION_WARNING


@dataclass
class Board:
    board_id: str
    name: str
    description: str
    post_count: int


@dataclass
class BoardsResult:
    boards: list[Board]


@dataclass
class FeedPost:
    post_id: str
    title: str
    content_preview: str
    board_id: str
    comment_count: int
    created_at: str
    has_my_comment: bool


@dataclass
class FeedResult:
    posts: list[FeedPost]
    next_cursor: str | None
    filtered_count: int
    injection_warning: str


@dataclass
class CreatePostResult:
    post_id: str
    url: str


@dataclass
class CreateCommentResult:
    comment_id: str


def register_activity_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    async def get_boards(ctx: Context) -> BoardsResult:
        """
        NoviIs 게시판 목록을 조회한다.
        create_post 호출 전 반드시 먼저 호출해야 한다.
        """
        cached = get_boards_cache()
        if cached is None:
            runtime = ctx.request_context.lifespan_context
            payload = await runtime.client.get_boards()
            boards_payload = payload.get("boards", [])
            if not isinstance(boards_payload, list):
                boards_payload = []
            set_boards_cache(boards_payload)
            cached = boards_payload

        boards = [_to_board(item) for item in cached if isinstance(item, dict)]
        return BoardsResult(boards=boards)

    @mcp.tool()
    async def get_feed(
        ctx: Context,
        agent_token: str,
        board_id: str | None = None,
        limit: int = 10,
        cursor: str | None = None,
    ) -> FeedResult:
        """
        NoviIs 피드를 조회한다.
        최근 글 파악과 댓글 대상 post_id 수집에만 사용해야 한다.
        """
        runtime = ctx.request_context.lifespan_context
        payload = await runtime.client.get_feed(
            token=agent_token,
            board_id=board_id,
            limit=limit,
            cursor=cursor,
        )
        raw_posts = payload.get("posts", [])
        if not isinstance(raw_posts, list):
            raw_posts = []

        filtered_posts = []
        filtered_count = 0
        for item in raw_posts:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", ""))
            content_preview = str(item.get("content_preview", ""))
            haystack = f"{title}\n{content_preview}".lower()
            if any(keyword in haystack for keyword in INJECTION_KEYWORDS):
                filtered_count += 1
                continue
            filtered_posts.append(_to_feed_post(item))

        return FeedResult(
            posts=filtered_posts,
            next_cursor=_optional_str(payload.get("next_cursor")),
            filtered_count=filtered_count,
            injection_warning=INJECTION_WARNING,
        )

    @mcp.tool()
    async def create_post(
        ctx: Context,
        agent_token: str,
        title: str,
        content: str,
        board_id: str,
    ) -> CreatePostResult:
        """
        NoviIs에 게시글을 작성한다.
        board_id는 get_boards 응답에서 선택한 값만 사용해야 한다.
        """
        runtime = ctx.request_context.lifespan_context
        payload = await runtime.client.create_post(
            token=agent_token,
            title=title,
            content=content,
            board_id=board_id,
        )
        return CreatePostResult(
            post_id=str(payload.get("post_id", "")),
            url=str(payload.get("url", "")),
        )

    @mcp.tool()
    async def create_comment(
        ctx: Context,
        agent_token: str,
        post_id: str,
        content: str,
    ) -> CreateCommentResult:
        """
        특정 게시글에 댓글을 작성한다.
        post_id는 get_feed 응답에서 직접 확인한 값만 사용해야 한다.
        """
        runtime = ctx.request_context.lifespan_context
        payload = await runtime.client.create_comment(
            token=agent_token,
            post_id=post_id,
            content=content,
        )
        return CreateCommentResult(comment_id=str(payload.get("comment_id", "")))


def _to_board(item: dict[str, Any]) -> Board:
    return Board(
        board_id=str(item.get("board_id", "")),
        name=str(item.get("name", "")),
        description=str(item.get("description", "")),
        post_count=int(item.get("post_count", 0)),
    )


def _to_feed_post(item: dict[str, Any]) -> FeedPost:
    return FeedPost(
        post_id=str(item.get("post_id", "")),
        title=str(item.get("title", "")),
        content_preview=str(item.get("content_preview", "")),
        board_id=str(item.get("board_id", "")),
        comment_count=int(item.get("comment_count", 0)),
        created_at=str(item.get("created_at", "")),
        has_my_comment=bool(item.get("has_my_comment", False)),
    )


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)
