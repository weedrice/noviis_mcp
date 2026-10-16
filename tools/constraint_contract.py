from __future__ import annotations

from typing import Any

from exceptions import BackendContractError


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
    return result
