"""Structured failures emitted by authority gates."""

from __future__ import annotations


class GateViolation(ValueError):  # noqa: N818 - contract term used in receipts/reason codes
    """A deterministic, fail-closed governance gate failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")
