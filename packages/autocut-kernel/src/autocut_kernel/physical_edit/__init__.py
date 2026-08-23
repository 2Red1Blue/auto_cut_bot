"""Pure physical-edit domain operations."""

from .exact_span import (
    CandidatePairLimitError,
    ExactSpanCompiler,
    ExactSpanError,
    ExactSpanValidationError,
    FixtureBeatInput,
    NoLegalSpanError,
    SpanSelectionPolicy,
    select_exact_span,
)

__all__ = [
    "CandidatePairLimitError",
    "ExactSpanCompiler",
    "ExactSpanError",
    "ExactSpanValidationError",
    "FixtureBeatInput",
    "NoLegalSpanError",
    "SpanSelectionPolicy",
    "select_exact_span",
]
