"""Fail-closed parser for the currently persisted LocalMediaCommand recipe."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, cast

from ..media.types import (
    MediaEvidence,
    MediaValidationError,
    PTSIndex,
    SourceIdentity,
    TickRange,
    TimeBase,
    ToolEvidence,
    ValidityIntervals,
    VideoStreamEvidence,
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


def _exact_keys(mapping: Mapping[str, object], expected: set[str], field_name: str) -> None:
    if set(mapping) != expected:
        raise RecipeValidationError("RECIPE_INVALID", f"{field_name} has an unsupported shape")


def _timebase(value: object, field_name: str) -> TimeBase:
    mapping = _mapping(value, field_name)
    _exact_keys(mapping, {"numerator", "denominator"}, field_name)
    try:
        return TimeBase(
            _pts(_required(mapping, "numerator"), f"{field_name}.numerator"),
            _pts(_required(mapping, "denominator"), f"{field_name}.denominator"),
        )
    except MediaValidationError as error:
        raise RecipeValidationError("RECIPE_INVALID", str(error)) from error


def _parse_evidence(value: object) -> MediaEvidence:
    evidence = _mapping(value, "recipe.evidence")
    _exact_keys(
        evidence,
        {
            "source",
            "video_stream",
            "pts_index",
            "pts_index_sha256",
            "validity_intervals",
            "ffprobe",
            "fixture_id",
            "fixture_manifest_sha256",
            "fixture_sidecar_sha256",
            "fixture_schema_version",
            "evidence_mode",
        },
        "recipe.evidence",
    )
    source = _mapping(_required(evidence, "source"), "recipe.evidence.source")
    _exact_keys(source, {"sha256", "byte_size"}, "recipe.evidence.source")
    video = _mapping(_required(evidence, "video_stream"), "recipe.evidence.video_stream")
    _exact_keys(
        video,
        {"stream_index", "codec_name", "width", "height", "time_base"},
        "recipe.evidence.video_stream",
    )
    frames = _required(evidence, "pts_index")
    if not isinstance(frames, list):
        raise RecipeValidationError("RECIPE_INVALID", "recipe.evidence.pts_index must be a list")
    frames = cast(list[object], frames)
    intervals_value = _required(evidence, "validity_intervals")
    if not isinstance(intervals_value, list):
        raise RecipeValidationError("RECIPE_INVALID", "recipe.evidence.validity_intervals must be a list")
    intervals_value = cast(list[object], intervals_value)
    intervals: list[TickRange] = []
    for index, item in enumerate(intervals_value):
        interval = _mapping(item, f"recipe.evidence.validity_intervals[{index}]")
        _exact_keys(
            interval,
            {"start_pts", "end_pts"},
            f"recipe.evidence.validity_intervals[{index}]",
        )
        intervals.append(
            TickRange(
                _pts(_required(interval, "start_pts"), f"validity_intervals[{index}].start_pts"),
                _pts(_required(interval, "end_pts"), f"validity_intervals[{index}].end_pts"),
            )
        )
    ffprobe = _mapping(_required(evidence, "ffprobe"), "recipe.evidence.ffprobe")
    _exact_keys(ffprobe, {"executable", "version", "stderr_sha256"}, "recipe.evidence.ffprobe")
    fixture_id = _required(evidence, "fixture_id")
    evidence_mode = _required(evidence, "evidence_mode")
    if not isinstance(fixture_id, str) or not fixture_id:
        raise RecipeValidationError("RECIPE_INVALID", "recipe.evidence.fixture_id must be non-empty")
    if not isinstance(evidence_mode, str):
        raise RecipeValidationError("RECIPE_INVALID", "recipe.evidence.evidence_mode must be a string")
    codec_name = _required(video, "codec_name")
    executable = _required(ffprobe, "executable")
    version = _required(ffprobe, "version")
    if not isinstance(codec_name, str) or not isinstance(executable, str) or not isinstance(version, str):
        raise RecipeValidationError("RECIPE_INVALID", "recipe evidence text fields must be strings")
    try:
        return MediaEvidence(
            source=SourceIdentity(
                _sha256(_required(source, "sha256"), "recipe.evidence.source.sha256"),
                _pts(_required(source, "byte_size"), "recipe.evidence.source.byte_size"),
            ),
            video_stream=VideoStreamEvidence(
                _pts(_required(video, "stream_index"), "recipe.evidence.video_stream.stream_index"),
                codec_name,
                _pts(_required(video, "width"), "recipe.evidence.video_stream.width"),
                _pts(_required(video, "height"), "recipe.evidence.video_stream.height"),
                _timebase(_required(video, "time_base"), "recipe.evidence.video_stream.time_base"),
            ),
            pts_index=PTSIndex(tuple(_pts(item, f"recipe.evidence.pts_index[{index}]") for index, item in enumerate(frames))),
            validity_intervals=ValidityIntervals(tuple(intervals)),
            pts_index_sha256=_sha256(_required(evidence, "pts_index_sha256"), "recipe.evidence.pts_index_sha256"),
            ffprobe=ToolEvidence(
                executable,
                version,
                _sha256(_required(ffprobe, "stderr_sha256"), "recipe.evidence.ffprobe.stderr_sha256"),
            ),
            fixture_id=fixture_id,
            fixture_manifest_sha256=_sha256(
                _required(evidence, "fixture_manifest_sha256"), "recipe.evidence.fixture_manifest_sha256"
            ),
            fixture_sidecar_sha256=_sha256(
                _required(evidence, "fixture_sidecar_sha256"), "recipe.evidence.fixture_sidecar_sha256"
            ),
            fixture_schema_version=_pts(
                _required(evidence, "fixture_schema_version"), "recipe.evidence.fixture_schema_version"
            ),
            evidence_mode=evidence_mode,
        )
    except MediaValidationError as error:
        raise RecipeValidationError("RECIPE_INVALID", str(error)) from error


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

    parsed_timebase = _timebase(_required(recipe, "timebase"), "recipe.timebase")
    evidence = _parse_evidence(_required(recipe, "evidence"))
    if evidence.source.sha256 != source_hash or evidence.source.byte_size != source_size:
        raise RecipeValidationError("SOURCE_IDENTITY_MISMATCH", "recipe source does not bind evidence source")
    if evidence.video_stream.time_base != parsed_timebase:
        raise RecipeValidationError("RECIPE_INVALID", "recipe timebase does not bind evidence video stream")
    try:
        evidence.pts_index.require_member(start_pts, "recipe.span.start_pts")
        evidence.pts_index.require_member(end_pts, "recipe.span.end_pts")
        if not evidence.validity_intervals.covers(TickRange(start_pts, end_pts)):
            raise RecipeValidationError("RECIPE_INVALID_SPAN", "recipe span is outside evidence validity intervals")
    except MediaValidationError as error:
        raise RecipeValidationError("RECIPE_INVALID_SPAN", str(error)) from error
    if evidence.evidence_mode != "fixture_ground_truth_v1":
        raise RecipeValidationError("RECIPE_INVALID", "recipe evidence_mode is unsupported")
    if runtime_profile == "production":
        raise RecipeValidationError("FIXTURE_PROFILE_DENIED", "fixture recipes are forbidden in production")

    return Recipe(
        source_sha256=source_hash,
        source_byte_size=source_size,
        time_base=parsed_timebase,
        start_pts=start_pts,
        end_pts=end_pts,
        fixture_id=evidence.fixture_id,
        fixture_mode=evidence.evidence_mode,
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
