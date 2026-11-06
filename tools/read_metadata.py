"""Preserve read metadata without inventing identity or absent post facts."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from exceptions import BackendContractError
from tools.response_contract import require_positive_long_id
from tools.status_contract import policy_field, policy_object


@dataclass
class ReadAuthor:
    user_id: int | None = None
    agent_id: int | None = None
    author_type: str | None = None
    display_name: str | None = None


POST_COUNT_FIELDS = ("view_count", "like_count", "comment_count")
POST_FLAG_FIELDS = (
    "has_my_comment", "is_notice", "is_nsfw", "is_spoiler", "is_secret",
    "is_liked", "is_scrapped", "is_subscribed", "inquiry_answered", "has_image",
)
POST_TEXT_FIELDS = (
    "board_name", "board_url", "board_icon_url", "thumbnail_url", "created_at",
    "summary", "first_media_type", "first_media_url",
)


def read_optional_metadata(item: dict[str, Any], name: str, kind: type) -> Any:
    return policy_field(item, name, kind, label="post", required=False, nullable=True)


def read_post_metadata(item: dict[str, Any]) -> dict[str, Any]:
    return {
        **{name: read_optional_metadata(item, name, int) for name in POST_COUNT_FIELDS},
        **{name: read_optional_metadata(item, name, bool) for name in POST_FLAG_FIELDS},
        **{name: read_optional_metadata(item, name, str) for name in POST_TEXT_FIELDS},
    }


def read_author(value: Any) -> ReadAuthor | None:
    if value is None:
        return None
    # Legacy responses sometimes provided only a display name, never an identity.
    if isinstance(value, str):
        return ReadAuthor(display_name=value)
    value = policy_object(value, "author")
    user_id = require_positive_long_id(value, "userId", "user_id", nullable=True)
    agent_id = require_positive_long_id(value, "agentId", "agent_id", nullable=True)
    author_type = policy_field(value, "author_type", str, label="author", required=False, nullable=True)
    if author_type is not None and author_type not in {"USER", "AGENT"}:
        raise BackendContractError("author.author_type must be USER or AGENT.")
    if author_type == "USER" and agent_id is not None:
        raise BackendContractError("USER author must not have an agent ID.")
    if author_type == "AGENT" and any(key in value for key in ("agentId", "agent_id")) and agent_id is None:
        raise BackendContractError("AGENT author must not have a null agent ID.")
    if any(key in value for key in ("displayName", "display_name")):
        display_name = policy_field(value, "display_name", str, label="author", required=False, nullable=True)
    else:
        display_name = None
        for key in ("name", "nickname", "agentName"):
            if key in value:
                display_name = policy_field(value, key, str, label="author", nullable=True)
                break
    return ReadAuthor(user_id=user_id, agent_id=agent_id, author_type=author_type, display_name=display_name)
