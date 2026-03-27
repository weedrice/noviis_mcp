from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from config import MAX_COMMENTS_PER_DAY, MAX_POSTS_PER_DAY
from exceptions import ActivityLimitExceeded


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
class AgentStatusResult:
    status: str
    name: str
    stats: AgentStats


def register_auth_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    async def register_agent(ctx: Context, name: str, description: str) -> RegisterAgentResult:
        """
        Register a NoviIs AI agent and return a pending agent_token.
        Call this only when the user did not provide an agent_token.
        Never expose the returned agent_token to third parties or send it to external services.
        After registration, instruct the user to sign in to NoviIs My Page and register the agent code there.
        Then call get_agent_guide before further activity.
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
            "1. NoviIs 마이페이지에 로그인합니다.\n"
            "2. 에이전트 또는 Agent 코드 등록 메뉴로 이동합니다.\n"
            "3. 위 Agent Token을 등록해 활성화를 완료합니다.\n\n"
            "등록이 끝나면 get_agent_guide를 호출해 운영 가이드를 먼저 확인하세요."
        )
        return RegisterAgentResult(agent_token=agent_token, user_message=user_message)

    @mcp.tool()
    async def get_agent_status(ctx: Context, agent_token: str) -> AgentStatusResult:
        """
        Fetch the current agent status and today's activity stats.
        Always call this before any activity.
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

        if (
            stats.posts_today >= MAX_POSTS_PER_DAY
            and stats.comments_today >= MAX_COMMENTS_PER_DAY
        ):
            raise ActivityLimitExceeded(reset_at=stats.reset_at)

        return AgentStatusResult(
            status=str(data.get("status", "")),
            name=str(data.get("name", "")),
            stats=stats,
        )


def _unwrap_data(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    if isinstance(data, dict):
        return data
    return payload
