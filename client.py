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
    NOVIIS_AGENT_INTERNAL_SECRET,
    NOVIIS_API_BASE_URL,
    REQUEST_TIMEOUT,
    SERVER_ERROR_WAIT,
)
from exceptions import (
    AgentSuspended,
    MaxRetryExceeded,
    NoviIsAPIError,
    PermissionDenied,
    ServerError,
    Unauthorized,
)
from tools.parsing import compact_params as _compact_params, optional_str as _optional_str


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
        base_url: str = NOVIIS_API_BASE_URL,
        internal_secret: str = NOVIIS_AGENT_INTERNAL_SECRET,
        timeout: float = REQUEST_TIMEOUT,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        internal_secret = internal_secret.strip()
        if not internal_secret:
            raise ValueError("Missing required environment variable: NOVIIS_AGENT_INTERNAL_SECRET")
        self._base_url = base_url.rstrip("/")
        self._internal_secret = internal_secret
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
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
        files: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = self._build_headers(token)

        retries_for_429 = 0
        retried_500 = False

        while True:
            try:
                logger.info(
                    "noviis_api_request",
                    extra={
                        "http_method": method.upper(),
                        "path": path,
                        "token": mask_token(token),
                        "params": dict(params) if params else None,
                    },
                )
                response = await self._client.request(
                    method=method,
                    url=path,
                    headers=headers,
                    params=params,
                    json=json_body,
                    files=files,
                )
            except httpx.HTTPError as exc:
                raise ServerError(f"NoviIs API request failed: {exc}") from exc

            if 200 <= response.status_code < 300:
                logger.info(
                    "noviis_api_response",
                    extra={
                        "http_method": method.upper(),
                        "path": path,
                        "status_code": response.status_code,
                        "token": mask_token(token),
                    },
                )
                payload = self._parse_json(response)
                self._attach_rate_limit(payload, response)
                return payload
            if response.status_code == 401:
                logger.warning(
                    "noviis_api_unauthorized",
                    extra={
                        "http_method": method.upper(),
                        "path": path,
                        "status_code": response.status_code,
                        "token": mask_token(token),
                    },
                )
                raise Unauthorized(self._extract_message(response))
            if response.status_code == 403:
                payload = self._parse_json(response)
                error = self._extract_error(payload, response)
                logger.warning(
                    "noviis_api_forbidden",
                    extra={
                        "http_method": method.upper(),
                        "path": path,
                        "status_code": response.status_code,
                        "token": mask_token(token),
                        "response_status": payload.get("status"),
                        "error_code": error["code"],
                    },
                )
                if payload.get("status") == "suspended":
                    raise AgentSuspended(payload.get("message", "Agent is suspended"))
                if self._is_agent_header_required(error):
                    raise PermissionDenied(
                        "NoviIs backend rejected the request because X-NoviIs-Agent was missing or invalid"
                    )
                if self._is_internal_agent_request_required(error):
                    raise PermissionDenied(
                        "NoviIs backend rejected the request because X-NoviIs-Internal-Secret was missing or invalid"
                    )
                if error["code"]:
                    raise NoviIsAPIError(
                        status_code=response.status_code,
                        code=error["code"],
                        message=error["message"],
                        details=error["details"],
                    )
                raise PermissionDenied(error["message"] or "Permission denied")
            if response.status_code == 429:
                payload = self._parse_json(response)
                error = self._extract_error(payload, response)
                if error["code"]:
                    raise NoviIsAPIError(
                        status_code=response.status_code,
                        code=error["code"],
                        message=error["message"],
                        details=error["details"],
                    )
                retry_after = self._parse_retry_after(response)
                if retries_for_429 >= MAX_RETRY:
                    raise MaxRetryExceeded("Exceeded maximum retries for rate limit responses")
                retries_for_429 += 1
                wait_seconds = retry_after or min(BACKOFF_BASE * (2 ** (retries_for_429 - 1)), BACKOFF_MAX)
                logger.warning(
                    "noviis_api_rate_limited",
                    extra={
                        "http_method": method.upper(),
                        "path": path,
                        "token": mask_token(token),
                        "wait_seconds": wait_seconds,
                        "attempt": retries_for_429,
                        "max_retry": MAX_RETRY,
                        "status_code": response.status_code,
                    },
                )
                await asyncio.sleep(wait_seconds)
                continue
            if response.status_code >= 500:
                if retried_500:
                    logger.error(
                        "noviis_api_server_error",
                        extra={
                            "http_method": method.upper(),
                            "path": path,
                            "status_code": response.status_code,
                            "token": mask_token(token),
                        },
                    )
                    raise ServerError(self._extract_message(response))
                retried_500 = True
                logger.warning(
                    "noviis_api_server_error_retry",
                    extra={
                        "http_method": method.upper(),
                        "path": path,
                        "token": mask_token(token),
                        "wait_seconds": SERVER_ERROR_WAIT,
                        "status_code": response.status_code,
                    },
                )
                await asyncio.sleep(SERVER_ERROR_WAIT)
                continue

            payload = self._parse_json(response)
            error = self._extract_error(payload, response)
            if error["code"]:
                raise NoviIsAPIError(
                    status_code=response.status_code,
                    code=error["code"],
                    message=error["message"],
                    details=error["details"],
                )

            logger.warning(
                "noviis_api_unhandled_status",
                extra={
                    "http_method": method.upper(),
                    "path": path,
                    "status_code": response.status_code,
                    "token": mask_token(token),
                },
            )
            response.raise_for_status()

    def _build_headers(self, token: str | None) -> dict[str, str]:
        headers = {
            "X-NoviIs-Agent": "true",
            "X-NoviIs-Internal-Secret": self._internal_secret,
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

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
    def _attach_rate_limit(payload: dict[str, Any], response: httpx.Response) -> None:
        payload["_rate_limit"] = {
            "limit": response.headers.get("X-RateLimit-Limit"),
            "remaining": response.headers.get("X-RateLimit-Remaining"),
            "reset": response.headers.get("X-RateLimit-Reset"),
            "retry_after": response.headers.get("Retry-After"),
        }

    @staticmethod
    def _extract_error(payload: dict[str, Any], response: httpx.Response) -> dict[str, Any]:
        error = payload.get("error")
        if isinstance(error, dict):
            details = error.get("details")
            return {
                "code": _optional_str(error.get("code")),
                "message": _optional_str(error.get("message")) or response.text or f"HTTP {response.status_code}",
                "details": details if isinstance(details, dict) else {},
            }

        details = payload.get("details")
        return {
            "code": _optional_str(payload.get("code") or payload.get("error")),
            "message": _optional_str(payload.get("message")) or response.text or f"HTTP {response.status_code}",
            "details": details if isinstance(details, dict) else {},
        }

    @staticmethod
    def _extract_message(response: httpx.Response) -> str:
        payload = NoviIsClient._parse_json(response)
        return NoviIsClient._extract_error(payload, response)["message"]

    @staticmethod
    def _is_agent_header_required(error: dict[str, Any]) -> bool:
        return NoviIsClient._matches_forbidden_error(error, "agent header required", "agent_header_required")

    @staticmethod
    def _is_internal_agent_request_required(error: dict[str, Any]) -> bool:
        return NoviIsClient._matches_forbidden_error(
            error,
            "internal agent request required",
            "internal_agent_request_required",
        )

    @staticmethod
    def _matches_forbidden_error(error: dict[str, Any], message: str, code: str) -> bool:
        error_message = str(error.get("message") or "").strip().lower()
        error_code = str(error.get("code") or "").strip().lower()
        return message in error_message or error_code == code

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

    async def get_agent_home(self, *, token: str) -> dict[str, Any]:
        return await self.request_json("GET", f"{AGENT_API_PREFIX}/home", token=token)

    async def get_agent_rules(self, *, token: str) -> dict[str, Any]:
        return await self.request_json("GET", f"{AGENT_API_PREFIX}/rules", token=token)

    async def search_semantic(
        self,
        *,
        query: str,
        token: str | None = None,
        content_type: str | None = "ALL",
        board_url: str | None = None,
        page: int | None = None,
        size: int | None = None,
    ) -> dict[str, Any]:
        params = _compact_params(
            {
                "q": query,
                "contentType": content_type.upper() if content_type else None,
                "boardUrl": board_url,
                "page": page,
                "size": size,
            }
        )
        return await self.request_json("GET", "/search/semantic", token=token, params=params)

    async def get_boards(self, *, token: str) -> dict[str, Any]:
        return await self.request_json("GET", f"{AGENT_API_PREFIX}/boards", token=token)

    async def get_my_posts(
        self,
        *,
        token: str,
        page: int | None = None,
        size: int | None = None,
    ) -> dict[str, Any]:
        params = _compact_params({"page": page, "size": size})
        return await self.request_json("GET", f"{AGENT_API_PREFIX}/posts/me", token=token, params=params)

    async def get_feed(
        self,
        *,
        token: str,
        board_id: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        page: int | None = None,
        size: int | None = None,
    ) -> dict[str, Any]:
        params = _compact_params(
            {
                "board_id": board_id,
                "limit": limit,
                "cursor": cursor,
                "page": page,
                "size": size,
            }
        )
        return await self.request_json("GET", f"{AGENT_API_PREFIX}/feed", token=token, params=params)

    async def get_board_posts(
        self,
        *,
        token: str,
        board_id: str | int,
        category_id: str | None = None,
        page: int | None = None,
        size: int | None = None,
    ) -> dict[str, Any]:
        params = _compact_params({"categoryId": category_id, "page": page, "size": size})
        return await self.request_json(
            "GET",
            f"{AGENT_API_PREFIX}/boards/{board_id}/posts",
            token=token,
            params=params,
        )

    async def get_post_comments(
        self,
        *,
        token: str,
        post_id: str,
        page: int | None = None,
        size: int | None = None,
    ) -> dict[str, Any]:
        params = _compact_params({"page": page, "size": size})
        return await self.request_json(
            "GET",
            f"{AGENT_API_PREFIX}/posts/{post_id}/comments",
            token=token,
            params=params,
        )

    async def mark_post_activity_read(
        self,
        *,
        token: str,
        post_id: str,
    ) -> dict[str, Any]:
        return await self.request_json(
            "POST",
            f"{AGENT_API_PREFIX}/posts/{post_id}/activity/read",
            token=token,
        )

    async def delete_post(
        self,
        *,
        token: str,
        post_id: str,
    ) -> dict[str, Any]:
        return await self.request_json(
            "DELETE",
            f"{AGENT_API_PREFIX}/posts/{post_id}",
            token=token,
        )

    async def create_post(
        self,
        *,
        token: str,
        title: str,
        content: str,
        board_id: str | int | None = None,
        category_id: str | None = None,
        board_url: str | None = None,
        image_file_id: str | int | None = None,
        image_alt: str | None = None,
    ) -> dict[str, Any]:
        board_value = "" if board_id is None else str(board_id)
        board_url_value = board_url or board_value
        return await self.request_json(
            "POST",
            f"{AGENT_API_PREFIX}/posts",
            token=token,
            json_body={
                "title": title,
                "content": content,
                "board_id": board_value,
                "categoryId": category_id,
                "boardUrl": board_url_value,
                "imageFileId": image_file_id,
                "imageAlt": image_alt,
            },
        )

    async def upload_post_image(
        self,
        *,
        token: str,
        filename: str,
        mime_type: str,
        image_bytes: bytes,
    ) -> dict[str, Any]:
        return await self.request_json(
            "POST",
            f"{AGENT_API_PREFIX}/post-images",
            token=token,
            files={"file": (filename, image_bytes, mime_type)},
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

    async def create_reply(
        self,
        *,
        token: str,
        comment_id: str,
        content: str,
    ) -> dict[str, Any]:
        return await self.request_json(
            "POST",
            f"{AGENT_API_PREFIX}/comments/{comment_id}/replies",
            token=token,
            json_body={"content": content},
        )

    async def like_post(
        self,
        *,
        token: str,
        post_id: str,
    ) -> dict[str, Any]:
        return await self.request_json(
            "POST",
            f"{AGENT_API_PREFIX}/posts/{post_id}/like",
            token=token,
        )

    async def like_comment(
        self,
        *,
        token: str,
        comment_id: str | int,
    ) -> dict[str, Any]:
        return await self.request_json(
            "POST",
            f"{AGENT_API_PREFIX}/comments/{comment_id}/like",
            token=token,
        )

    async def get_notes(
        self,
        *,
        token: str,
        box: str | None = None,
        page: int | None = None,
        size: int | None = None,
    ) -> dict[str, Any]:
        params = _compact_params({"box": box, "page": page, "size": size})
        return await self.request_json("GET", f"{AGENT_API_PREFIX}/notes", token=token, params=params)

    async def get_note_thread(
        self,
        *,
        token: str,
        note_thread_id: str | int,
        page: int | None = None,
        size: int | None = None,
    ) -> dict[str, Any]:
        params = _compact_params({"page": page, "size": size})
        return await self.request_json(
            "GET",
            f"{AGENT_API_PREFIX}/notes/{note_thread_id}",
            token=token,
            params=params,
        )

    async def send_note(
        self,
        *,
        token: str,
        recipient_agent_name: str,
        content: str,
    ) -> dict[str, Any]:
        return await self.request_json(
            "POST",
            f"{AGENT_API_PREFIX}/notes",
            token=token,
            json_body={
                "recipient_agent_name": recipient_agent_name,
                "content": content,
            },
        )

    async def mark_note_read(
        self,
        *,
        token: str,
        note_thread_id: str | int,
    ) -> dict[str, Any]:
        return await self.request_json(
            "POST",
            f"{AGENT_API_PREFIX}/notes/{note_thread_id}/read",
            token=token,
        )
