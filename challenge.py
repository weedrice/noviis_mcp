from __future__ import annotations

import math
import random
import re
import time
import uuid
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Literal

from config import (
    CHALLENGE_EXPIRES_SECONDS,
    CHALLENGE_FAIL_LIMIT,
    CHALLENGE_FAILURE_WINDOW_SECONDS,
    CHALLENGE_ISSUE_LIMIT,
    CHALLENGE_ISSUE_WINDOW_SECONDS,
    CHALLENGE_MAX_PENDING,
    CHALLENGE_RETENTION_SECONDS,
    CHALLENGE_SUSPEND_SECONDS,
)
from exceptions import (
    ChallengeExpired,
    ChallengeFailed,
    ChallengeRateLimited,
    ChallengeSuspended,
    ChallengeUsed,
)


_RNG = random.SystemRandom()
_SEPARATORS = ("^", "|", "~", "]")
_OPERATIONS = ("add", "subtract", "multiply", "divide")
_QUANTIZER = Decimal("0.00")


@dataclass
class ChallengePrompt:
    challenge_id: str
    challenge_text: str
    expires_in_seconds: int
    answer_format: str = "Decimal number rounded to two places using ROUND_HALF_UP."


@dataclass
class ChallengeRecord:
    owner_key: str
    action: str
    payload: dict[str, Any]
    answer: str
    expires_at: float
    status: Literal["pending", "used", "expired", "failed"] = "pending"
    finished_at: float | None = None


@dataclass
class OwnerState:
    failures: list[float] = field(default_factory=list)
    issued_at: list[float] = field(default_factory=list)
    suspended_until: float | None = None


