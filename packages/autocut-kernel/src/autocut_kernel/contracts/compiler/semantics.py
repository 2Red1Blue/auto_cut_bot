"""Fail-closed semantic checks that JSON Schema cannot express alone."""

from __future__ import annotations

import re
from dataclasses import dataclass
from math import gcd
from typing import Any, Mapping

from .errors import ContractCompilerError

_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class SourceClockBinding:
    """One SourceManifest-resolved clock, bound to its immutable owner Artifact.

    Construction belongs to the SourceManifest/reference resolver. Keeping the
    resolved owner together with source and clock facts prevents a caller from
    borrowing another source's temporal bounds to validate a span.
    """

    artifact_id: str
    content_hash: str
    source_id: str
    source_sha256: str
    clock_id: str
    numerator: int
    denominator: int
    origin_tick: int
    duration_tick: int


def validate_source_span_temporal_semantics(
    span: Mapping[str, Any],
    *,
    source_clock: SourceClockBinding,
) -> None:
    """Enforce the Stage 4 timing invariants for one structurally valid span.

    A JSON Schema checks field shapes. This function deliberately requires one
    resolved SourceManifest clock binding; a loose pair of bounds is not enough
    to prove a span belongs to its referenced source.
    """

    _validate_source_clock(source_clock)
    try:
        artifact_ref = span["artifact_ref"]
        source_id = span["source_id"]
        source_sha256 = span["source_sha256"]
        clock_id = span["clock_id"]
        time_base = span["time_base"]
        in_tick = span["in_tick"]
        out_tick = span["out_tick"]
    except KeyError as error:
        raise ContractCompilerError(f"source span is missing {error.args[0]!r}") from error
    if type(artifact_ref) is not dict or set(artifact_ref) != {"artifact_id", "content_hash"}:  # noqa: E721
        raise ContractCompilerError("source span artifact_ref must be a closed ArtifactRef")
    if (
        artifact_ref["artifact_id"] != source_clock.artifact_id
        or artifact_ref["content_hash"] != source_clock.content_hash
        or source_id != source_clock.source_id
        or source_sha256 != source_clock.source_sha256
        or clock_id != source_clock.clock_id
    ):
        raise ContractCompilerError("source span does not match its resolved SourceClock owner")
    if type(time_base) is not dict or set(time_base) != {"num", "den"}:  # noqa: E721
        raise ContractCompilerError("source span time_base must be a closed {num, den} object")
    numerator = time_base["num"]
    denominator = time_base["den"]
    if (
        type(numerator) is not int
        or type(denominator) is not int
        or numerator <= 0
        or denominator <= 0
    ):  # noqa: E721
        raise ContractCompilerError("source span time_base num and den must be positive integers")
    if gcd(numerator, denominator) != 1:
        raise ContractCompilerError("source span time_base must be reduced")
    if (numerator, denominator) != (source_clock.numerator, source_clock.denominator):
        raise ContractCompilerError("source span time_base does not match its resolved SourceClock")
    if type(in_tick) is not int or type(out_tick) is not int:  # noqa: E721
        raise ContractCompilerError("source span ticks must be integers")
    source_end = source_clock.origin_tick + source_clock.duration_tick
    if not source_clock.origin_tick <= in_tick < out_tick <= source_end:
        raise ContractCompilerError("source span must satisfy origin <= in < out <= origin + duration")


def _validate_source_clock(source_clock: SourceClockBinding) -> None:
    if type(source_clock) is not SourceClockBinding:  # noqa: E721
        raise ContractCompilerError("source span requires a resolved SourceClockBinding")
    required_text = (
        source_clock.artifact_id,
        source_clock.content_hash,
        source_clock.source_id,
        source_clock.source_sha256,
        source_clock.clock_id,
    )
    if any(type(value) is not str or not value for value in required_text):  # noqa: E721
        raise ContractCompilerError("resolved SourceClockBinding text fields must be non-empty strings")
    if not _SHA256.fullmatch(source_clock.content_hash) or not _SHA256.fullmatch(
        source_clock.source_sha256
    ):
        raise ContractCompilerError("resolved SourceClockBinding hashes must be sha256 values")
    if (
        type(source_clock.numerator) is not int
        or type(source_clock.denominator) is not int
        or type(source_clock.origin_tick) is not int
        or type(source_clock.duration_tick) is not int
        or source_clock.numerator <= 0
        or source_clock.denominator <= 0
        or source_clock.duration_tick < 0
    ):  # noqa: E721
        raise ContractCompilerError("resolved SourceClockBinding has invalid clock values")
    if gcd(source_clock.numerator, source_clock.denominator) != 1:
        raise ContractCompilerError("resolved SourceClockBinding time_base must be reduced")
