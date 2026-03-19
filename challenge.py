from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from config import (
    CHALLENGE_EXPIRES_SECONDS,
    CHALLENGE_FAIL_LIMIT,
    CHALLENGE_SUSPEND_SECONDS,
)
from exceptions import (
    ChallengeExpired,
    ChallengeFailed,
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


@dataclass
class ChallengeRecord:
    owner_key: str
    action: str
    payload: dict[str, Any]
    answer: str
    expires_at: datetime
    used: bool = False


@dataclass
class FailureState:
    failures: int = 0
    suspended_until: datetime | None = None


class ChallengeManager:
    def __init__(self) -> None:
        self._challenges: dict[str, ChallengeRecord] = {}
        self._failures: dict[str, FailureState] = {}

    def issue_challenge(
        self,
        *,
        owner_key: str,
        action: str,
        payload: dict[str, Any],
    ) -> ChallengePrompt:
        self._cleanup()
        state = self._failures.setdefault(owner_key, FailureState())
        now = _now()
        if state.suspended_until and state.suspended_until > now:
            retry_after = int((state.suspended_until - now).total_seconds())
            raise ChallengeSuspended(retry_after=max(1, retry_after))

        left, right, operation = _random_expression()
        answer = _calculate_answer(left, right, operation)
        challenge_id = str(uuid.uuid4())
        self._challenges[challenge_id] = ChallengeRecord(
            owner_key=owner_key,
            action=action,
            payload=dict(payload),
            answer=answer,
            expires_at=now + timedelta(seconds=CHALLENGE_EXPIRES_SECONDS),
        )
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
        self._cleanup()
        state = self._failures.setdefault(owner_key, FailureState())
        now = _now()
        if state.suspended_until and state.suspended_until > now:
            retry_after = int((state.suspended_until - now).total_seconds())
            raise ChallengeSuspended(retry_after=max(1, retry_after))

        record = self._challenges.get(challenge_id)
        if record is None:
            self._mark_failure(owner_key)
            raise ChallengeFailed("Unknown challenge_id. Request a new challenge.")
        if record.used:
            self._mark_failure(owner_key)
            raise ChallengeUsed("This challenge was already used. Request a new challenge.")
        if record.expires_at < now:
            self._mark_failure(owner_key)
            raise ChallengeExpired("The challenge expired. Request a new challenge.")
        if record.owner_key != owner_key or record.action != action or record.payload != dict(payload):
            self._mark_failure(owner_key)
            raise ChallengeFailed("Challenge context mismatch. Request a new challenge.")

        normalized_answer = _normalize_answer(answer)
        if normalized_answer != record.answer:
            self._mark_failure(owner_key)
            raise ChallengeFailed("Incorrect challenge answer. Request a new challenge.")

        record.used = True
        self._failures[owner_key] = FailureState()

    def _mark_failure(self, owner_key: str) -> None:
        state = self._failures.setdefault(owner_key, FailureState())
        state.failures += 1
        if state.failures >= CHALLENGE_FAIL_LIMIT:
            state.failures = 0
            state.suspended_until = _now() + timedelta(seconds=CHALLENGE_SUSPEND_SECONDS)
            raise ChallengeSuspended(retry_after=CHALLENGE_SUSPEND_SECONDS)

    def _cleanup(self) -> None:
        now = _now()
        expired_ids = [
            challenge_id
            for challenge_id, record in self._challenges.items()
            if record.expires_at < now or record.used
        ]
        for challenge_id in expired_ids:
            self._challenges.pop(challenge_id, None)

        expired_suspensions = [
            key
            for key, state in self._failures.items()
            if state.suspended_until is not None and state.suspended_until <= now
        ]
        for key in expired_suspensions:
            self._failures[key] = FailureState()


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
    try:
        numeric = Decimal(answer.strip())
    except (InvalidOperation, AttributeError):
        raise ChallengeFailed("Challenge answer must be a numeric string.") from None
    return str(numeric.quantize(_QUANTIZER, rounding=ROUND_HALF_UP))


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


def _now() -> datetime:
    return datetime.now(UTC)
