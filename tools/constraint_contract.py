from __future__ import annotations

from typing import Any

from exceptions import BackendContractError
from tools.status_contract import policy_field, validate_policy_permission, validate_policy_quota


def validate_hard_constraints(value: Any, *, post_only: bool = False) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BackendContractError("hard_constraints must be an object.")
    result = dict(value)
    fields = {
        "suspended": ("suspended",),
        "can_create_post": ("can_create_post", "canCreatePost", "can_post", "canPost"),
        "posts_remaining": ("posts_remaining", "postsRemaining"),
    }
    if not post_only:
        fields.update({"can_create_comment": ("can_create_comment", "canCreateComment", "can_comment", "canComment"),
                       "comments_remaining": ("comments_remaining", "commentsRemaining")})
    for name, aliases in fields.items():
        present = [value[key] for key in aliases if key in value]
        expected = int if name.endswith("_remaining") else bool
        if not present or any(type(item) is not expected for item in present) or any(item != present[0] for item in present):
            raise BackendContractError(f"hard_constraints.{name} is missing, invalid, or conflicting.")
        if expected is int and present[0] < 0:
            raise BackendContractError(f"hard_constraints.{name} must be nonnegative.")
        result[name] = present[0]
    for name, expected in (("can_send_note", bool), ("notes_remaining", int), ("note_daily_limit", int)):
        result[name] = policy_field(value, name, expected, label="hard_constraints", required=False)
    result["next_note_allowed_at"] = policy_field(value, "next_note_allowed_at", str, label="hard_constraints", required=False, nullable=True, timestamp=True)
    for permission, quota in (("can_create_post", "posts_remaining"), ("can_send_note", "notes_remaining")):
        validate_policy_permission(label=f"hard_constraints.{permission}", suspended=result["suspended"], allowed=result[permission], remaining=result[quota])
    if not post_only:
        validate_policy_permission(label="hard_constraints.can_create_comment", suspended=result["suspended"], allowed=result["can_create_comment"], remaining=result["comments_remaining"])
    validate_policy_quota(result["note_daily_limit"], result["notes_remaining"], "hard_constraints.notes_remaining")
    return result
