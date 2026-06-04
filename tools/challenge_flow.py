from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from exceptions import ChallengeExpired, ChallengeFailed, ChallengeSuspended, ChallengeUsed


_Result = TypeVar("_Result")


def require_challenge_pair(challenge_id: str | None, answer: str | None) -> None:
    if (challenge_id is None) != (answer is None):
        raise ValueError("challenge_id and answer must be provided together")


def issue_challenge_result(
    *,
    runtime: Any,
    result_factory: Callable[..., _Result],
    owner_key: str,
    action: str,
    payload: dict[str, Any],
) -> _Result:
    return result_factory(
        status="challenge_required",
        challenge=runtime.challenge_manager.issue_challenge(
            owner_key=owner_key,
            action=action,
            payload=payload,
        ),
    )


def verify_or_reissue_challenge(
    *,
    runtime: Any,
    result_factory: Callable[..., _Result],
    owner_key: str,
    action: str,
    payload: dict[str, Any],
    challenge_id: str,
    answer: str,
) -> _Result | None:
    try:
        runtime.challenge_manager.verify_challenge(
            owner_key=owner_key,
            action=action,
            challenge_id=challenge_id,
            answer=answer,
            payload=payload,
        )
    except ChallengeSuspended as exc:
        return result_factory(
            status="challenge_suspended",
            error="challenge_suspended",
            message="Challenge attempts are temporarily suspended. Wait before retrying.",
            retry_after_seconds=exc.retry_after,
        )
    except (ChallengeExpired, ChallengeUsed, ChallengeFailed) as exc:
        return result_factory(
            status="challenge_required",
            challenge=runtime.challenge_manager.issue_challenge(
                owner_key=owner_key,
                action=action,
                payload=payload,
            ),
            error=challenge_error_code(exc),
            message=str(exc),
        )
    return None


def challenge_error_code(exc: Exception) -> str:
    if isinstance(exc, ChallengeExpired):
        return "challenge_expired"
    if isinstance(exc, ChallengeUsed):
        return "challenge_used"
    return "challenge_failed"
