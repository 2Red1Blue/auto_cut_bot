"""Frozen, provider-neutral policy for durable VLM generation recovery."""

from __future__ import annotations

from dataclasses import dataclass

from ..media.types import canonical_sha256
from .models import VlmValidationError

GENERATION_RETRY_STRATEGY_VERSION = "generation-retry-v1"
# The first registered provider profile permits up to 5 minutes of file
# preparation plus 5 minutes of streamed generation.  Keep a second 10-minute
# fencing margin so a live owner cannot be overtaken by a reconciler.
GENERATION_PROVIDER_LEASE_SECONDS = 20 * 60


@dataclass(frozen=True, slots=True)
class GenerationRetryPolicy:
    """Bound one command to an explicit number of separately persisted attempts."""

    strategy_version: str
    max_attempts: int
    backoff_seconds: tuple[int, ...]

    def __post_init__(self) -> None:
        if self.strategy_version != GENERATION_RETRY_STRATEGY_VERSION:
            raise VlmValidationError("generation retry strategy_version is not registered")
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 3:  # noqa: E721
            raise VlmValidationError(
                "generation retry max_attempts must be between one and three"
            )
        if type(self.backoff_seconds) is not tuple:  # noqa: E721
            raise VlmValidationError("generation retry backoff_seconds must be a tuple")
        if len(self.backoff_seconds) != self.max_attempts - 1:
            raise VlmValidationError(
                "generation retry backoff_seconds must contain max_attempts - 1 entries"
            )
        if any(type(value) is not int or value < 0 for value in self.backoff_seconds):  # noqa: E721
            raise VlmValidationError(
                "generation retry backoff_seconds must contain non-negative integers"
            )

    def to_mapping(self) -> dict[str, object]:
        return {
            "backoff_seconds": list(self.backoff_seconds),
            "max_attempts": self.max_attempts,
            "strategy_version": self.strategy_version,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    def backoff_after(self, attempt_ordinal: int) -> int:
        """Return the frozen delay before the next Attempt may be dispatched."""

        if type(attempt_ordinal) is not int or not (  # noqa: E721
            1 <= attempt_ordinal < self.max_attempts
        ):
            raise VlmValidationError(
                "attempt_ordinal must identify a retryable non-final Attempt"
            )
        return self.backoff_seconds[attempt_ordinal - 1]


__all__ = [
    "GENERATION_PROVIDER_LEASE_SECONDS",
    "GENERATION_RETRY_STRATEGY_VERSION",
    "GenerationRetryPolicy",
]
