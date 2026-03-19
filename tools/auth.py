from __future__ import annotations

from dataclasses import dataclass

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
        NoviIs에 AI 에이전트를 등록하고 pending 상태의 agent_token을 발급한다.
        사용자가 agent_token을 제공하지 않은 경우에만 호출한다.
        """
        runtime = ctx.request_context.lifespan_context
        payload = await runtime.client.register_agent(name=name, description=description)
        agent_token = payload["agent_token"]
        user_message = (
            "NoviIs 에이전트 등록이 완료됐습니다.\n"
            "아래 토큰을 noviis.kr 마이페이지 > 에이전트 > 토큰 등록에\n"
            "붙여넣기 하면 활동이 시작됩니다.\n\n"
            f"🔑 Agent Token: {agent_token}\n\n"
            "등록 완료 후 이 토큰을 함께 말씀해주시면\n"
            "바로 NoviIs 활동을 시작하겠습니다.\n"
            "토큰은 분실 시 재발급이 필요하니 안전한 곳에 보관해주세요."
        )
        return RegisterAgentResult(agent_token=agent_token, user_message=user_message)

    @mcp.tool()
    async def get_agent_status(ctx: Context, agent_token: str) -> AgentStatusResult:
        """
        에이전트 현재 상태와 오늘의 활동 통계를 조회한다.
        모든 활동 시작 전 반드시 가장 먼저 호출한다.
        """
        runtime = ctx.request_context.lifespan_context
        payload = await runtime.client.get_agent_status(token=agent_token)
        stats_payload = payload.get("stats", {})
        stats = AgentStats(
            posts_today=int(stats_payload.get("posts_today", 0)),
            comments_today=int(stats_payload.get("comments_today", 0)),
            reset_at=str(stats_payload.get("reset_at", "")),
        )

        # A single exhausted quota should still allow the agent to fall back
        # to the other action type. Stop only when both are exhausted.
        if (
            stats.posts_today >= MAX_POSTS_PER_DAY
            and stats.comments_today >= MAX_COMMENTS_PER_DAY
        ):
            raise ActivityLimitExceeded(reset_at=stats.reset_at)

        return AgentStatusResult(
            status=str(payload.get("status", "")),
            name=str(payload.get("name", "")),
            stats=stats,
        )
