"""Selected native audio layout, independently versioned from presentation evidence.

The hashes bind normalized records, not proof that a tool executed or a Store
accepted them. In particular, the historical presentation output hash does not
cover sample rate or channel count; the selected-metadata hash below does.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from .root_evidence import AudioSampleBoundarySet, AudioSourceOutcome, CoverageOutcome
from .stage4_predecessor import PresentationTimelineProbe
from .types import MediaValidationError, TimeBase, canonical_sha256, sha256_prefixed


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value:  # noqa: E721
        raise MediaValidationError(f"{name} must be nonempty text")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise MediaValidationError(f"{name} must be strict UTF-8") from error
    return value


def _integer(value: object, name: str, minimum: int | None = None) -> int:
    if type(value) is not int or (minimum is not None and value < minimum):  # noqa: E721
        raise MediaValidationError(f"{name} must be an integer >= {minimum}")
    return value


def _hash(value: object, name: str) -> str:
    return sha256_prefixed(_text(value, name), name)


def _closed(value: object, keys: set[str], name: str) -> dict[str, object]:
    if type(value) is not dict:  # noqa: E721
        raise MediaValidationError(f"{name} must be a closed object")
    raw = cast(dict[object, object], value)
    if any(type(key) is not str for key in raw) or set(raw) != keys:  # noqa: E721
        raise MediaValidationError(f"{name} must have exactly the declared fields")
    return cast(dict[str, object], raw)


def _time_base(value: object) -> TimeBase:
    raw = _closed(value, {"numerator", "denominator"}, "time_base")
    return TimeBase(
        _integer(raw["numerator"], "numerator", 1),
        _integer(raw["denominator"], "denominator", 1),
    )


@dataclass(frozen=True, slots=True)
class SelectedAudioStreamMetadata:
    """Normalized selected ffprobe stream fields; never inferred from a clock."""

    stream_index: int
    time_base: TimeBase
    declared_start_tick: int
    sample_rate: int
    channels: int

    def __post_init__(self) -> None:
        _integer(self.stream_index, "stream_index", 0)
        if type(self.time_base) is not TimeBase:
            raise MediaValidationError("time_base must be an exact TimeBase")
        _integer(self.declared_start_tick, "declared_start_tick")
        _integer(self.sample_rate, "sample_rate", 1)
        _integer(self.channels, "channels", 1)

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": "selected-audio-stream-metadata-v1",
            "codec_type": "audio",
            "stream_index": self.stream_index,
            "time_base": {
                "numerator": self.time_base.numerator,
                "denominator": self.time_base.denominator,
            },
            "declared_start_tick": self.declared_start_tick,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class AudioStreamFacts:
    source_id: str
    source_sha256: str
    stream_index: int
    clock_id: str
    time_base: TimeBase
    origin_tick: int
    end_tick: int
    sample_rate: int
    channels: int
    audio_sample_boundary_set_sha256: str
    selected_audio_metadata: SelectedAudioStreamMetadata
    selected_audio_metadata_sha256: str
    probe_execution_sha256: str

    def __post_init__(self) -> None:
        _text(self.source_id, "source_id")
        _hash(self.source_sha256, "source_sha256")
        _integer(self.stream_index, "stream_index", 0)
        _text(self.clock_id, "clock_id")
        if self.clock_id != f"audio-stream-{self.stream_index}":
            raise MediaValidationError("audio clock must identify the selected stream")
        if type(self.time_base) is not TimeBase:
            raise MediaValidationError("time_base must be an exact TimeBase")
        _integer(self.origin_tick, "origin_tick")
        _integer(self.end_tick, "end_tick")
        if self.end_tick <= self.origin_tick:
            raise MediaValidationError("audio stream range must be nonempty")
        _integer(self.sample_rate, "sample_rate", 1)
        _integer(self.channels, "channels", 1)
        _hash(self.audio_sample_boundary_set_sha256, "audio_sample_boundary_set_sha256")
        _hash(self.probe_execution_sha256, "probe_execution_sha256")
        _hash(self.selected_audio_metadata_sha256, "selected_audio_metadata_sha256")
        metadata = self.selected_audio_metadata
        if type(metadata) is not SelectedAudioStreamMetadata:
            raise MediaValidationError("selected_audio_metadata must be exact typed metadata")
        if (
            metadata.stream_index != self.stream_index
            or metadata.time_base != self.time_base
            or metadata.declared_start_tick != self.origin_tick
            or metadata.sample_rate != self.sample_rate
            or metadata.channels != self.channels
            or metadata.canonical_hash != self.selected_audio_metadata_sha256
        ):
            raise MediaValidationError("selected audio metadata does not bind the stream facts")

    def assert_matches(
        self, probe: PresentationTimelineProbe, audio: AudioSampleBoundarySet
    ) -> None:
        """Check exact record consistency, not committed/native-execution authority."""
        if (
            type(probe) is not PresentationTimelineProbe
            or type(audio) is not AudioSampleBoundarySet
        ):
            raise MediaValidationError("audio facts require exact presentation/audio evidence")
        track, context = probe.audio, audio.context
        if (
            self.source_id != probe.source_id
            or self.source_id != context.source_id
            or self.source_sha256 != probe.source_sha256
            or self.source_sha256 != context.source_sha256
            or self.source_sha256 != probe.probe_execution.source_input_sha256
            or self.stream_index != track.stream_index
            or self.clock_id != track.clock_id
            or self.clock_id != context.clock_id
            or self.time_base != track.time_base
            or self.time_base != context.time_base
            or self.origin_tick != track.origin_tick
            or self.origin_tick != context.origin_tick
            or self.end_tick != track.end_tick
            or self.end_tick != context.end_tick
            or self.audio_sample_boundary_set_sha256 != audio.canonical_hash
            or self.audio_sample_boundary_set_sha256 != probe.audio_sample_boundary_set_sha256
            or self.probe_execution_sha256 != canonical_sha256(probe.probe_execution.to_mapping())
            or audio.coverage.outcome is not CoverageOutcome.COMPLETE
            or audio.source_outcome is not AudioSourceOutcome.BOUNDARIES_AVAILABLE
        ):
            raise MediaValidationError("audio stream facts do not match exact probe evidence")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": "audio-stream-facts-v1",
            "source_id": self.source_id,
            "source_sha256": self.source_sha256,
            "stream_index": self.stream_index,
            "clock_id": self.clock_id,
            "time_base": {
                "numerator": self.time_base.numerator,
                "denominator": self.time_base.denominator,
            },
            "origin_tick": self.origin_tick,
            "end_tick": self.end_tick,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "audio_sample_boundary_set_sha256": self.audio_sample_boundary_set_sha256,
            "selected_audio_metadata": self.selected_audio_metadata.to_mapping(),
            "selected_audio_metadata_sha256": self.selected_audio_metadata_sha256,
            "probe_execution_sha256": self.probe_execution_sha256,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


def decode_audio_stream_facts(value: object) -> AudioStreamFacts:
    raw = _closed(
        value,
        {
            "schema_version",
            "source_id",
            "source_sha256",
            "stream_index",
            "clock_id",
            "time_base",
            "origin_tick",
            "end_tick",
            "sample_rate",
            "channels",
            "audio_sample_boundary_set_sha256",
            "selected_audio_metadata",
            "selected_audio_metadata_sha256",
            "probe_execution_sha256",
        },
        "audio_stream_facts",
    )
    if _text(raw["schema_version"], "schema_version") != "audio-stream-facts-v1":
        raise MediaValidationError("unsupported audio stream facts schema")
    metadata = _closed(
        raw["selected_audio_metadata"],
        {
            "schema_version",
            "codec_type",
            "stream_index",
            "time_base",
            "declared_start_tick",
            "sample_rate",
            "channels",
        },
        "selected_audio_metadata",
    )
    if (
        _text(metadata["schema_version"], "metadata.schema_version")
        != "selected-audio-stream-metadata-v1"
        or _text(metadata["codec_type"], "codec_type") != "audio"
    ):
        raise MediaValidationError("unsupported selected audio metadata schema or codec type")
    result = AudioStreamFacts(
        _text(raw["source_id"], "source_id"),
        _text(raw["source_sha256"], "source_sha256"),
        _integer(raw["stream_index"], "stream_index", 0),
        _text(raw["clock_id"], "clock_id"),
        _time_base(raw["time_base"]),
        _integer(raw["origin_tick"], "origin_tick"),
        _integer(raw["end_tick"], "end_tick"),
        _integer(raw["sample_rate"], "sample_rate", 1),
        _integer(raw["channels"], "channels", 1),
        _text(raw["audio_sample_boundary_set_sha256"], "audio_sample_boundary_set_sha256"),
        SelectedAudioStreamMetadata(
            _integer(metadata["stream_index"], "metadata.stream_index", 0),
            _time_base(metadata["time_base"]),
            _integer(metadata["declared_start_tick"], "declared_start_tick"),
            _integer(metadata["sample_rate"], "metadata.sample_rate", 1),
            _integer(metadata["channels"], "metadata.channels", 1),
        ),
        _text(raw["selected_audio_metadata_sha256"], "selected_audio_metadata_sha256"),
        _text(raw["probe_execution_sha256"], "probe_execution_sha256"),
    )
    if result.to_mapping() != raw:
        raise MediaValidationError("audio stream facts must be canonical")
    return result
