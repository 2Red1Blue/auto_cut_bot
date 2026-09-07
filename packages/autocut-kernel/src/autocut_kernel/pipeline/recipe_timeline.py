"""Read-only, deterministic display projections for committed production Recipes.

Timeline values are derived from immutable Stage 4 recipe evidence.  They are
not a renderer input and carry no authority to alter a Recipe, materialize a
Blob, or promote a local render.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Final, Literal

from ..rendering.production_render_plan import (
    PRODUCTION_AV_H264_AAC_PROFILE,
    ProductionAvRenderProfile,
    build_production_render_plan,
)
from ..store.models import CommittedArtifactMemberReference
from .production_recipe import ProductionRecipe, ProductionSpan

RECIPE_TIMELINE_SCHEMA_VERSION: Final = "recipe-timeline-v1"
RECIPE_DIFF_SCHEMA_VERSION: Final = "recipe-diff-v1"


class RecipeTimelineError(ValueError):
    """The exact Recipe cannot be safely projected for display."""


@dataclass(frozen=True, slots=True)
class RecipeTimelineReadLimits:
    """Bound the inspection-only database and display path."""

    max_members: int = 130
    max_member_payload_bytes: int = 1_048_576
    max_total_payload_bytes: int = 8_388_608
    max_recipes: int = 64
    max_beats_per_recipe: int = 128
    max_clips_per_recipe: int = 2_048

    def __post_init__(self) -> None:
        for name in (
            "max_members",
            "max_member_payload_bytes",
            "max_total_payload_bytes",
            "max_recipes",
            "max_beats_per_recipe",
            "max_clips_per_recipe",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 1:  # noqa: E721
                raise RecipeTimelineError(f"{name} must be a positive integer")
        if self.max_member_payload_bytes > self.max_total_payload_bytes:
            raise RecipeTimelineError("member payload bound cannot exceed total payload bound")


@dataclass(frozen=True, slots=True)
class RecipeTimelineTimeBase:
    numerator: int
    denominator: int

    def __post_init__(self) -> None:
        if (
            type(self.numerator) is not int
            or type(self.denominator) is not int
            or self.numerator < 1
            or self.denominator < 1
        ):  # noqa: E721
            raise RecipeTimelineError("timeline time base must have positive integer components")

    def to_mapping(self) -> dict[str, int]:
        return {"numerator": self.numerator, "denominator": self.denominator}


@dataclass(frozen=True, slots=True)
class RecipeTimelineClip:
    ordinal: int
    beat_ordinal: int
    span_ordinal: int
    input_ordinal: int
    beat_id: str
    requirement_id: str
    alternative_id: str
    candidate_id: str
    output_in_tick: int
    output_out_tick: int
    video_clock_id: str
    video_time_base: RecipeTimelineTimeBase
    video_in_tick: int
    video_out_tick: int
    audio_clock_id: str
    audio_time_base: RecipeTimelineTimeBase
    audio_in_tick: int
    audio_out_tick: int
    exact_span_query_sha256: str
    exact_span_result_sha256: str
    exact_span_proof_sha256: str
    av_pairing_proof_sha256: str

    def __post_init__(self) -> None:
        for name in (
            "ordinal",
            "beat_ordinal",
            "span_ordinal",
            "input_ordinal",
            "output_in_tick",
            "output_out_tick",
            "video_in_tick",
            "video_out_tick",
            "audio_in_tick",
            "audio_out_tick",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:  # noqa: E721
                raise RecipeTimelineError(f"timeline clip {name} must be a non-negative integer")
        if (
            self.output_in_tick >= self.output_out_tick
            or self.video_in_tick >= self.video_out_tick
            or self.audio_in_tick >= self.audio_out_tick
        ):
            raise RecipeTimelineError("timeline clip ranges must be non-empty")
        if type(self.video_time_base) is not RecipeTimelineTimeBase:  # noqa: E721
            raise RecipeTimelineError("timeline clip requires an exact video time base")
        if type(self.audio_time_base) is not RecipeTimelineTimeBase:  # noqa: E721
            raise RecipeTimelineError("timeline clip requires an exact audio time base")
        for name in (
            "beat_id",
            "requirement_id",
            "alternative_id",
            "candidate_id",
            "video_clock_id",
            "audio_clock_id",
            "exact_span_query_sha256",
            "exact_span_result_sha256",
            "exact_span_proof_sha256",
            "av_pairing_proof_sha256",
        ):
            value = getattr(self, name)
            if type(value) is not str or not value:  # noqa: E721
                raise RecipeTimelineError(f"timeline clip {name} must be non-empty text")

    @property
    def stable_key(self) -> tuple[str, str, str]:
        return self.beat_id, self.requirement_id, self.alternative_id

    def to_mapping(self) -> dict[str, object]:
        return {
            "ordinal": self.ordinal,
            "beat_ordinal": self.beat_ordinal,
            "span_ordinal": self.span_ordinal,
            "input_ordinal": self.input_ordinal,
            "beat_id": self.beat_id,
            "requirement_id": self.requirement_id,
            "alternative_id": self.alternative_id,
            "candidate_id": self.candidate_id,
            "output_in_tick": self.output_in_tick,
            "output_out_tick": self.output_out_tick,
            "video_clock_id": self.video_clock_id,
            "video_time_base": self.video_time_base.to_mapping(),
            "video_in_tick": self.video_in_tick,
            "video_out_tick": self.video_out_tick,
            "audio_clock_id": self.audio_clock_id,
            "audio_time_base": self.audio_time_base.to_mapping(),
            "audio_in_tick": self.audio_in_tick,
            "audio_out_tick": self.audio_out_tick,
        }


@dataclass(frozen=True, slots=True)
class RecipeTimeline:
    recipe_reference: CommittedArtifactMemberReference
    recipe_sha256: str
    story_id: str
    recipe_profile_id: str
    recipe_profile_sha256: str
    render_profile_id: str
    render_profile_sha256: str
    output_timescale: int
    duration_ticks: int
    clips: tuple[RecipeTimelineClip, ...]
    schema_version: str = RECIPE_TIMELINE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.recipe_reference) is not CommittedArtifactMemberReference:  # noqa: E721
            raise RecipeTimelineError("timeline requires an exact committed Recipe reference")
        if self.recipe_reference.artifact_type != "recipe":
            raise RecipeTimelineError("timeline reference must name a Recipe artifact")
        for name in (
            "recipe_sha256",
            "story_id",
            "recipe_profile_id",
            "recipe_profile_sha256",
            "render_profile_id",
            "render_profile_sha256",
        ):
            value = getattr(self, name)
            if type(value) is not str or not value:  # noqa: E721
                raise RecipeTimelineError(f"timeline {name} must be non-empty text")
        if self.schema_version != RECIPE_TIMELINE_SCHEMA_VERSION:
            raise RecipeTimelineError("timeline schema version is unsupported")
        if type(self.output_timescale) is not int or self.output_timescale < 1:  # noqa: E721
            raise RecipeTimelineError("timeline output_timescale must be positive")
        if type(self.duration_ticks) is not int or self.duration_ticks < 1:  # noqa: E721
            raise RecipeTimelineError("timeline duration_ticks must be positive")
        if not self.clips or any(type(item) is not RecipeTimelineClip for item in self.clips):  # noqa: E721
            raise RecipeTimelineError("timeline requires non-empty exact clips")
        if tuple(item.ordinal for item in self.clips) != tuple(range(len(self.clips))):
            raise RecipeTimelineError("timeline clip ordinals are incomplete")
        if self.clips[0].output_in_tick != 0:
            raise RecipeTimelineError("timeline must begin at output tick zero")
        if any(
            earlier.output_out_tick != later.output_in_tick
            for earlier, later in zip(self.clips, self.clips[1:], strict=False)
        ) or self.clips[-1].output_out_tick != self.duration_ticks:
            raise RecipeTimelineError("timeline output clips are not contiguous")
        if self.recipe_reference.content_hash != self.recipe_sha256:
            raise RecipeTimelineError("timeline Recipe reference hash differs from Recipe content")
        if self.recipe_reference.logical_id != "production_recipe@" + self.story_id:
            raise RecipeTimelineError("timeline Recipe reference story identity differs")
        if len({item.stable_key for item in self.clips}) != len(self.clips):
            raise RecipeTimelineError("timeline contains duplicate stable clip keys")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "recipe_revision": self.recipe_reference.revision,
            "story_id": self.story_id,
            "recipe_profile_id": self.recipe_profile_id,
            "render_profile_id": self.render_profile_id,
            "output_timescale": self.output_timescale,
            "duration_ticks": self.duration_ticks,
            "clips": [item.to_mapping() for item in self.clips],
        }


@dataclass(frozen=True, slots=True)
class RecipeTimelineChange:
    kind: Literal["added", "removed", "moved", "variant_changed"]
    stable_key: tuple[str, str, str]
    base_ordinal: int | None
    target_ordinal: int | None
    base_candidate_id: str | None
    target_candidate_id: str | None

    def __post_init__(self) -> None:
        if self.kind not in ("added", "removed", "moved", "variant_changed"):
            raise RecipeTimelineError("timeline diff change kind is unsupported")
        if len(self.stable_key) != 3 or any(not item for item in self.stable_key):
            raise RecipeTimelineError("timeline diff change key is invalid")
        for name in ("base_ordinal", "target_ordinal"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 0):  # noqa: E721
                raise RecipeTimelineError(f"timeline diff {name} is invalid")

    def to_mapping(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "stable_key": list(self.stable_key),
            "base_ordinal": self.base_ordinal,
            "target_ordinal": self.target_ordinal,
            "base_candidate_id": self.base_candidate_id,
            "target_candidate_id": self.target_candidate_id,
        }


@dataclass(frozen=True, slots=True)
class RecipeDiff:
    base_recipe_reference: CommittedArtifactMemberReference
    target_recipe_reference: CommittedArtifactMemberReference
    story_id: str
    output_timescale: int
    changes: tuple[RecipeTimelineChange, ...]
    schema_version: str = RECIPE_DIFF_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.base_recipe_reference) is not CommittedArtifactMemberReference
            or type(self.target_recipe_reference) is not CommittedArtifactMemberReference
        ):  # noqa: E721
            raise RecipeTimelineError("recipe diff requires exact committed Recipe references")
        if self.schema_version != RECIPE_DIFF_SCHEMA_VERSION:
            raise RecipeTimelineError("recipe diff schema version is unsupported")
        if type(self.story_id) is not str or not self.story_id:  # noqa: E721
            raise RecipeTimelineError("recipe diff story_id must be non-empty")
        if type(self.output_timescale) is not int or self.output_timescale < 1:  # noqa: E721
            raise RecipeTimelineError("recipe diff output_timescale must be positive")
        if any(type(item) is not RecipeTimelineChange for item in self.changes):  # noqa: E721
            raise RecipeTimelineError("recipe diff changes are invalid")

    @property
    def requires_qc(self) -> bool:
        return bool(self.changes)

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "base_recipe_revision": self.base_recipe_reference.revision,
            "target_recipe_revision": self.target_recipe_reference.revision,
            "story_id": self.story_id,
            "output_timescale": self.output_timescale,
            "requires_qc": self.requires_qc,
            "changes": [item.to_mapping() for item in self.changes],
        }


def _to_timeline_time_base(value: object) -> RecipeTimelineTimeBase:
    numerator = getattr(value, "numerator", None)
    denominator = getattr(value, "denominator", None)
    return RecipeTimelineTimeBase(numerator, denominator)


def _output_ticks(duration: Fraction, *, timescale: int) -> int:
    output = duration * timescale
    if output.denominator != 1 or output.numerator < 1:
        raise RecipeTimelineError("Recipe duration cannot be represented by the output timescale")
    return output.numerator


def _spans(recipe: ProductionRecipe) -> tuple[tuple[int, ProductionSpan], ...]:
    values: list[tuple[int, ProductionSpan]] = []
    for beat in recipe.story.beats:
        for span in beat.spans:
            values.append((beat.ordinal, span))
    return tuple(values)


def project_recipe_timeline(
    recipe: ProductionRecipe,
    recipe_reference: CommittedArtifactMemberReference,
    *,
    render_profile: ProductionAvRenderProfile = PRODUCTION_AV_H264_AAC_PROFILE,
    limits: RecipeTimelineReadLimits = RecipeTimelineReadLimits(),
) -> RecipeTimeline:
    """Project one immutable Recipe into exact output and source timelines."""
    if type(recipe) is not ProductionRecipe:  # noqa: E721
        raise RecipeTimelineError("timeline projection requires an exact ProductionRecipe")
    if type(recipe_reference) is not CommittedArtifactMemberReference:  # noqa: E721
        raise RecipeTimelineError("timeline projection requires an exact Recipe reference")
    if type(render_profile) is not ProductionAvRenderProfile:  # noqa: E721
        raise RecipeTimelineError("timeline projection requires an exact render profile")
    if type(limits) is not RecipeTimelineReadLimits:  # noqa: E721
        raise RecipeTimelineError("timeline projection requires exact read limits")
    if recipe_reference.content_hash != recipe.canonical_hash:
        raise RecipeTimelineError("timeline Recipe reference content hash differs")
    if recipe_reference.logical_id != "production_recipe@" + recipe.story.story_id:
        raise RecipeTimelineError("timeline Recipe reference story differs")
    if len(recipe.story.beats) > limits.max_beats_per_recipe:
        raise RecipeTimelineError("timeline Recipe exceeds beat read limit")
    spans = _spans(recipe)
    if len(spans) > limits.max_clips_per_recipe:
        raise RecipeTimelineError("timeline Recipe exceeds clip read limit")
    plan = build_production_render_plan(recipe, profile=render_profile)
    if len(plan.segments) != len(spans):
        raise RecipeTimelineError("render plan segment census differs from Recipe spans")

    cursor = 0
    clips: list[RecipeTimelineClip] = []
    for ordinal, (segment, (beat_ordinal, span)) in enumerate(zip(plan.segments, spans, strict=True)):
        if (
            segment.ordinal != ordinal
            or segment.beat_ordinal != beat_ordinal
            or segment.span_ordinal != span.ordinal
            or segment.beat_id != span.exact_span_query.beat_id
            or segment.requirement_id != span.requirement_id
            or segment.candidate_id != span.candidate_id
        ):
            raise RecipeTimelineError("render plan segment differs from exact Recipe span")
        duration_ticks = _output_ticks(segment.video_duration, timescale=plan.output_timescale)
        output_end = cursor + duration_ticks
        clips.append(
            RecipeTimelineClip(
                ordinal=ordinal,
                beat_ordinal=segment.beat_ordinal,
                span_ordinal=segment.span_ordinal,
                input_ordinal=segment.input_ordinal,
                beat_id=segment.beat_id,
                requirement_id=segment.requirement_id,
                alternative_id=span.alternative_id,
                candidate_id=segment.candidate_id,
                output_in_tick=cursor,
                output_out_tick=output_end,
                video_clock_id=segment.video_clock_id,
                video_time_base=_to_timeline_time_base(segment.video_time_base),
                video_in_tick=segment.video_in_tick,
                video_out_tick=segment.video_out_tick,
                audio_clock_id=segment.audio_clock_id,
                audio_time_base=_to_timeline_time_base(segment.audio_time_base),
                audio_in_tick=segment.audio_in_tick,
                audio_out_tick=segment.audio_out_tick,
                exact_span_query_sha256=segment.exact_span_query_sha256,
                exact_span_result_sha256=segment.exact_span_result_sha256,
                exact_span_proof_sha256=segment.exact_span_proof_sha256,
                av_pairing_proof_sha256=segment.av_pairing_proof_sha256,
            )
        )
        cursor = output_end
    return RecipeTimeline(
        recipe_reference=recipe_reference,
        recipe_sha256=recipe.canonical_hash,
        story_id=recipe.story.story_id,
        recipe_profile_id=recipe.profile_id,
        recipe_profile_sha256=recipe.profile_sha256,
        render_profile_id=render_profile.profile_id,
        render_profile_sha256=render_profile.canonical_hash,
        output_timescale=plan.output_timescale,
        duration_ticks=cursor,
        clips=tuple(clips),
    )


def diff_recipe_timelines(base: RecipeTimeline, target: RecipeTimeline) -> RecipeDiff:
    """Compare two exact timeline projections without initiating any work."""
    if type(base) is not RecipeTimeline or type(target) is not RecipeTimeline:  # noqa: E721
        raise RecipeTimelineError("recipe diff requires exact timeline values")
    if base.story_id != target.story_id:
        raise RecipeTimelineError("RECIPE_DIFF_STORY_MISMATCH")
    if base.output_timescale != target.output_timescale:
        raise RecipeTimelineError("RECIPE_DIFF_TIME_BASE_MISMATCH")
    # Source clocks intentionally need not match: a legal variant may choose a
    # different source.  Each clip retains its own clock/time-base pair and no
    # source ticks are converted during diffing; only output-time comparisons
    # require one shared timescale.
    base_by_key = {item.stable_key: item for item in base.clips}
    target_by_key = {item.stable_key: item for item in target.clips}
    changes: list[RecipeTimelineChange] = []
    for key in sorted(set(base_by_key) | set(target_by_key)):
        before = base_by_key.get(key)
        after = target_by_key.get(key)
        if before is None:
            assert after is not None
            changes.append(RecipeTimelineChange("added", key, None, after.ordinal, None, after.candidate_id))
        elif after is None:
            changes.append(RecipeTimelineChange("removed", key, before.ordinal, None, before.candidate_id, None))
        elif (
            before.candidate_id != after.candidate_id
            or before.exact_span_result_sha256 != after.exact_span_result_sha256
        ):
            changes.append(
                RecipeTimelineChange(
                    "variant_changed", key, before.ordinal, after.ordinal,
                    before.candidate_id, after.candidate_id,
                )
            )
        elif before.ordinal != after.ordinal:
            changes.append(
                RecipeTimelineChange(
                    "moved", key, before.ordinal, after.ordinal,
                    before.candidate_id, after.candidate_id,
                )
            )
    return RecipeDiff(
        base.recipe_reference,
        target.recipe_reference,
        base.story_id,
        base.output_timescale,
        tuple(changes),
    )


__all__ = (
    "RECIPE_DIFF_SCHEMA_VERSION",
    "RECIPE_TIMELINE_SCHEMA_VERSION",
    "RecipeDiff",
    "RecipeTimeline",
    "RecipeTimelineChange",
    "RecipeTimelineClip",
    "RecipeTimelineError",
    "RecipeTimelineReadLimits",
    "RecipeTimelineTimeBase",
    "diff_recipe_timelines",
    "project_recipe_timeline",
)
