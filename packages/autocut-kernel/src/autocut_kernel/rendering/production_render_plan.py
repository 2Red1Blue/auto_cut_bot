"""Pure production Recipe to deterministic FFmpeg render-plan projection.

This module deliberately stops before file materialization and process
execution.  A logical plan contains immutable BlobRefs and exact source ticks;
host paths are bound only when constructing an invocation.  Consequently the
same admitted Recipe has one plan identity on macOS, WSL, and a worker host.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from fractions import Fraction
from math import lcm
from pathlib import Path
from typing import Final

from ..media.types import TimeBase, canonical_sha256, require_pts, sha256_prefixed
from ..pipeline.production_recipe import ProductionRecipe, ProductionSpan
from ..store.models import BlobRef, CommittedArtifactMemberReference

PRODUCTION_RENDER_PLAN_SCHEMA_VERSION: Final = "production-render-plan-v1"
PRODUCTION_RENDER_PROFILE_ID: Final = "production-av-h264-aac-v1"


class ProductionRenderPlanError(ValueError):
    """The production Recipe cannot be projected into the closed render plan."""


def _positive_integer(value: object, label: str) -> int:
    try:
        result = require_pts(value, label)
    except ValueError as error:
        raise ProductionRenderPlanError(str(error)) from error
    if result <= 0:
        raise ProductionRenderPlanError(f"{label} must be positive")
    return result


def _nonempty_text(value: object, label: str) -> str:
    if type(value) is not str or not value.strip():  # noqa: E721
        raise ProductionRenderPlanError(f"{label} must be non-empty text")
    return value


def _blob_mapping(reference: BlobRef) -> dict[str, object]:
    return {
        "object_id": str(reference.object_id),
        "content_hash": reference.content_hash,
        "byte_length": reference.byte_length,
        "media_type": reference.media_type,
    }


def _time_base_mapping(value: TimeBase) -> dict[str, int]:
    return {"numerator": value.numerator, "denominator": value.denominator}


def _absolute_path(value: object, label: str) -> Path:
    if not isinstance(value, Path) or not value.is_absolute():
        raise ProductionRenderPlanError(
            f"production render {label} must be an absolute pathlib.Path value"
        )
    return value


@dataclass(frozen=True, slots=True)
class ProductionAvRenderProfile:
    """Closed output and encoder policy for the first production A/V renderer."""

    profile_id: str = PRODUCTION_RENDER_PROFILE_ID
    width: int = 720
    height: int = 1280
    video_codec: str = "libx264"
    pixel_format: str = "yuv420p"
    preset: str = "medium"
    crf: int = 18
    keyframe_interval: int = 48
    audio_codec: str = "aac"
    audio_sample_rate: int = 48_000
    audio_channel_layout: str = "stereo"
    audio_bitrate: str = "192k"
    output_timescale: int = 90_000
    max_output_timescale: int = 2_147_483_647
    av_duration_policy: str = "exact"

    def __post_init__(self) -> None:
        _nonempty_text(self.profile_id, "production render profile_id")
        width = _positive_integer(self.width, "production render width")
        height = _positive_integer(self.height, "production render height")
        if width % 2 or height % 2:
            raise ProductionRenderPlanError("production render dimensions must be even")
        if self.video_codec != "libx264" or self.pixel_format != "yuv420p":
            raise ProductionRenderPlanError("production render video codec profile is unsupported")
        _nonempty_text(self.preset, "production render preset")
        if type(self.crf) is not int or self.crf < 0 or self.crf > 51:  # noqa: E721
            raise ProductionRenderPlanError("production render crf must be an integer in 0..51")
        _positive_integer(self.keyframe_interval, "production render keyframe_interval")
        if self.audio_codec != "aac":
            raise ProductionRenderPlanError("production render audio codec is unsupported")
        _positive_integer(self.audio_sample_rate, "production render audio_sample_rate")
        if self.audio_channel_layout != "stereo":
            raise ProductionRenderPlanError("production render channel layout is unsupported")
        _nonempty_text(self.audio_bitrate, "production render audio_bitrate")
        output_timescale = _positive_integer(
            self.output_timescale, "production render output_timescale"
        )
        maximum = _positive_integer(
            self.max_output_timescale, "production render max_output_timescale"
        )
        if maximum < output_timescale:
            raise ProductionRenderPlanError(
                "production render max_output_timescale must cover output_timescale"
            )
        if self.av_duration_policy != "exact":
            raise ProductionRenderPlanError(
                "the first production renderer requires exact A/V segment durations"
            )

    def to_mapping(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "width": self.width,
            "height": self.height,
            "video_codec": self.video_codec,
            "pixel_format": self.pixel_format,
            "preset": self.preset,
            "crf": self.crf,
            "keyframe_interval": self.keyframe_interval,
            "audio_codec": self.audio_codec,
            "audio_sample_rate": self.audio_sample_rate,
            "audio_channel_layout": self.audio_channel_layout,
            "audio_bitrate": self.audio_bitrate,
            "output_timescale": self.output_timescale,
            "max_output_timescale": self.max_output_timescale,
            "av_duration_policy": self.av_duration_policy,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


PRODUCTION_AV_H264_AAC_PROFILE: Final = ProductionAvRenderProfile()


@dataclass(frozen=True, slots=True)
class ProductionRenderInput:
    """One first-occurrence-ordered immutable source input."""

    ordinal: int
    source_blob: BlobRef
    source_manifest_ref: CommittedArtifactMemberReference

    def __post_init__(self) -> None:
        if type(self.ordinal) is not int or self.ordinal < 0:  # noqa: E721
            raise ProductionRenderPlanError("production render input ordinal is invalid")
        if type(self.source_blob) is not BlobRef:  # noqa: E721
            raise ProductionRenderPlanError("production render input requires an exact BlobRef")
        if not self.source_blob.media_type.startswith("video/"):
            raise ProductionRenderPlanError("production render input must be video media")
        if type(self.source_manifest_ref) is not CommittedArtifactMemberReference:  # noqa: E721
            raise ProductionRenderPlanError(
                "production render input requires an exact SourceManifest reference"
            )

    def to_mapping(self) -> dict[str, object]:
        return {
            "ordinal": self.ordinal,
            "source_blob": _blob_mapping(self.source_blob),
            "source_manifest_ref": self.source_manifest_ref.to_mapping(),
        }


@dataclass(frozen=True, slots=True)
class ProductionRenderSegment:
    """One ordered exact A/V source span projected for FFmpeg."""

    ordinal: int
    beat_ordinal: int
    span_ordinal: int
    input_ordinal: int
    beat_id: str
    requirement_id: str
    candidate_id: str
    source_id: str
    video_clock_id: str
    video_time_base: TimeBase
    video_in_tick: int
    video_out_tick: int
    audio_time_base: TimeBase
    audio_clock_id: str
    audio_in_tick: int
    audio_out_tick: int
    exact_span_query_sha256: str
    exact_span_result_sha256: str
    exact_span_proof_sha256: str
    av_pairing_proof_sha256: str

    def __post_init__(self) -> None:
        for label, value in (
            ("segment ordinal", self.ordinal),
            ("segment beat_ordinal", self.beat_ordinal),
            ("segment span_ordinal", self.span_ordinal),
            ("segment input_ordinal", self.input_ordinal),
        ):
            if type(value) is not int or value < 0:  # noqa: E721
                raise ProductionRenderPlanError(f"production render {label} is invalid")
        if type(self.video_time_base) is not TimeBase or type(self.audio_time_base) is not TimeBase:  # noqa: E721
            raise ProductionRenderPlanError("production render segment requires exact A/V clocks")
        _nonempty_text(self.beat_id, "production render segment beat_id")
        _nonempty_text(self.requirement_id, "production render segment requirement_id")
        _nonempty_text(self.candidate_id, "production render segment candidate_id")
        _nonempty_text(self.source_id, "production render segment source_id")
        _nonempty_text(self.video_clock_id, "production render segment video_clock_id")
        _nonempty_text(self.audio_clock_id, "production render segment audio_clock_id")
        for label, value in (
            ("video_in_tick", self.video_in_tick),
            ("video_out_tick", self.video_out_tick),
            ("audio_in_tick", self.audio_in_tick),
            ("audio_out_tick", self.audio_out_tick),
        ):
            try:
                require_pts(value, f"production render segment {label}")
            except ValueError as error:
                raise ProductionRenderPlanError(str(error)) from error
        if self.video_in_tick >= self.video_out_tick or self.audio_in_tick >= self.audio_out_tick:
            raise ProductionRenderPlanError("production render segment ranges must be non-empty")
        for label, value in (
            ("exact_span_query_sha256", self.exact_span_query_sha256),
            ("exact_span_result_sha256", self.exact_span_result_sha256),
            ("exact_span_proof_sha256", self.exact_span_proof_sha256),
            ("av_pairing_proof_sha256", self.av_pairing_proof_sha256),
        ):
            try:
                sha256_prefixed(value, f"production render segment {label}")
            except ValueError as error:
                raise ProductionRenderPlanError(str(error)) from error
        if self.video_duration != self.audio_duration:
            raise ProductionRenderPlanError(
                "production render segment A/V durations differ; an explicit calibrated "
                "reconciliation policy is required"
            )

    @property
    def video_duration(self) -> Fraction:
        return Fraction(
            (self.video_out_tick - self.video_in_tick) * self.video_time_base.numerator,
            self.video_time_base.denominator,
        )

    @property
    def audio_duration(self) -> Fraction:
        return Fraction(
            (self.audio_out_tick - self.audio_in_tick) * self.audio_time_base.numerator,
            self.audio_time_base.denominator,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "ordinal": self.ordinal,
            "beat_ordinal": self.beat_ordinal,
            "span_ordinal": self.span_ordinal,
            "input_ordinal": self.input_ordinal,
            "beat_id": self.beat_id,
            "requirement_id": self.requirement_id,
            "candidate_id": self.candidate_id,
            "source_id": self.source_id,
            "video_clock_id": self.video_clock_id,
            "video_time_base": _time_base_mapping(self.video_time_base),
            "video_in_tick": self.video_in_tick,
            "video_out_tick": self.video_out_tick,
            "audio_time_base": _time_base_mapping(self.audio_time_base),
            "audio_clock_id": self.audio_clock_id,
            "audio_in_tick": self.audio_in_tick,
            "audio_out_tick": self.audio_out_tick,
            "exact_span_query_sha256": self.exact_span_query_sha256,
            "exact_span_result_sha256": self.exact_span_result_sha256,
            "exact_span_proof_sha256": self.exact_span_proof_sha256,
            "av_pairing_proof_sha256": self.av_pairing_proof_sha256,
        }


@dataclass(frozen=True, slots=True)
class ProductionRenderPlan:
    """Path-independent logical render graph derived from one production Recipe."""

    recipe_sha256: str
    profile_sha256: str
    story_id: str
    blueprint_story_sha256: str
    inputs: tuple[ProductionRenderInput, ...]
    segments: tuple[ProductionRenderSegment, ...]
    output_timescale: int
    filter_graph: str

    def __post_init__(self) -> None:
        for label, value in (
            ("recipe_sha256", self.recipe_sha256),
            ("profile_sha256", self.profile_sha256),
            ("blueprint_story_sha256", self.blueprint_story_sha256),
        ):
            try:
                sha256_prefixed(value, f"production render plan {label}")
            except ValueError as error:
                raise ProductionRenderPlanError(str(error)) from error
        _nonempty_text(self.story_id, "production render plan story_id")
        if not self.inputs or not self.segments:
            raise ProductionRenderPlanError("production render plan must be non-empty")
        if tuple(item.ordinal for item in self.inputs) != tuple(range(len(self.inputs))):
            raise ProductionRenderPlanError("production render input ordinals are not complete")
        if tuple(item.ordinal for item in self.segments) != tuple(range(len(self.segments))):
            raise ProductionRenderPlanError("production render segment ordinals are not complete")
        if any(item.input_ordinal >= len(self.inputs) for item in self.segments):
            raise ProductionRenderPlanError("production render segment references an unknown input")
        _positive_integer(self.output_timescale, "production render plan output_timescale")
        _nonempty_text(self.filter_graph, "production render plan filter_graph")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": PRODUCTION_RENDER_PLAN_SCHEMA_VERSION,
            "recipe_sha256": self.recipe_sha256,
            "profile_sha256": self.profile_sha256,
            "story_id": self.story_id,
            "blueprint_story_sha256": self.blueprint_story_sha256,
            "inputs": [item.to_mapping() for item in self.inputs],
            "segments": [item.to_mapping() for item in self.segments],
            "output_timescale": self.output_timescale,
            "filter_graph": self.filter_graph,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class ProductionRenderInvocation:
    """Host-bound shell-free FFmpeg argv for one logical production plan."""

    plan_sha256: str
    argv: tuple[str, ...]

    def __post_init__(self) -> None:
        try:
            sha256_prefixed(self.plan_sha256, "production render invocation plan_sha256")
        except ValueError as error:
            raise ProductionRenderPlanError(str(error)) from error
        if not self.argv or self.argv[0] != "ffmpeg":
            raise ProductionRenderPlanError("production render invocation must call ffmpeg")
        if any(item in self.argv for item in ("-ss", "-to", "-c", "copy")):
            raise ProductionRenderPlanError(
                "production render invocation cannot seek or stream-copy"
            )


def _source_input_ordinal(
    span: ProductionSpan,
    inputs: list[ProductionRenderInput],
    by_object_id: dict[object, int],
) -> int:
    existing = by_object_id.get(span.source_blob.object_id)
    if existing is not None:
        if (
            inputs[existing].source_blob != span.source_blob
            or inputs[existing].source_manifest_ref != span.source_manifest_ref
        ):
            raise ProductionRenderPlanError(
                "one immutable source object_id resolves to conflicting authority"
            )
        return existing
    ordinal = len(inputs)
    inputs.append(ProductionRenderInput(ordinal, span.source_blob, span.source_manifest_ref))
    by_object_id[span.source_blob.object_id] = ordinal
    return ordinal


def _segment(
    *,
    ordinal: int,
    beat_ordinal: int,
    span: ProductionSpan,
    input_ordinal: int,
) -> ProductionRenderSegment:
    proof = span.exact_span_result.boundary_proof
    result = ProductionRenderSegment(
        ordinal=ordinal,
        beat_ordinal=beat_ordinal,
        span_ordinal=span.ordinal,
        input_ordinal=input_ordinal,
        beat_id=span.exact_span_query.beat_id,
        requirement_id=span.requirement_id,
        candidate_id=span.candidate_id,
        source_id=proof.source_id,
        video_clock_id=proof.video_clock_id,
        video_time_base=proof.video_time_base,
        video_in_tick=proof.video_in_tick,
        video_out_tick=proof.video_out_tick,
        audio_time_base=proof.audio_time_base,
        audio_clock_id=proof.audio_clock_id,
        audio_in_tick=proof.audio_in_tick,
        audio_out_tick=proof.audio_out_tick,
        exact_span_query_sha256=span.exact_span_query_sha256,
        exact_span_result_sha256=span.exact_span_result_sha256,
        exact_span_proof_sha256=span.exact_span_proof_sha256,
        av_pairing_proof_sha256=span.av_pairing_proof_sha256,
    )
    return result


def _output_timescale(
    segments: tuple[ProductionRenderSegment, ...], profile: ProductionAvRenderProfile
) -> int:
    value = profile.output_timescale
    for segment in segments:
        value = lcm(value, segment.video_time_base.denominator)
        if value > profile.max_output_timescale:
            raise ProductionRenderPlanError(
                "source video clocks cannot be represented by the render profile timescale"
            )
    return value


def _require_profile_timing(
    segments: tuple[ProductionRenderSegment, ...], profile: ProductionAvRenderProfile
) -> None:
    for segment in segments:
        output_sample_count = segment.audio_duration * profile.audio_sample_rate
        if output_sample_count.denominator != 1:
            raise ProductionRenderPlanError(
                "production render segment duration cannot be represented by an exact integer "
                "number of output audio samples"
            )


def _filter_graph(
    segments: tuple[ProductionRenderSegment, ...], profile: ProductionAvRenderProfile
) -> str:
    filters: list[str] = []
    for segment in segments:
        video = (
            f"[{segment.input_ordinal}:v:0]"
            f"trim=start_pts={segment.video_in_tick}:end_pts={segment.video_out_tick},"
            "setpts=PTS-STARTPTS,"
            f"scale=w={profile.width}:h={profile.height}:"
            "force_original_aspect_ratio=decrease:force_divisible_by=2,"
            f"pad=width={profile.width}:height={profile.height}:"
            "x=(ow-iw)/2:y=(oh-ih)/2:color=black,"
            "setsar=1,"
            f"format=pix_fmts={profile.pixel_format}[v{segment.ordinal}]"
        )
        audio = (
            f"[{segment.input_ordinal}:a:0]"
            f"atrim=start_pts={segment.audio_in_tick}:end_pts={segment.audio_out_tick},"
            "asetpts=PTS-STARTPTS,"
            f"aformat=sample_fmts=fltp:sample_rates={profile.audio_sample_rate}:"
            f"channel_layouts={profile.audio_channel_layout}[a{segment.ordinal}]"
        )
        filters.extend((video, audio))
    if len(segments) > 1:
        inputs = "".join(f"[v{item.ordinal}][a{item.ordinal}]" for item in segments)
        filters.append(f"{inputs}concat=n={len(segments)}:v=1:a=1[vout][aout]")
    return ";".join(filters)


def build_production_render_plan(
    recipe: ProductionRecipe,
    *,
    profile: ProductionAvRenderProfile = PRODUCTION_AV_H264_AAC_PROFILE,
) -> ProductionRenderPlan:
    """Project an admitted production Recipe into one path-independent render graph."""
    if type(recipe) is not ProductionRecipe:  # noqa: E721
        raise ProductionRenderPlanError("production render requires a ProductionRecipe")
    if type(profile) is not ProductionAvRenderProfile:  # noqa: E721
        raise ProductionRenderPlanError("production render requires a closed render profile")
    inputs: list[ProductionRenderInput] = []
    by_object_id: dict[object, int] = {}
    segments: list[ProductionRenderSegment] = []
    for beat in recipe.story.beats:
        for span in beat.spans:
            input_ordinal = _source_input_ordinal(span, inputs, by_object_id)
            segments.append(
                _segment(
                    ordinal=len(segments),
                    beat_ordinal=beat.ordinal,
                    span=span,
                    input_ordinal=input_ordinal,
                )
            )
    frozen_segments = tuple(segments)
    _require_profile_timing(frozen_segments, profile)
    return ProductionRenderPlan(
        recipe_sha256=recipe.canonical_hash,
        profile_sha256=profile.canonical_hash,
        story_id=recipe.story.story_id,
        blueprint_story_sha256=recipe.story.blueprint_story_sha256,
        inputs=tuple(inputs),
        segments=frozen_segments,
        output_timescale=_output_timescale(frozen_segments, profile),
        filter_graph=_filter_graph(frozen_segments, profile),
    )


def bind_production_render_invocation(
    plan: ProductionRenderPlan,
    *,
    source_paths: Mapping[BlobRef, Path],
    output_path: Path,
    profile: ProductionAvRenderProfile = PRODUCTION_AV_H264_AAC_PROFILE,
) -> ProductionRenderInvocation:
    """Bind verified materialized paths without changing the logical plan identity."""
    if type(plan) is not ProductionRenderPlan:  # noqa: E721
        raise ProductionRenderPlanError("production render binding requires a logical plan")
    if (
        type(profile) is not ProductionAvRenderProfile
        or profile.canonical_hash != plan.profile_sha256
    ):  # noqa: E721
        raise ProductionRenderPlanError("production render profile differs from the logical plan")
    if plan.filter_graph != _filter_graph(plan.segments, profile):
        raise ProductionRenderPlanError("production render filter graph is not trusted")
    if plan.output_timescale != _output_timescale(plan.segments, profile):
        raise ProductionRenderPlanError("production render output timescale is not trusted")
    _require_profile_timing(plan.segments, profile)
    expected = tuple(item.source_blob for item in plan.inputs)
    if len(source_paths) != len(expected) or set(source_paths) != set(expected):
        raise ProductionRenderPlanError(
            "production render source paths must bind every exact input BlobRef and no others"
        )
    resolved_paths: list[Path] = []
    for reference in expected:
        resolved_paths.append(_absolute_path(source_paths[reference], "source path"))
    resolved_output = _absolute_path(output_path, "output_path")
    argv: list[str] = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-copyts"]
    for source_path in resolved_paths:
        argv.extend(("-i", str(source_path)))
    video_label = "[vout]" if len(plan.segments) > 1 else "[v0]"
    audio_label = "[aout]" if len(plan.segments) > 1 else "[a0]"
    argv.extend(
        (
            "-filter_complex",
            plan.filter_graph,
            "-map",
            video_label,
            "-map",
            audio_label,
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-c:v",
            profile.video_codec,
            "-pix_fmt",
            profile.pixel_format,
            "-preset",
            profile.preset,
            "-crf",
            str(profile.crf),
            "-g",
            str(profile.keyframe_interval),
            "-fps_mode",
            "vfr",
            "-video_track_timescale",
            str(plan.output_timescale),
            "-c:a",
            profile.audio_codec,
            "-ar",
            str(profile.audio_sample_rate),
            "-ac",
            "2",
            "-b:a",
            profile.audio_bitrate,
            "-movflags",
            "+faststart",
            "-f",
            "mp4",
            str(resolved_output),
        )
    )
    return ProductionRenderInvocation(plan.canonical_hash, tuple(argv))


__all__ = (
    "PRODUCTION_AV_H264_AAC_PROFILE",
    "PRODUCTION_RENDER_PLAN_SCHEMA_VERSION",
    "PRODUCTION_RENDER_PROFILE_ID",
    "ProductionAvRenderProfile",
    "ProductionRenderInput",
    "ProductionRenderInvocation",
    "ProductionRenderPlan",
    "ProductionRenderPlanError",
    "ProductionRenderSegment",
    "bind_production_render_invocation",
    "build_production_render_plan",
)
