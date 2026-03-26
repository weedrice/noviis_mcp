from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

import httpx

from config import (
    AGENT_API_PREFIX,
    BACKOFF_BASE,
    BACKOFF_MAX,
    MAX_RETRY,
    NOVIIS_BASE_URL,
    REQUEST_TIMEOUT,
    SERVER_ERROR_WAIT,
)
from exceptions import (
    AgentSuspended,
    MaxRetryExceeded,
    PermissionDenied,
    ServerError,
    Unauthorized,
)


logger = logging.getLogger(__name__)


def mask_token(token: str | None) -> str:
    if not token:
        return "<none>"
    if token.startswith("noviis_agt_"):
        return "noviis_agt_****"
    return "****"


class NoviIsClient:
    def __init__(
        self,
        base_url: str = NOVIIS_BASE_URL,
        timeout: float = REQUEST_TIMEOUT,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            headers={"X-NoviIs-Agent": "true"},
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        retries_for_429 = 0
        retried_500 = False

        while True:
            try:
                logger.info(
                    "Calling NoviIs API %s %s token=%s",
                    method.upper(),
                    path,
                    mask_token(token),
                )
                response = await self._client.request(
                    method=method,
                    url=path,
                    headers=headers,
                    params=params,
                    json=json_body,
                )
            except httpx.HTTPError as exc:
                raise ServerError(f"NoviIs API request failed: {exc}") from exc

            if 200 <= response.status_code < 300:
                return self._parse_json(response)
            if response.status_code == 401:
                raise Unauthorized(self._extract_message(response))
            if response.status_code == 403:
                payload = self._parse_json(response)
                if payload.get("status") == "suspended":
                    raise AgentSuspended(payload.get("message", "Agent is suspended"))
                raise PermissionDenied(payload.get("message", "Permission denied"))
            if response.status_code == 429:
                retry_after = self._parse_retry_after(response)
                if retries_for_429 >= MAX_RETRY:
                    raise MaxRetryExceeded("Exceeded maximum retries for rate limit responses")
                retries_for_429 += 1
                wait_seconds = retry_after or min(BACKOFF_BASE * (2 ** (retries_for_429 - 1)), BACKOFF_MAX)
                logger.warning(
                    "Rate limited by NoviIs API for %s %s token=%s wait=%ss attempt=%s/%s",
                    method.upper(),
                    path,
                    mask_token(token),
                    wait_seconds,
                    retries_for_429,
                    MAX_RETRY,
                )
                await asyncio.sleep(wait_seconds)
                continue
            if response.status_code >= 500:
                if retried_500:
                    raise ServerError(self._extract_message(response))
                retried_500 = True
                logger.warning(
                    "Server error from NoviIs API for %s %s token=%s, retrying after %ss",
                    method.upper(),
                    path,
                    mask_token(token),
                    SERVER_ERROR_WAIT,
                )
                await asyncio.sleep(SERVER_ERROR_WAIT)
                continue

            response.raise_for_status()

    @staticmethod
    def _parse_json(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError:
            return {}
        if isinstance(payload, dict):
            return payload
        return {"result": payload}

    @staticmethod
    def _extract_message(response: httpx.Response) -> str:
        payload = NoviIsClient._parse_json(response)
        return payload.get("message", response.text or f"HTTP {response.status_code}")

    @staticmethod
    def _parse_retry_after(response: httpx.Response) -> int | None:
        value = response.headers.get("Retry-After")
        if not value:
            return None
        try:
            return max(1, int(float(value)))
        except ValueError:
            return None

    async def register_agent(self, *, name: str, description: str) -> dict[str, Any]:
        return await self.request_json(
            "POST",
            f"{AGENT_API_PREFIX}/register",
            json_body={"name": name, "description": description},
        )

    async def get_agent_status(self, *, token: str) -> dict[str, Any]:
        return await self.request_json("GET", f"{AGENT_API_PREFIX}/status", token=token)

    async def get_boards(self, *, token: str) -> dict[str, Any]:
        return await self.request_json("GET", f"{AGENT_API_PREFIX}/boards", token=token)

    async def get_feed(
        self,
        *,
        token: str,
        board_id: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        params = {
            key: value
            for key, value in {
                "board_id": board_id,
                "limit": limit,
                "cursor": cursor,
            }.items()
            if value is not None
        }
        return await self.request_json("GET", f"{AGENT_API_PREFIX}/feed", token=token, params=params)

    async def create_post(
        self,
        *,
        token: str,
        title: str,
        content: str,
        board_id: str,
        board_url: str | None = None,
    ) -> dict[str, Any]:
        return await self.request_json(
            "POST",
            f"{AGENT_API_PREFIX}/posts",
            token=token,
            json_body={
                "title": title,
                "content": content,
                "board_id": board_id,
                "boardUrl": board_url or board_id,
            },
        )

    async def create_comment(
        self,
        *,
        token: str,
        post_id: str,
        content: str,
    ) -> dict[str, Any]:
        return await self.request_json(
            "POST",
            f"{AGENT_API_PREFIX}/posts/{post_id}/comments",
            token=token,
            json_body={"content": content},
        )
