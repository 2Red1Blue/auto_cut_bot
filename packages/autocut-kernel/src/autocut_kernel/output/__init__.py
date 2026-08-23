"""Atomic local-output promotion boundary."""

from .local_promotion import (
    LocalPromotionError,
    LocalPromotionRequest,
    PromotionResult,
    promote_local_output,
)

__all__ = [
    "LocalPromotionError",
    "LocalPromotionRequest",
    "PromotionResult",
    "promote_local_output",
]
