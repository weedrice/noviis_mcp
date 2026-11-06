"""Validate comment state and counters without coercing malformed wire values."""
from typing import Any

from exceptions import BackendContractError
from tools.status_contract import policy_field


COMMENT_STATUSES = frozenset({"ACTIVE", "DELETED", "BLINDED", "BLOCKED_AUTHOR"})


def read_comment_metadata(item: dict[str, Any], *, nested_reply_count: int) -> dict[str, Any]:
    deleted = policy_field(item, "is_deleted", bool, label="comment", required=False)
    status = policy_field(item, "status", str, label="comment", required=False)
    if status is None:
        status = "DELETED" if deleted else "ACTIVE"
    else:
        status = status.upper()
        if status not in COMMENT_STATUSES:
            raise BackendContractError("Comment status is not recognized.")
    if deleted is not None and deleted != (status == "DELETED"):
        raise BackendContractError("Comment status and deletion flag disagree.")

    count = policy_field(item, "reply_count", int, label="comment", required=False)
    has_replies = policy_field(item, "has_replies", bool, label="comment", required=False)
    # Missing legacy counters may describe a partially embedded tree. Only
    # compare an explicit total with the explicit flag, not with omitted pages.
    if count is not None and has_replies is not None and has_replies != (count > 0):
        raise BackendContractError("Comment reply count and has_replies disagree.")
    if count is not None and count < nested_reply_count:
        raise BackendContractError("Comment reply count is smaller than its embedded reply collection.")
    if has_replies is False and nested_reply_count:
        raise BackendContractError("Comment has embedded replies but has_replies is false.")
    reply_count = nested_reply_count if count is None else count
    return {
        "status": status,
        "is_deleted": status == "DELETED",
        "reply_count": reply_count,
        "has_replies": reply_count > 0 if has_replies is None else has_replies,
        "depth": policy_field(item, "depth", int, label="comment", required=False) or 0,
        "like_count": policy_field(item, "like_count", int, label="comment", required=False) or 0,
    }
