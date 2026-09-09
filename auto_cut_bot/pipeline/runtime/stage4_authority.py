"""Closed Stage 4 policy authority for the durable Runtime adapter.

This value is intentionally separate from caller input and process defaults.
Composition must obtain it from a protected installed authority source before a
``stage4_recipe`` port can be constructed.  It contains policy only: it does
not resolve media, query a Store, or grant render/publication authority.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from importlib import resources
from typing import Final, Mapping, cast

from autocut_kernel.contracts.compiler.canonical import canonical_json_hash
from autocut_kernel.media.types import TimeBase
from autocut_kernel.physical_edit.candidate_exact_span import CandidateExactSpanPolicy
from autocut_kernel.physical_edit.editorial_exact_span import (
    EditorialExactSpanPolicy,
)
from autocut_kernel.pipeline.compile_production_recipe_command import (
    ProductionRecipeCompilationLimits,
)

STAGE4_RECIPE_AUTHORITY_SCHEMA_VERSION: Final = "stage4-recipe-authority-v1"
STAGE4_RECIPE_RUNTIME_STRATEGY: Final = "stage4-recipe-runtime-v1"
_MAX_EXACT_INTEGER: Final = 2**53 - 1
_MAX_AUTHORITY_BYTES: Final = 256 * 1024
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")


class Stage4RecipeAuthorityError(ValueError):
    """A Stage 4 runtime policy source is malformed or not explicit."""


def _sha256(value: object, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:  # noqa: E721
        raise Stage4RecipeAuthorityError(f"{label} must be a lowercase sha256 digest")
    return value


def _no_duplicate_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise Stage4RecipeAuthorityError("Stage 4 authority JSON has duplicate keys")
        result[key] = value
    return result


def _object(value: object, fields: tuple[str, ...], label: str) -> Mapping[str, object]:
    if type(value) is not dict:  # noqa: E721
        raise Stage4RecipeAuthorityError(f"{label} must be a closed object")
    raw = cast(dict[object, object], value)
    if any(type(key) is not str for key in raw) or set(raw) != set(fields):  # noqa: E721
        raise Stage4RecipeAuthorityError(f"{label} has missing or unknown fields")
    return cast(Mapping[str, object], raw)


def _integer(value: object, label: str, *, minimum: int = 1) -> int:
    if type(value) is not int or not minimum <= value <= _MAX_EXACT_INTEGER:  # noqa: E721
        raise Stage4RecipeAuthorityError(f"{label} must be a bounded exact integer")
    return value


def _time_base(value: object, label: str) -> TimeBase:
    raw = _object(value, ("numerator", "denominator"), label)
    try:
        return TimeBase(
            _integer(raw["numerator"], f"{label}.numerator"),
            _integer(raw["denominator"], f"{label}.denominator"),
        )
    except ValueError as error:
        raise Stage4RecipeAuthorityError(str(error)) from error


def _editorial_policy(value: object) -> EditorialExactSpanPolicy:
    raw = _object(
        value,
        ("strategy_version", "context_maximum_extension"),
        "editorial_exact_span_policy",
    )
    extension = _object(
        raw["context_maximum_extension"],
        ("tick", "time_base"),
        "editorial_exact_span_policy.context_maximum_extension",
    )
    try:
        return EditorialExactSpanPolicy(
            cast(str, raw["strategy_version"]),
            _integer(extension["tick"], "context_maximum_extension.tick"),
            _time_base(extension["time_base"], "context_maximum_extension.time_base"),
        )
    except (TypeError, ValueError) as error:
        raise Stage4RecipeAuthorityError("editorial exact-span policy is invalid") from error


def _candidate_policy(value: object) -> CandidateExactSpanPolicy:
    raw = _object(
        value,
        (
            "strategy",
            "max_video_pair_visits",
            "max_av_pair_visits",
            "endpoint_stability_video_tick",
            "subtitle_clearance_floor_video_tick",
            "av_sync_tolerance_audio_tick",
        ),
        "candidate_exact_span_policy",
    )
    if raw["strategy"] != "candidate-local-exact-v1":
        raise Stage4RecipeAuthorityError("candidate exact-span strategy is unsupported")
    try:
        return CandidateExactSpanPolicy(
            _integer(raw["max_video_pair_visits"], "max_video_pair_visits"),
            _integer(raw["max_av_pair_visits"], "max_av_pair_visits"),
            _integer(raw["endpoint_stability_video_tick"], "endpoint_stability_video_tick"),
            _integer(
                raw["subtitle_clearance_floor_video_tick"],
                "subtitle_clearance_floor_video_tick",
            ),
            _integer(
                raw["av_sync_tolerance_audio_tick"],
                "av_sync_tolerance_audio_tick",
                minimum=0,
            ),
        )
    except (TypeError, ValueError) as error:
        raise Stage4RecipeAuthorityError("candidate exact-span policy is invalid") from error


def _compilation_limits(value: object) -> ProductionRecipeCompilationLimits:
    raw = _object(
        value,
        ("max_compilation_entries", "max_member_payload_bytes", "max_total_payload_bytes"),
        "compilation_limits",
    )
    try:
        return ProductionRecipeCompilationLimits(
            _integer(raw["max_compilation_entries"], "max_compilation_entries"),
            _integer(raw["max_member_payload_bytes"], "max_member_payload_bytes"),
            _integer(raw["max_total_payload_bytes"], "max_total_payload_bytes"),
        )
    except (TypeError, ValueError) as error:
        raise Stage4RecipeAuthorityError("Stage 4 compilation limits are invalid") from error


@dataclass(frozen=True, slots=True)
class Stage4RecipeAuthorityProfile:
    """Exact policies needed to issue one Stage 4 Recipe command request."""

    artifact_revision: int
    editorial_exact_span_policy: EditorialExactSpanPolicy
    candidate_exact_span_policy: CandidateExactSpanPolicy
    compilation_limits: ProductionRecipeCompilationLimits
    strategy_version: str = STAGE4_RECIPE_RUNTIME_STRATEGY

    def __post_init__(self) -> None:
        _integer(self.artifact_revision, "artifact_revision")
        if self.strategy_version != STAGE4_RECIPE_RUNTIME_STRATEGY:
            raise Stage4RecipeAuthorityError("Stage 4 runtime strategy is unsupported")
        if type(self.editorial_exact_span_policy) is not EditorialExactSpanPolicy:  # noqa: E721
            raise Stage4RecipeAuthorityError("editorial exact-span policy must be exact")
        if type(self.candidate_exact_span_policy) is not CandidateExactSpanPolicy:  # noqa: E721
            raise Stage4RecipeAuthorityError("candidate exact-span policy must be exact")
        if type(self.compilation_limits) is not ProductionRecipeCompilationLimits:  # noqa: E721
            raise Stage4RecipeAuthorityError("compilation limits must be exact")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": STAGE4_RECIPE_AUTHORITY_SCHEMA_VERSION,
            "strategy_version": self.strategy_version,
            "artifact_revision": self.artifact_revision,
            "editorial_exact_span_policy": self.editorial_exact_span_policy.to_mapping(),
            "candidate_exact_span_policy": self.candidate_exact_span_policy.to_mapping(),
            "compilation_limits": self.compilation_limits.to_mapping(),
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())

    @classmethod
    def from_mapping(cls, value: object) -> Stage4RecipeAuthorityProfile:
        raw = _object(
            value,
            (
                "schema_version",
                "strategy_version",
                "artifact_revision",
                "editorial_exact_span_policy",
                "candidate_exact_span_policy",
                "compilation_limits",
            ),
            "stage4_recipe_authority",
        )
        if raw["schema_version"] != STAGE4_RECIPE_AUTHORITY_SCHEMA_VERSION:
            raise Stage4RecipeAuthorityError("Stage 4 authority schema is unsupported")
        return cls(
            _integer(raw["artifact_revision"], "artifact_revision"),
            _editorial_policy(raw["editorial_exact_span_policy"]),
            _candidate_policy(raw["candidate_exact_span_policy"]),
            _compilation_limits(raw["compilation_limits"]),
            cast(str, raw["strategy_version"]),
        )


def decode_stage4_recipe_authority(
    raw: bytes,
    *,
    expected_sha256: str,
) -> Stage4RecipeAuthorityProfile:
    """Decode one fixed, digest-bound package authority source."""
    if type(raw) is not bytes or not raw or len(raw) > _MAX_AUTHORITY_BYTES:  # noqa: E721
        raise Stage4RecipeAuthorityError("Stage 4 authority source is missing or exceeds its bound")
    digest = _sha256(expected_sha256, "Stage 4 authority digest")
    actual = "sha256:" + hashlib.sha256(raw).hexdigest()
    if actual != digest:
        raise Stage4RecipeAuthorityError("Stage 4 authority source digest mismatch")
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_no_duplicate_object,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (UnicodeError, ValueError, json.JSONDecodeError, RecursionError) as error:
        if isinstance(error, Stage4RecipeAuthorityError):
            raise
        raise Stage4RecipeAuthorityError("Stage 4 authority source is not strict UTF-8 JSON") from error
    return Stage4RecipeAuthorityProfile.from_mapping(value)


def load_installed_stage4_recipe_authority() -> Stage4RecipeAuthorityProfile:
    """Load only the fixed package resources; no path or environment override exists."""
    try:
        root = resources.files(__package__).joinpath("_authority")
        with root.joinpath("stage4-recipe.sha256").open("rb") as stream:
            digest_raw = stream.read(73)
        if len(digest_raw) != 72 or not digest_raw.endswith(b"\n"):
            raise Stage4RecipeAuthorityError("Stage 4 authority digest framing is invalid")
        with root.joinpath("stage4-recipe.json").open("rb") as stream:
            raw = stream.read(_MAX_AUTHORITY_BYTES + 1)
    except Stage4RecipeAuthorityError:
        raise
    except (ModuleNotFoundError, OSError, UnicodeError) as error:
        raise Stage4RecipeAuthorityError("installed Stage 4 authority source is unavailable") from error
    try:
        digest = digest_raw[:-1].decode("ascii")
    except UnicodeDecodeError as error:
        raise Stage4RecipeAuthorityError("Stage 4 authority digest is not ASCII") from error
    return decode_stage4_recipe_authority(raw, expected_sha256=digest)


__all__ = (
    "STAGE4_RECIPE_AUTHORITY_SCHEMA_VERSION",
    "STAGE4_RECIPE_RUNTIME_STRATEGY",
    "Stage4RecipeAuthorityError",
    "Stage4RecipeAuthorityProfile",
    "decode_stage4_recipe_authority",
    "load_installed_stage4_recipe_authority",
)
