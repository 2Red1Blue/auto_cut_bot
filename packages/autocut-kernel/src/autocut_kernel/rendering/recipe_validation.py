"""Fail-closed parser for the currently persisted LocalMediaCommand recipe."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, cast

from ..media.types import (
    MediaValidationError,
    TimeBase,
    canonical_sha256,
    require_pts,
    sha256_prefixed,
)
from .models import Recipe

RuntimeProfile = Literal["production", "test", "shadow"]


class RecipeValidationError(ValueError):
    """A stable recipe denial with no endpoint repair or numeric coercion."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


def _mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise RecipeValidationError("RECIPE_INVALID", f"{field_name} must be an object")
    raw_mapping = cast(Mapping[object, object], value)
    if any(not isinstance(key, str) for key in raw_mapping):
        raise RecipeValidationError("RECIPE_INVALID", f"{field_name} keys must be strings")
    return cast(Mapping[str, object], value)


def _required(mapping: Mapping[str, object], field_name: str) -> object:
    if field_name not in mapping:
        raise RecipeValidationError("RECIPE_INVALID", f"{field_name} is required")
    return mapping[field_name]


def _pts(value: object, field_name: str) -> int:
    try:
        return require_pts(value, field_name)
    except MediaValidationError as error:
        raise RecipeValidationError("RECIPE_INVALID_SPAN", str(error)) from error


def _sha256(value: object, field_name: str) -> str:
    try:
        return sha256_prefixed(value, field_name)
    except MediaValidationError as error:
        raise RecipeValidationError("RECIPE_INVALID", str(error)) from error


def _runtime_profile(profile: object) -> RuntimeProfile:
    if profile not in {"production", "test", "shadow"}:
        raise RecipeValidationError("RECIPE_INVALID", "profile must be production, test, or shadow")
    return cast(RuntimeProfile, profile)


def parse_recipe(
    payload: object,
    *,
    expected_source_sha256: str,
    profile: RuntimeProfile,
) -> Recipe:
    """Parse and validate exactly one fixture-backed source span.

    The accepted shape is the recipe emitted by :class:`LocalMediaCommand`:
    ``source``, ``span``, ``timebase``, and immutable fixture ``evidence``.
    Other compiler provenance fields are retained in the original artifact but
    are deliberately not interpreted by this renderer slice.
    """
    recipe = _mapping(payload, "recipe")
    if not recipe:
        raise RecipeValidationError("RECIPE_EMPTY", "recipe must not be empty")
    runtime_profile = _runtime_profile(profile)
    expected_hash = _sha256(expected_source_sha256, "expected_source_sha256")

    source = _mapping(_required(recipe, "source"), "recipe.source")
    allowed_source = {"sha256", "byte_size"}
    if set(source) != allowed_source:
        raise RecipeValidationError("RECIPE_INVALID", "recipe.source has an unsupported shape")
    source_hash = _sha256(_required(source, "sha256"), "recipe.source.sha256")
    source_size = _pts(_required(source, "byte_size"), "recipe.source.byte_size")
    if source_size <= 0:
        raise RecipeValidationError("RECIPE_INVALID", "recipe.source.byte_size must be positive")
    if source_hash != expected_hash:
        raise RecipeValidationError("SOURCE_IDENTITY_MISMATCH", "recipe source hash does not match input")

    span = _mapping(_required(recipe, "span"), "recipe.span")
    if set(span) != {"start_pts", "end_pts"}:
        raise RecipeValidationError("RECIPE_INVALID_SPAN", "recipe.span has an unsupported shape")
    start_pts = _pts(_required(span, "start_pts"), "recipe.span.start_pts")
    end_pts = _pts(_required(span, "end_pts"), "recipe.span.end_pts")
    if start_pts >= end_pts:
        raise RecipeValidationError("RECIPE_INVALID_SPAN", "recipe span must satisfy start_pts < end_pts")

    timebase = _mapping(_required(recipe, "timebase"), "recipe.timebase")
    if set(timebase) != {"numerator", "denominator"}:
        raise RecipeValidationError("RECIPE_INVALID", "recipe.timebase has an unsupported shape")
    try:
        parsed_timebase = TimeBase(
            _pts(_required(timebase, "numerator"), "recipe.timebase.numerator"),
            _pts(_required(timebase, "denominator"), "recipe.timebase.denominator"),
        )
    except MediaValidationError as error:
        raise RecipeValidationError("RECIPE_INVALID", str(error)) from error

    evidence = _mapping(_required(recipe, "evidence"), "recipe.evidence")
    fixture_id = _required(evidence, "fixture_id")
    fixture_mode = _required(evidence, "evidence_mode")
    if not isinstance(fixture_id, str) or not fixture_id:
        raise RecipeValidationError("RECIPE_INVALID", "recipe.evidence.fixture_id must be non-empty")
    if not isinstance(fixture_mode, str) or fixture_mode != "fixture_ground_truth_v1":
        raise RecipeValidationError("RECIPE_INVALID", "recipe evidence_mode is unsupported")
    if runtime_profile == "production":
        raise RecipeValidationError("FIXTURE_PROFILE_DENIED", "fixture recipes are forbidden in production")

    return Recipe(
        source_sha256=source_hash,
        source_byte_size=source_size,
        time_base=parsed_timebase,
        start_pts=start_pts,
        end_pts=end_pts,
        fixture_id=fixture_id,
        fixture_mode=fixture_mode,
        canonical_hash=canonical_sha256(payload),
    )


def validate_recipe(
    payload: object,
    *,
    expected_source_sha256: str,
    profile: RuntimeProfile,
) -> Recipe:
    """Alias for the parser, emphasizing validation at the renderer boundary."""
    return parse_recipe(payload, expected_source_sha256=expected_source_sha256, profile=profile)