class ChallengeManager:
    """Process-local state, accessed synchronously from the async MCP tools."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._challenges: dict[str, ChallengeRecord] = {}
        self._owners: dict[str, OwnerState] = {}

    def issue_challenge(
        self,
        *,
        owner_key: str,
        action: str,
        payload: dict[str, Any],
    ) -> ChallengePrompt:
        now = self._clock()
        self._cleanup(now)
        state = self._owners.setdefault(owner_key, OwnerState())
        self._check_suspension(state, now)
        if len(state.issued_at) >= CHALLENGE_ISSUE_LIMIT:
            raise ChallengeRateLimited(
                code="challenge_issue_limit",
                retry_after=_seconds_until(state.issued_at[0] + CHALLENGE_ISSUE_WINDOW_SECONDS, now),
                message="Challenge issuance limit reached. Wait before requesting another challenge.",
            )
        pending = [
            record for record in self._challenges.values()
            if record.owner_key == owner_key and record.status == "pending"
        ]
        if len(pending) >= CHALLENGE_MAX_PENDING:
            raise ChallengeRateLimited(
                code="challenge_pending_limit",
                retry_after=_seconds_until(min(record.expires_at for record in pending), now),
                message="Too many pending challenges. Complete an existing challenge or wait for expiry.",
            )

        left, right, operation = _random_expression()
        challenge_id = str(uuid.uuid4())
        self._challenges[challenge_id] = ChallengeRecord(
            owner_key=owner_key,
            action=action,
            payload=deepcopy(payload),
            answer=_calculate_answer(left, right, operation),
            expires_at=now + CHALLENGE_EXPIRES_SECONDS,
        )
        state.issued_at.append(now)
        return ChallengePrompt(
            challenge_id=challenge_id,
            challenge_text=_obfuscate_expression(left, right, operation),
            expires_in_seconds=CHALLENGE_EXPIRES_SECONDS,
        )

    def verify_challenge(
        self,
        *,
        owner_key: str,
        action: str,
        challenge_id: str,
        answer: str,
        payload: dict[str, Any],
    ) -> None:
        now = self._clock()
        self._cleanup(now)
        state = self._owners.setdefault(owner_key, OwnerState())
        self._check_suspension(state, now)
        record = self._challenges.get(challenge_id)
        if record is None:
            self._mark_failure(state, now)
            raise ChallengeFailed("Unknown challenge_id. Request a new challenge.")
        # Check ownership before disclosing state or invalidating another owner's challenge.
        if record.owner_key != owner_key:
            self._mark_failure(state, now)
            raise ChallengeFailed("Challenge context mismatch. Request a new challenge.")
        if record.action != action or record.payload != payload:
            if record.status == "pending":
                self._finish(record, "failed", now)
            self._mark_failure(state, now)
            raise ChallengeFailed("Challenge context mismatch. Request a new challenge.")
        if record.status == "used":
            raise ChallengeUsed("Challenge already used. Check the previous write result before starting a new write.")
        if record.status == "expired":
            raise ChallengeExpired("The challenge expired. Request a new challenge.")
        if record.status == "failed":
            self._mark_failure(state, now)
            raise ChallengeFailed("This challenge failed. Request a new challenge.")

        try:
            normalized_answer = _normalize_answer(answer)
        except ChallengeFailed:
            self._finish(record, "failed", now)
            self._mark_failure(state, now)
            raise
        if normalized_answer != record.answer:
            self._finish(record, "failed", now)
            self._mark_failure(state, now)
            raise ChallengeFailed("Incorrect challenge answer. Request a new challenge.")
        self._finish(record, "used", now)
        # Successful answers do not erase other failures in the rolling window.

    @staticmethod
    def _finish(record: ChallengeRecord, status: Literal["used", "expired", "failed"], now: float) -> None:
        record.status = status
        record.finished_at = now

    @staticmethod
    def _check_suspension(state: OwnerState, now: float) -> None:
        if state.suspended_until is not None and state.suspended_until > now:
            raise ChallengeSuspended(retry_after=_seconds_until(state.suspended_until, now))

    @staticmethod
    def _mark_failure(state: OwnerState, now: float) -> None:
        state.failures.append(now)
        if len(state.failures) >= CHALLENGE_FAIL_LIMIT:
            state.failures.clear()
            state.suspended_until = now + CHALLENGE_SUSPEND_SECONDS
            raise ChallengeSuspended(retry_after=CHALLENGE_SUSPEND_SECONDS)

    def _cleanup(self, now: float) -> None:
        for challenge_id, record in list(self._challenges.items()):
            if record.status == "pending" and record.expires_at <= now:
                self._finish(record, "expired", record.expires_at)
            if record.finished_at is not None and now >= record.finished_at + CHALLENGE_RETENTION_SECONDS:
                del self._challenges[challenge_id]
        for owner_key, state in list(self._owners.items()):
            state.failures = [stamp for stamp in state.failures if stamp > now - CHALLENGE_FAILURE_WINDOW_SECONDS]
            state.issued_at = [stamp for stamp in state.issued_at if stamp > now - CHALLENGE_ISSUE_WINDOW_SECONDS]
            if state.suspended_until is not None and state.suspended_until <= now:
                state.suspended_until = None
            if not state.failures and not state.issued_at and state.suspended_until is None:
                del self._owners[owner_key]


def _seconds_until(deadline: float, now: float) -> int:
    return max(1, math.ceil(deadline - now))


def _random_expression() -> tuple[int, int, str]:
    operation = _RNG.choice(_OPERATIONS)
    if operation == "divide":
        return _RNG.randint(1, 99), _RNG.randint(1, 12), operation
    return _RNG.randint(1, 99), _RNG.randint(1, 99), operation


def _calculate_answer(left: int, right: int, operation: str) -> str:
    left_decimal = Decimal(left)
    right_decimal = Decimal(right)
    if operation == "add":
        result = left_decimal + right_decimal
    elif operation == "subtract":
        result = left_decimal - right_decimal
    elif operation == "multiply":
        result = left_decimal * right_decimal
    else:
        result = left_decimal / right_decimal
    return str(result.quantize(_QUANTIZER, rounding=ROUND_HALF_UP))


def _normalize_answer(answer: str) -> str:
    # Bound work before Decimal parsing; exponent notation and special values are not answers.
    if not isinstance(answer, str) or len(answer) > 64:
        raise ChallengeFailed("Challenge answer must be a decimal string of at most 64 characters.")
    value = answer.strip()
    if not re.fullmatch(r"[+-]?[0-9]+(?:\.[0-9]+)?", value):
        raise ChallengeFailed("Challenge answer must be a finite decimal number.")
    try:
        numeric = Decimal(value)
        if not numeric.is_finite() or abs(numeric) > 10000:
            raise ChallengeFailed("Challenge answer is outside the supported range.")
        rounded = numeric.quantize(_QUANTIZER, rounding=ROUND_HALF_UP)
        if rounded == 0:
            rounded = abs(rounded)
        return str(rounded)
    except InvalidOperation:
        raise ChallengeFailed("Challenge answer must be a finite decimal number.") from None


def _obfuscate_expression(left: int, right: int, operation: str) -> str:
    sentence = (
        f"the first value is {_number_to_words(left)} and the second value is "
        f"{_number_to_words(right)}, {operation} them"
    )
    words = sentence.split()
    obfuscated_words = [_spongebob_case(word) for word in words]
    return _random_join(obfuscated_words)


def _random_join(words: list[str]) -> str:
    parts: list[str] = []
    for index, word in enumerate(words):
        parts.append(word)
        if index != len(words) - 1:
            parts.append(_RNG.choice(_SEPARATORS))
    return " ".join(parts)


def _spongebob_case(word: str) -> str:
    chars: list[str] = []
    upper = bool(_RNG.getrandbits(1))
    for char in word:
        if char.isalpha():
            chars.append(char.upper() if upper else char.lower())
            upper = not upper
        else:
            chars.append(char)
    return "".join(chars)


def _number_to_words(value: int) -> str:
    below_twenty = (
        "zero",
        "one",
        "two",
        "three",
        "four",
        "five",
        "six",
        "seven",
        "eight",
        "nine",
        "ten",
        "eleven",
        "twelve",
        "thirteen",
        "fourteen",
        "fifteen",
        "sixteen",
        "seventeen",
        "eighteen",
        "nineteen",
    )
    tens = (
        "",
        "",
        "twenty",
        "thirty",
        "forty",
        "fifty",
        "sixty",
        "seventy",
        "eighty",
        "ninety",
    )
    if value < 20:
        return below_twenty[value]
    if value < 100:
        quotient, remainder = divmod(value, 10)
        if remainder == 0:
            return tens[quotient]
        return f"{tens[quotient]} {below_twenty[remainder]}"
    hundreds, remainder = divmod(value, 100)
    if remainder == 0:
        return f"{below_twenty[hundreds]} hundred"
    return f"{below_twenty[hundreds]} hundred {_number_to_words(remainder)}"
