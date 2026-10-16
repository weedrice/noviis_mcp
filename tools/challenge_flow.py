from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from exceptions import ChallengeExpired, ChallengeFailed, ChallengeRateLimited, ChallengeSuspended, ChallengeUsed


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
    error: str | None = None,
    message: str | None = None,
) -> _Result:
    try:
        challenge = runtime.challenge_manager.issue_challenge(
            owner_key=owner_key,
            action=action,
            payload=payload,
        )
    except (ChallengeSuspended, ChallengeRateLimited) as exc:
        return _limited_result(result_factory, exc)
    return result_factory(
        status="challenge_required",
        challenge=challenge,
        error=error,
        message=message,
        retryable=True,
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
        return _limited_result(result_factory, exc)
    except ChallengeUsed as exc:
        return result_factory(
            status="blocked",
            error="challenge_used",
            message=str(exc),
            retryable=False,
        )
    except (ChallengeExpired, ChallengeFailed) as exc:
        return issue_challenge_result(
            runtime=runtime,
            result_factory=result_factory,
            owner_key=owner_key,
            action=action,
            payload=payload,
            error=challenge_error_code(exc),
            message=str(exc),
        )
    return None


def _limited_result(
    result_factory: Callable[..., _Result], exc: ChallengeSuspended | ChallengeRateLimited
) -> _Result:
    suspended = isinstance(exc, ChallengeSuspended)
    return result_factory(
        status="challenge_suspended" if suspended else "challenge_rate_limited",
        error="challenge_suspended" if suspended else exc.code,
        message=str(exc),
        retry_after_seconds=exc.retry_after,
        retryable=True,
    )


def challenge_error_code(exc: Exception) -> str:
    if isinstance(exc, ChallengeExpired):
        return "challenge_expired"
    if isinstance(exc, ChallengeUsed):
        return "challenge_used"
    return "challenge_failed"
