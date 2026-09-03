"""Pure, deterministic full-file production-QC collector contracts.

This module intentionally has no persistence, subprocess, filesystem, or policy
imports.  A later runner supplies bytes to the online reducers and owns process
execution, materialization, leases, and evidence attachment.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Final, Literal, Protocol, cast

_MAX_TOPOLOGY_BYTES: Final = 1024 * 1024
_MAX_RECORD_BYTES: Final = 64 * 1024
_EXAMPLE_CAP: Final = 8
_OBSERVATION_EXAMPLE_CAP: Final = _EXAMPLE_CAP * 2
_SHA256_PATTERN: Final = re.compile(r"sha256:[0-9a-f]{64}\Z")
_EMPTY_SHA256: Final = "sha256:" + hashlib.sha256(b"").hexdigest()
_INTEGER_PATTERN: Final = re.compile(r"-?(?:0|[1-9][0-9]*)\Z")
_DECIMAL_PATTERN: Final = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]*[1-9])?\Z")
_TIMESTAMP_DECIMAL_PATTERN: Final = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")

CollectionStatus = Literal["completed", "incomplete", "not_run", "not_applicable"]
Coverage = Literal["full_file", "partial", "none", "not_applicable"]
MeasurementKind = Literal["integer", "decimal", "rational", "boolean", "text", "sha256"]
MeasurementUnit = Literal[
    "none", "count", "byte", "tick", "second", "frame", "sample", "packet", "stream",
    "channel", "hertz", "decibel", "lufs", "percent", "ratio",
]

COLLECTOR_CHECK_SCHEMA_VERSION: Final = "production-av-qc-v1"


class CollectorError(ValueError):
    """A closed parser, schema, or collection-observation error."""


def _sha256(value: object, label: str) -> str:
    if type(value) is not str or _SHA256_PATTERN.fullmatch(value) is None:  # noqa: E721
        raise CollectorError(f"{label} must be a lowercase sha256 identity")
    return value


def _identifier(value: object, label: str) -> str:
    if type(value) is not str or re.fullmatch(r"[a-z][a-z0-9_]*", value) is None:  # noqa: E721
        raise CollectorError(f"{label} must be a lowercase identifier")
    return value


def _integer(value: object, label: str, *, nonnegative: bool = False) -> int:
    if type(value) is not int or isinstance(value, bool):  # noqa: E721
        raise CollectorError(f"{label} must be an integer")
    if nonnegative and value < 0:
        raise CollectorError(f"{label} must be non-negative")
    return value


def _canonical_rational(raw: str) -> str:
    if type(raw) is not str or not raw or raw in {"N/A", "nan", "inf", "+inf", "-inf"}:  # noqa: E721
        raise CollectorError("timestamp must be a finite exact decimal or rational")
    try:
        if "/" in raw:
            numerator, denominator = raw.split("/", 1)
            if _INTEGER_PATTERN.fullmatch(numerator) is None or _INTEGER_PATTERN.fullmatch(denominator) is None:
                raise ValueError
            result = Fraction(int(numerator), int(denominator))
        else:
            if _TIMESTAMP_DECIMAL_PATTERN.fullmatch(raw) is None:
                raise ValueError
            result = Fraction(Decimal(raw))
    except (ArithmeticError, InvalidOperation, ValueError, ZeroDivisionError) as error:
        raise CollectorError("timestamp must be a finite exact decimal or rational") from error
    canonical = f"{result.numerator}/{result.denominator}"
    if "/" in raw and raw != canonical:
        raise CollectorError("rational timestamp is not reduced")
    if result == 0 and raw.startswith("-"):
        raise CollectorError("timestamp may not be negative zero")
    return canonical


def _canonical_decimal(raw: str) -> str:
    if type(raw) is not str or _DECIMAL_PATTERN.fullmatch(raw) is None:  # noqa: E721
        raise CollectorError("decimal must be finite, non-exponent, and without trailing zeroes")
    if raw == "-0":
        raise CollectorError("decimal may not be negative zero")
    return raw


def parse_rational_timestamp(raw: str) -> str:
    """Return an exact reduced rational; never float-round a tool timestamp."""

    return _canonical_rational(raw)


def parse_astats_value(raw: str) -> str:
    """Normalize astats scalar output, preserving non-finite sentinel observations."""

    if type(raw) is not str:  # noqa: E721
        raise CollectorError("astats value must be text")
    normalized = raw.strip().lower()
    if normalized in {"nan", "inf", "+inf", "-inf"}:
        return "inf" if normalized == "+inf" else normalized
    # Unlike a timeline endpoint, an astats scalar is not a signed temporal
    # coordinate.  FFmpeg legitimately prints values such as ``-0.000000``
    # (for example Entropy on silent PCM).  Preserve the raw stream hash as
    # evidence, but normalize this numerically equivalent observation to zero
    # instead of rejecting an otherwise complete scan.
    if _TIMESTAMP_DECIMAL_PATTERN.fullmatch(normalized) is None:
        raise CollectorError("astats value must be finite decimal text")
    try:
        value = Fraction(Decimal(normalized))
    except (ArithmeticError, InvalidOperation, ValueError, ZeroDivisionError) as error:
        raise CollectorError("astats value must be finite decimal text") from error
    return f"{value.numerator}/{value.denominator}"


@dataclass(frozen=True, slots=True)
class MeasurementSpec:
    name: str
    value_kind: MeasurementKind
    unit: MeasurementUnit

    def __post_init__(self) -> None:
        _identifier(self.name, "measurement name")


@dataclass(frozen=True, slots=True)
class Measurement:
    """A scalar objective observation in the Store-compatible closed shape."""

    name: str
    value_kind: MeasurementKind
    value: str
    unit: MeasurementUnit

    def __post_init__(self) -> None:
        _identifier(self.name, "measurement name")
        if self.value_kind not in {"integer", "decimal", "rational", "boolean", "text", "sha256"}:
            raise CollectorError("measurement value kind is unsupported")
        if self.unit not in {
            "none", "count", "byte", "tick", "second", "frame", "sample", "packet", "stream",
            "channel", "hertz", "decibel", "lufs", "percent", "ratio",
        }:
            raise CollectorError("measurement unit is unsupported")
        if type(self.value) is not str or not self.value:  # noqa: E721
            raise CollectorError("measurement value must be nonempty text")
        if self.value_kind == "integer" and _INTEGER_PATTERN.fullmatch(self.value) is None:
            raise CollectorError("integer measurement is not canonical")
        if self.value_kind == "decimal":
            _canonical_decimal(self.value)
        if self.value_kind == "boolean" and self.value not in {"true", "false"}:
            raise CollectorError("boolean measurement is not canonical")
        if self.value_kind == "sha256":
            _sha256(self.value, "sha256 measurement")
        if self.value_kind == "rational":
            if self.value != _canonical_rational(self.value):
                raise CollectorError("rational measurement is not reduced")


@dataclass(frozen=True, slots=True)
class CollectorSpec:
    ordinal: int
    check_id: str
    check_schema_version: str
    parser_schema_version: str
    dependencies: tuple[str, ...]
    measurements: tuple[MeasurementSpec, ...]
    argv_template: tuple[str, ...]

    def __post_init__(self) -> None:
        _integer(self.ordinal, "collector ordinal", nonnegative=True)
        _identifier(self.check_id, "collector check id")
        if self.check_schema_version != COLLECTOR_CHECK_SCHEMA_VERSION:
            raise CollectorError("collector check schema version is unsupported")
        if type(self.parser_schema_version) is not str or not self.parser_schema_version.startswith("production-qc-"):
            raise CollectorError("collector parser schema version is unsupported")
        if not self.argv_template or "<exact-output>" not in self.argv_template:
            raise CollectorError("collector argv template must contain <exact-output>")
        if any("v:0" in argument or "a:0" in argument for argument in self.argv_template):
            raise CollectorError("collector argv template may not use relative stream selection")
        names = tuple(item.name for item in self.measurements)
        if not names or names != tuple(sorted(names)) or len(names) != len(set(names)):
            raise CollectorError("collector measurement schema must be nonempty, sorted, and unique")
        if len(self.dependencies) != len(set(self.dependencies)):
            raise CollectorError("collector dependencies must be unique")

    @property
    def canonical_argv_sha256(self) -> str:
        payload = "\0".join(self.argv_template).encode("utf-8")
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    @property
    def argv_template_sha256(self) -> str:
        """Explicit name for the runner/report field bound to this template."""

        return self.canonical_argv_sha256


def _spec(
    ordinal: int, check_id: str, dependencies: tuple[str, ...], measurements: tuple[MeasurementSpec, ...], *argv: str
) -> CollectorSpec:
    return CollectorSpec(
        ordinal,
        check_id,
        COLLECTOR_CHECK_SCHEMA_VERSION,
        "production-qc-collector-v1",
        dependencies,
        measurements,
        argv,
    )


def _m(name: str, kind: MeasurementKind, unit: MeasurementUnit) -> MeasurementSpec:
    return MeasurementSpec(name, kind, unit)


# These lists deliberately contain only objective observations.  Thresholds and any
# interpretation belong to the later evaluator, never this registry.
PRODUCTION_QC_COLLECTORS: Final = (
    _spec(0, "exact_object_identity", (), (_m("file_byte_length", "integer", "byte"), _m("file_sha256", "sha256", "none"), _m("regular_file", "boolean", "none"), _m("stable_file_identity", "boolean", "none")), "identity", "--descriptor-scan", "<exact-output>"),
    _spec(1, "container_stream_topology", ("exact_object_identity",), (_m("audio_stream_count", "integer", "stream"), _m("stream_count", "integer", "stream"), _m("video_stream_count", "integer", "stream")), "ffprobe", "-v", "error", "-bitexact", "-show_optional_fields", "always", "-count_packets", "-show_format", "-show_streams", "-show_entries", "format=format_name:format_tags=:stream=index,codec_type,codec_name,time_base,width,height,pix_fmt,sample_rate,channels,channel_layout,nb_read_packets:stream_tags=:stream_disposition=", "-of", "json=string_validation=fail", "<exact-output>"),
    _spec(2, "packet_timeline_integrity", ("container_stream_topology",), (_m("packet_count", "integer", "packet"), _m("stream_count", "integer", "stream"), _m("timestamp_anomaly_count", "integer", "count")), "ffprobe", "-v", "error", "-of", "compact=p=1:nk=0", "-show_entries", "packet=stream_index,pts,dts,duration,flags", "-show_packets", "-select_streams", "<absolute-stream-index>", "<exact-output>"),
    _spec(3, "decoded_frame_timeline", ("container_stream_topology",), (_m("frame_count", "integer", "frame"), _m("sample_count", "integer", "sample"), _m("timestamp_anomaly_count", "integer", "count")), "ffprobe", "-v", "error", "-of", "compact=p=1:nk=0", "-show_entries", "frame=stream_index,media_type,pts,pkt_dts,pkt_duration,best_effort_timestamp,nb_samples", "-show_frames", "-select_streams", "<absolute-stream-index>", "<exact-output>"),
    _spec(4, "full_video_decode", ("container_stream_topology",), (_m("framehash_row_count", "integer", "frame"), _m("video_stream_count", "integer", "stream")), "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-nostats", "-bitexact", "-xerror", "-err_detect", "explode", "-max_error_rate", "0", "-abort_on", "empty_output_stream", "-i", "<exact-output>", "-map", "0:<absolute-stream-index>", "-map_metadata", "-1", "-map_chapters", "-1", "-an", "-sn", "-dn", "-fps_mode", "passthrough", "-c:v", "rawvideo", "-f", "framehash", "-hash", "sha256", "-progress", "pipe:<progress-fd>", "-"),
    _spec(5, "full_audio_decode", ("container_stream_topology",), (_m("audio_stream_count", "integer", "stream"), _m("framehash_row_count", "integer", "frame")), "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-nostats", "-bitexact", "-xerror", "-err_detect", "explode", "-max_error_rate", "0", "-abort_on", "empty_output_stream", "-i", "<exact-output>", "-map", "0:<absolute-stream-index>", "-map_metadata", "-1", "-map_chapters", "-1", "-vn", "-sn", "-dn", "-c:a", "pcm_s32le", "-f", "framehash", "-hash", "sha256", "-progress", "pipe:<progress-fd>", "-"),
    _spec(6, "video_black_intervals", ("full_video_decode",), (_m("interval_count", "integer", "count"), _m("right_censored_interval_count", "integer", "count")), "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-nostats", "-bitexact", "-xerror", "-err_detect", "explode", "-max_error_rate", "0", "-abort_on", "empty_output_stream", "-i", "<exact-output>", "-map", "0:<absolute-stream-index>", "-map_metadata", "-1", "-map_chapters", "-1", "-an", "-sn", "-dn", "-vf", "settb=AVTB,blackdetect=d=0:pic_th=0.980:pix_th=0.100,metadata=mode=print:file='pipe\\:<metadata-fd>':direct=1", "-fps_mode", "passthrough", "-progress", "pipe:<progress-fd>", "-f", "null", "-"),
    _spec(7, "video_freeze_intervals", ("full_video_decode",), (_m("interval_count", "integer", "count"), _m("right_censored_interval_count", "integer", "count")), "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-nostats", "-bitexact", "-xerror", "-err_detect", "explode", "-max_error_rate", "0", "-abort_on", "empty_output_stream", "-i", "<exact-output>", "-map", "0:<absolute-stream-index>", "-map_metadata", "-1", "-map_chapters", "-1", "-an", "-sn", "-dn", "-vf", "settb=AVTB,freezedetect=n=0.001:d=0.500,metadata=mode=print:file='pipe\\:<metadata-fd>':direct=1", "-fps_mode", "passthrough", "-progress", "pipe:<progress-fd>", "-f", "null", "-"),
    _spec(8, "audio_silence_intervals", ("full_audio_decode",), (_m("channel_count", "integer", "channel"), _m("interval_count", "integer", "count"), _m("right_censored_interval_count", "integer", "count")), "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-nostats", "-bitexact", "-xerror", "-err_detect", "explode", "-max_error_rate", "0", "-abort_on", "empty_output_stream", "-i", "<exact-output>", "-map", "0:<absolute-stream-index>", "-map_metadata", "-1", "-map_chapters", "-1", "-vn", "-sn", "-dn", "-af", "asettb=AVTB,silencedetect=n=-50dB:d=0.500:mono=1,ametadata=mode=print:file='pipe\\:<metadata-fd>':direct=1", "-progress", "pipe:<progress-fd>", "-f", "null", "-"),
    _spec(9, "audio_sample_health", ("full_audio_decode",), (_m("channel_count", "integer", "channel"), _m("nonfinite_value_count", "integer", "count"), _m("snapshot_count", "integer", "count")), "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-nostats", "-bitexact", "-xerror", "-err_detect", "explode", "-max_error_rate", "0", "-abort_on", "empty_output_stream", "-i", "<exact-output>", "-map", "0:<absolute-stream-index>", "-map_metadata", "-1", "-map_chapters", "-1", "-vn", "-sn", "-dn", "-af", "asettb=AVTB,astats=metadata=1:reset=0,ametadata=mode=print:file='pipe\\:<metadata-fd>':direct=1", "-progress", "pipe:<progress-fd>", "-f", "null", "-"),
    _spec(10, "av_presentation_envelope", ("decoded_frame_timeline",), (_m("audio_stream_count", "integer", "stream"), _m("video_stream_count", "integer", "stream")), "projection", "<exact-output>"),
    _spec(11, "edit_junction_continuity", ("decoded_frame_timeline",), (_m("junction_count", "integer", "count"), _m("observation_count", "integer", "count")), "projection", "<exact-output>"),
)
PRODUCTION_RENDER_QC_COLLECTOR_REGISTRY: Final = PRODUCTION_QC_COLLECTORS


@dataclass(frozen=True, slots=True)
class StreamTopology:
    index: int
    codec_type: Literal["video", "audio", "subtitle", "data", "attachment", "unknown"]
    codec_name: str
    time_base: str | None
    width: int | None
    height: int | None
    pix_fmt: str | None
    sample_rate: int | None
    channels: int | None
    channel_layout: str | None
    nb_read_packets: int

    def __post_init__(self) -> None:
        _integer(self.index, "stream index", nonnegative=True)
        if self.codec_type not in {"video", "audio", "subtitle", "data", "attachment", "unknown"}:
            raise CollectorError("stream codec type is unsupported")
        if type(self.codec_name) is not str or not self.codec_name:
            raise CollectorError("stream codec name is invalid")
        if self.time_base is not None:
            _canonical_rational(self.time_base)
        _integer(self.nb_read_packets, "stream packet count", nonnegative=True)
        if self.codec_type == "video":
            if type(self.width) is not int or type(self.height) is not int or self.width <= 0 or self.height <= 0:  # noqa: E721
                raise CollectorError("video stream dimensions are invalid")
            if type(self.pix_fmt) is not str or not self.pix_fmt:
                raise CollectorError("video stream pixel format is invalid")
        if self.codec_type == "audio":
            if type(self.sample_rate) is not int or self.sample_rate <= 0 or type(self.channels) is not int or self.channels <= 0:  # noqa: E721
                raise CollectorError("audio stream properties are invalid")


@dataclass(frozen=True, slots=True)
class Topology:
    streams: tuple[StreamTopology, ...]

    def __post_init__(self) -> None:
        if len(self.streams) > 256:
            raise CollectorError("topology stream count is outside bounds")
        indexes = tuple(stream.index for stream in self.streams)
        if len(indexes) != len(set(indexes)):
            raise CollectorError("topology contains duplicate absolute stream indexes")

    def indexes(self, codec_type: str) -> tuple[int, ...]:
        return tuple(stream.index for stream in self.streams if stream.codec_type == codec_type)


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise CollectorError("topology JSON contains a duplicate key")
        result[key] = value
    return result


def parse_topology_json(raw: bytes, *, max_bytes: int = _MAX_TOPOLOGY_BYTES) -> Topology:
    """Strictly parse the bounded FFprobe topology object without JSON coercions."""

    if type(max_bytes) is not int or max_bytes <= 0:  # noqa: E721
        raise CollectorError("topology JSON byte bound is invalid")
    if type(raw) is not bytes or not raw or len(raw) > max_bytes:  # noqa: E721
        raise CollectorError("topology JSON exceeds its byte bound")
    try:
        parsed: object = json.loads(
            raw.decode("utf-8", "strict"),
            object_pairs_hook=_unique_json_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                CollectorError(f"invalid JSON constant {value}")
            ),
        )
    except CollectorError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise CollectorError("topology JSON must be strict UTF-8 JSON") from error
    if type(parsed) is not dict:  # noqa: E721
        raise CollectorError("topology JSON root is not closed")
    root = cast(dict[str, object], parsed)
    allowed_root = {"format", "programs", "stream_groups", "streams"}
    if set(root) != allowed_root:
        raise CollectorError("topology JSON root is not closed")
    for section in ("programs", "stream_groups"):
        if type(root[section]) is not list or root[section]:  # noqa: E721
            raise CollectorError(
                "topology contains an unsupported nonempty program or stream group"
            )
    if type(root["format"]) is not dict or set(cast(dict[str, object], root["format"])) != {"format_name"}:
        raise CollectorError("topology format object is not closed")
    if type(cast(dict[str, object], root["format"])["format_name"]) is not str:  # noqa: E721
        raise CollectorError("topology format name is invalid")
    raw_streams = root["streams"]
    if type(raw_streams) is not list:  # noqa: E721
        raise CollectorError("topology streams must be an array")
    streams: list[StreamTopology] = []
    for item in cast(list[object], raw_streams):
        if type(item) is not dict:  # noqa: E721
            raise CollectorError("topology stream object is not closed")
        record = cast(dict[str, object], item)
        allowed = {"index", "codec_type", "codec_name", "time_base", "width", "height", "pix_fmt", "sample_rate", "channels", "channel_layout", "nb_read_packets"}
        required = {"index", "codec_type", "codec_name", "nb_read_packets"}
        if not required <= set(record) or not set(record) <= allowed:
            raise CollectorError("topology stream object is not closed")
        if type(record["index"]) is not int or type(record["codec_type"]) is not str or type(record["codec_name"]) is not str:  # noqa: E721
            raise CollectorError("topology stream field type is invalid")
        packet_count = record["nb_read_packets"]
        if type(packet_count) is str and _INTEGER_PATTERN.fullmatch(packet_count) is not None:
            packet_count = int(packet_count)
        if type(packet_count) is not int:  # noqa: E721
            raise CollectorError("topology packet count is invalid")
        normalized_numbers: dict[str, int | None] = {}
        for field in ("width", "height", "sample_rate", "channels"):
            value = record.get(field)
            if value == "N/A":
                value = None
            if type(value) is str and _INTEGER_PATTERN.fullmatch(value) is not None:
                value = int(value)
            if value is not None and type(value) is not int:  # noqa: E721
                raise CollectorError("topology numeric stream field is invalid")
            normalized_numbers[field] = value
        for field in ("pix_fmt", "channel_layout"):
            if record.get(field) is not None and type(record[field]) is not str:  # noqa: E721
                raise CollectorError("topology text stream field is invalid")
        raw_time_base = record.get("time_base")
        if raw_time_base is not None and type(raw_time_base) is not str:  # noqa: E721
            raise CollectorError("topology time base is invalid")
        time_base = None if raw_time_base in {None, "N/A", "0/0"} else raw_time_base
        streams.append(StreamTopology(record["index"], cast(Literal["video", "audio", "subtitle", "data", "attachment", "unknown"], record["codec_type"]), record["codec_name"], time_base, normalized_numbers["width"], normalized_numbers["height"], cast(str | None, record.get("pix_fmt")), normalized_numbers["sample_rate"], normalized_numbers["channels"], cast(str | None, record.get("channel_layout")), packet_count))
    return Topology(tuple(streams))


def build_stream_argv(argv_template: Sequence[str], stream_index: int) -> tuple[str, ...]:
    """Bind an already discovered absolute index into a canonical argv template."""

    _integer(stream_index, "absolute stream index", nonnegative=True)
    if any("v:0" in argument or "a:0" in argument for argument in argv_template):
        raise CollectorError("relative stream selection is forbidden")
    if not any("<absolute-stream-index>" in argument for argument in argv_template):
        raise CollectorError("stream argv template does not declare an absolute index")
    return tuple(argument.replace("<absolute-stream-index>", str(stream_index)) for argument in argv_template)


def bind_collector_argv(template: CollectorSpec, *, exact_output: str, stream_index: int | None = None, progress_fd: int | None = None, metadata_fd: int | None = None) -> tuple[str, ...]:
    """Bind runtime-only values after the canonical template hash has been selected."""

    if type(exact_output) is not str or not exact_output or "\0" in exact_output:  # noqa: E721
        raise CollectorError("exact output binding is invalid")
    bindings = {"<exact-output>": exact_output}
    for token, value in (("<absolute-stream-index>", stream_index), ("<progress-fd>", progress_fd), ("<metadata-fd>", metadata_fd)):
        used = any(token in argument for argument in template.argv_template)
        minimum = {
            "<absolute-stream-index>": 0,
            "<metadata-fd>": 1,
            "<progress-fd>": 3,
        }[token]
        if used and (type(value) is not int or value < minimum or value == 2):  # noqa: E721
            raise CollectorError(f"collector binding for {token} is invalid")
        if used:
            bindings[token] = str(value)
    result = tuple(argument for argument in template.argv_template)
    for token, value in bindings.items():
        result = tuple(argument.replace(token, value) for argument in result)
    if any("<" in argument or "v:0" in argument or "a:0" in argument for argument in result):
        raise CollectorError("collector argv retained an unresolved or relative selector")
    return result


def parse_compact_record(line: str) -> dict[str, str]:
    """Parse one compact FFprobe record and reject duplicate keys and malformed fields."""

    if type(line) is not str or not line or len(line.encode("utf-8")) > _MAX_RECORD_BYTES:  # noqa: E721
        raise CollectorError("compact record is empty or exceeds its bound")
    fields = line.rstrip("\n").split("|")
    if not fields[0] or "=" in fields[0]:
        raise CollectorError("compact record lacks a section name")
    result = {"section": fields[0]}
    for field in fields[1:]:
        if field.count("=") != 1:
            raise CollectorError("compact record field is malformed")
        key, value = field.split("=", 1)
        if not key or key in result:
            raise CollectorError("compact record contains duplicate or empty key")
        result[key] = value
    return result


class BoundedStreamReducer:
    """Common online byte accounting and deterministic first/last examples."""

    def __init__(self, *, example_cap: int = _EXAMPLE_CAP) -> None:
        if type(example_cap) is not int or not 1 <= example_cap <= 64:  # noqa: E721
            raise CollectorError("example cap is outside bounds")
        self._digest = hashlib.sha256()
        self.stream_byte_length = 0
        self.record_count = 0
        self._first_examples: list[str] = []
        self._last_examples: list[str] = []
        self._example_cap = example_cap
        self._completed = False

    @property
    def stream_sha256(self) -> str:
        return "sha256:" + self._digest.hexdigest()

    @property
    def examples(self) -> tuple[str, ...]:
        return tuple(self._first_examples + self._last_examples)

    @property
    def first_examples(self) -> tuple[str, ...]:
        return tuple(self._first_examples)

    @property
    def last_examples(self) -> tuple[str, ...]:
        return tuple(self._last_examples)

    def _ingest_bytes(self, data: bytes) -> None:
        if self._completed or type(data) is not bytes:  # noqa: E721
            raise CollectorError("reducer cannot accept this byte stream")
        self._digest.update(data)
        self.stream_byte_length += len(data)

    def _example(self, value: str) -> None:
        if len(value.encode("utf-8")) > _MAX_RECORD_BYTES:
            raise CollectorError("example exceeds its byte bound")
        if len(self._first_examples) < self._example_cap:
            self._first_examples.append(value)
        else:
            self._last_examples.append(value)
            if len(self._last_examples) > self._example_cap:
                self._last_examples.pop(0)


class CompactTimelineReducer(BoundedStreamReducer):
    """Online packet/frame compact-output reducer with no frame-rate synthesis.

    Timestamp irregularities are observations: a B-frame reorder or VFR duration
    does not turn into a policy decision at collection time.  Missing or ``N/A``
    timestamps are counted rather than silently replaced.
    """

    def __init__(self, section: Literal["packet", "frame"], stream_indexes: Sequence[int]) -> None:
        super().__init__()
        if section not in {"packet", "frame"}:
            raise CollectorError("timeline section is unsupported")
        indexes = tuple(stream_indexes)
        if not indexes or any(type(index) is not int or index < 0 for index in indexes):  # noqa: E721
            raise CollectorError("timeline requires discovered absolute stream indexes")
        self._section = section
        self._indexes = frozenset(indexes)
        self._tail = b""
        self.timestamp_anomaly_count = 0
        self.missing_timestamp_count = 0
        self.first_pts: dict[int, int] = {}
        self.last_pts: dict[int, int] = {}
        self.first_dts: dict[int, int] = {}
        self.last_dts: dict[int, int] = {}
        self.first_duration: dict[int, int] = {}
        self.last_duration: dict[int, int] = {}

    def feed(self, data: bytes) -> None:
        self._ingest_bytes(data)
        self._tail += data
        while b"\n" in self._tail:
            raw, self._tail = self._tail.split(b"\n", 1)
            if not raw:
                continue
            if len(raw) > _MAX_RECORD_BYTES:
                raise CollectorError("compact timeline record exceeds its byte bound")
            try:
                record = parse_compact_record(raw.decode("utf-8", "strict"))
            except UnicodeError as error:
                raise CollectorError("compact timeline is not UTF-8") from error
            if record["section"].rsplit(".", 1)[-1] != self._section:
                raise CollectorError("compact timeline has an unexpected section")
            index_text = record.get("stream_index")
            if index_text is None or _INTEGER_PATTERN.fullmatch(index_text) is None:
                raise CollectorError("compact timeline lacks an absolute stream index")
            index = int(index_text)
            if index not in self._indexes:
                raise CollectorError("compact timeline selected an undiscovered stream index")
            self.record_count += 1
            for field, first, last in (("pts", self.first_pts, self.last_pts), ("dts", self.first_dts, self.last_dts), ("duration", self.first_duration, self.last_duration)):
                value = record.get(field, record.get(f"pkt_{field}"))
                if value is None or value == "N/A":
                    self.missing_timestamp_count += 1
                    self.timestamp_anomaly_count += 1
                    self._example(f"missing_{field}:{index}")
                    continue
                if _INTEGER_PATTERN.fullmatch(value) is None:
                    raise CollectorError(f"compact timeline {field} is malformed")
                number = int(value)
                if field == "duration" and number < 0:
                    self.timestamp_anomaly_count += 1
                    self._example(f"negative_duration:{index}:{number}")
                if field != "duration" and index in last and number < last[index]:
                    self.timestamp_anomaly_count += 1
                    self._example(f"{field}_regression:{index}:{last[index]}:{number}")
                first.setdefault(index, number)
                last[index] = number
        if len(self._tail) > _MAX_RECORD_BYTES:
            raise CollectorError("compact timeline record exceeds its byte bound")

    def complete(self) -> None:
        if self._tail:
            raise CollectorError("compact timeline is truncated")
        self._completed = True


class AstatsReducer(BoundedStreamReducer):
    """Cumulative astats metadata reducer with explicit non-finite sentinels."""

    def __init__(self, channel_count: int) -> None:
        super().__init__()
        _integer(channel_count, "astats channel count", nonnegative=True)
        if channel_count == 0:
            raise CollectorError("astats requires an audio channel")
        self.channel_count = channel_count
        self.snapshot_count = 0
        self.nonfinite_value_count = 0
        self._snapshot_open = False
        self._final_snapshot_complete = False

    def feed(self, data: bytes) -> None:
        self._ingest_bytes(data)

    def begin_snapshot(self) -> None:
        if self._snapshot_open:
            raise CollectorError("astats snapshot already open")
        self._snapshot_open = True

    def feed_metadata(self, key: str, value: str) -> None:
        if not self._snapshot_open or not key.startswith("lavfi.astats."):
            raise CollectorError("astats metadata is outside an open snapshot")
        parsed = parse_astats_value(value)
        if parsed in {"nan", "inf", "-inf"}:
            self.nonfinite_value_count += 1
            self._example(f"{key}={parsed}")

    def finish_snapshot(self, *, final: bool = False) -> None:
        if not self._snapshot_open:
            raise CollectorError("astats snapshot is not open")
        self.snapshot_count += 1
        self._snapshot_open = False
        self._final_snapshot_complete = final

    def complete(self) -> None:
        if self._snapshot_open or self.snapshot_count == 0 or not self._final_snapshot_complete:
            raise CollectorError("astats lacks a final complete cumulative snapshot")
        self._completed = True


class ProgressReducer(BoundedStreamReducer):
    """Strict FFmpeg progress reducer requiring exactly one terminal marker."""

    def __init__(self) -> None:
        super().__init__()
        self._tail = b""
        self._terminal_count = 0
        self.mapped_output_records = 0

    def feed(self, data: bytes) -> None:
        self._ingest_bytes(data)
        self._tail += data
        if len(self._tail) > _MAX_RECORD_BYTES:
            raise CollectorError("progress record exceeds its byte bound")
        while b"\n" in self._tail:
            raw, self._tail = self._tail.split(b"\n", 1)
            if not raw:
                continue
            try:
                key, value = raw.decode("ascii", "strict").split("=", 1)
            except (UnicodeError, ValueError) as error:
                raise CollectorError("progress record is malformed") from error
            if key == "progress":
                if value == "end":
                    self._terminal_count += 1
                elif value != "continue":
                    raise CollectorError("progress marker is unsupported")
            elif key in {"frame", "out_time_us", "total_size"}:
                if value == "N/A" and key != "frame":
                    continue
                if _INTEGER_PATTERN.fullmatch(value) is None:
                    raise CollectorError("progress counter is malformed")
                if key == "frame":
                    self.mapped_output_records = max(self.mapped_output_records, int(value))

    def complete(self) -> None:
        if self._tail or self._terminal_count != 1:
            raise CollectorError("full decode lacks complete terminal progress evidence")
        self._completed = True


class FramehashReducer(BoundedStreamReducer):
    """Online framehash parser; rows are retained only as bounded examples."""

    def __init__(self) -> None:
        super().__init__()
        self._tail = b""
        self.row_count = 0
        self.first_pts: int | None = None
        self.last_pts: int | None = None

    def feed(self, data: bytes) -> None:
        self._ingest_bytes(data)
        self._tail += data
        if len(self._tail) > _MAX_RECORD_BYTES:
            raise CollectorError("framehash row exceeds its byte bound")
        while b"\n" in self._tail:
            raw, self._tail = self._tail.split(b"\n", 1)
            if not raw or raw.startswith(b"#"):
                continue
            try:
                fields = [field.strip() for field in raw.decode("ascii", "strict").split(",")]
                if len(fields) < 6 or _INTEGER_PATTERN.fullmatch(fields[2]) is None:
                    raise ValueError
                pts = int(fields[2])
            except (UnicodeError, ValueError) as error:
                raise CollectorError("framehash row is malformed") from error
            self.row_count += 1
            self.first_pts = pts if self.first_pts is None else self.first_pts
            self.last_pts = pts
            self._example("|".join(fields[:6]))

    def complete(self) -> None:
        if self._tail or self.row_count == 0:
            raise CollectorError("full decode lacks a nonempty framehash stream")
        self._completed = True


class DetectorIntervalReducer(BoundedStreamReducer):
    """Detector metadata reducer that preserves unmatched terminal starts as censored."""

    def __init__(self, detector: Literal["black", "freeze", "silence"]) -> None:
        super().__init__()
        if detector not in {"black", "freeze", "silence"}:
            raise CollectorError("detector is unsupported")
        self._detector = detector
        self._open_start: dict[int, str] = {}
        self._intervals: list[tuple[int, str, str | None]] = []

    @property
    def intervals(self) -> tuple[tuple[str, str | None], ...]:
        return tuple((start, end) for channel, start, end in self._intervals if channel == 0)

    @property
    def channel_intervals(self) -> tuple[tuple[int, str, str | None], ...]:
        return tuple(self._intervals)

    @property
    def right_censored_count(self) -> int:
        return sum(end is None for _, _, end in self._intervals)

    def feed(self, data: bytes) -> None:
        self._ingest_bytes(data)

    def feed_metadata(self, key: str, value: str) -> None:
        channel = 0
        if self._detector == "black" and key in {"lavfi.black_start", "lavfi.black_end"}:
            event = key.removeprefix("lavfi.black_")
        elif self._detector == "freeze" and key in {"lavfi.freezedetect.freeze_start", "lavfi.freezedetect.freeze_end", "lavfi.freezedetect.freeze_duration"}:
            event = key.removeprefix("lavfi.freezedetect.freeze_")
        elif self._detector == "silence" and key.startswith("lavfi.silence_"):
            event = key.removeprefix("lavfi.silence_")
            if "." in event:
                event, channel_text = event.split(".", 1)
                if _INTEGER_PATTERN.fullmatch(channel_text) is None:
                    raise CollectorError("silence channel is malformed")
                channel = int(channel_text)
        else:
            raise CollectorError("detector metadata key is unrelated")
        timestamp = parse_rational_timestamp(value)
        if event == "duration":
            # Detector duration can be emitted before or after the paired end;
            # it is a redundant observation, never an inferred interval endpoint.
            return
        if event == "start":
            if channel in self._open_start:
                raise CollectorError("detector interval starts before prior interval ends")
            self._open_start[channel] = timestamp
        elif event == "end":
            if channel not in self._open_start:
                raise CollectorError("detector interval ends before it starts")
            start = self._open_start.pop(channel)
            if Fraction(timestamp) < Fraction(start):
                raise CollectorError("detector interval ends before it starts")
            self._intervals.append((channel, start, timestamp))
            self._example(f"{self._detector}:{channel}:{start}:{timestamp}")
        else:
            raise CollectorError("detector metadata event is unsupported")

    def complete(self) -> None:
        for channel, start in sorted(self._open_start.items()):
            self._intervals.append((channel, start, None))
            self._example(f"{self._detector}:{channel}:{start}:right_censored")
        self._open_start.clear()
        self._completed = True


class MetadataPrintReducer(BoundedStreamReducer):
    """Strict online parser for FFmpeg metadata=print/ametadata=print output."""

    def __init__(self, target: DetectorIntervalReducer | AstatsReducer) -> None:
        super().__init__()
        self._target = target
        self._tail = b""
        self._header_open = False

    def feed(self, data: bytes) -> None:
        self._ingest_bytes(data)
        self._tail += data
        while b"\n" in self._tail:
            raw, self._tail = self._tail.split(b"\n", 1)
            if len(raw) > _MAX_RECORD_BYTES:
                raise CollectorError("metadata line exceeds its byte bound")
            if not raw:
                continue
            try:
                line = raw.decode("utf-8", "strict")
            except UnicodeError as error:
                raise CollectorError("metadata line is not UTF-8") from error
            if line.startswith("frame:"):
                if self._header_open and isinstance(self._target, AstatsReducer):
                    self._target.finish_snapshot()
                self._header_open = True
                if isinstance(self._target, AstatsReducer):
                    self._target.begin_snapshot()
                continue
            if not self._header_open or line.count("=") != 1:
                raise CollectorError("metadata event is malformed or lacks a frame header")
            key, value = line.split("=", 1)
            if not key or not value:
                raise CollectorError("metadata event is malformed")
            self.record_count += 1
            if isinstance(self._target, DetectorIntervalReducer):
                self._target.feed_metadata(key, value)
            else:
                self._target.feed_metadata(key, value)
        if len(self._tail) > _MAX_RECORD_BYTES:
            raise CollectorError("metadata line exceeds its byte bound")

    def complete(self) -> None:
        if self._tail:
            raise CollectorError("metadata stream is truncated")
        if isinstance(self._target, AstatsReducer) and self._header_open:
            self._target.finish_snapshot(final=True)
        self._target.complete()
        self._completed = True


@dataclass(frozen=True, slots=True)
class CollectionObservation:
    """Runner-facing path-free output for one objective collection attempt."""

    collection_status: CollectionStatus
    coverage: Coverage
    measurements: tuple[Measurement, ...]
    stream_byte_length: int
    stream_sha256: str
    record_count: int
    diagnostic_code: str | None = None
    examples: tuple[str, ...] = ()
    progress_stream_byte_length: int = 0
    progress_stream_sha256: str = _EMPTY_SHA256

    def __post_init__(self) -> None:
        pairs: Mapping[str, frozenset[str]] = {
            "completed": frozenset({"full_file", "not_applicable"}),
            "incomplete": frozenset({"partial", "none"}),
            "not_run": frozenset({"none"}),
            "not_applicable": frozenset({"not_applicable"}),
        }
        if self.collection_status not in pairs or self.coverage not in pairs[self.collection_status]:
            raise CollectorError("collection status and coverage disagree")
        _integer(self.stream_byte_length, "stream byte length", nonnegative=True)
        _integer(self.record_count, "stream record count", nonnegative=True)
        _sha256(self.stream_sha256, "stream sha256")
        _integer(
            self.progress_stream_byte_length,
            "progress stream byte length",
            nonnegative=True,
        )
        _sha256(self.progress_stream_sha256, "progress stream sha256")
        names = tuple(item.name for item in self.measurements)
        if len(self.measurements) > 256 or names != tuple(sorted(names)) or len(names) != len(set(names)):
            raise CollectorError("observation measurements must be sorted and unique")
        if self.diagnostic_code is not None:
            _identifier(self.diagnostic_code, "diagnostic code")
        if (
            type(self.examples) is not tuple  # noqa: E721
            or len(self.examples) > _OBSERVATION_EXAMPLE_CAP
            or any(
                type(example) is not str
                or not example
                or "\x00" in example
                or "\n" in example
                or len(example.encode("utf-8")) > _MAX_RECORD_BYTES
                for example in self.examples
            )
        ):
            raise CollectorError("observation examples are not bounded canonical text")

    @classmethod
    def completed(
        cls, spec: CollectorSpec, values: Mapping[str, str], *, stream_byte_length: int,
        stream_sha256: str, record_count: int, examples: tuple[str, ...] = (),
        progress_stream_byte_length: int = 0, progress_stream_sha256: str = _EMPTY_SHA256,
    ) -> CollectionObservation:
        if set(values) != {item.name for item in spec.measurements}:
            raise CollectorError("observation measurements do not match collector schema")
        measurements = tuple(
            Measurement(item.name, item.value_kind, values[item.name], item.unit)
            for item in spec.measurements
        )
        return cls(
            "completed",
            "full_file",
            measurements,
            stream_byte_length,
            stream_sha256,
            record_count,
            examples=examples,
            progress_stream_byte_length=progress_stream_byte_length,
            progress_stream_sha256=progress_stream_sha256,
        )


class CollectorProcess(Protocol):
    """Minimal runner port; implementations feed streams incrementally without a shell."""

    def run_collector(self, argv: tuple[str, ...], *, stdout: BoundedStreamReducer, progress: ProgressReducer | None) -> int: ...


def canonical_argv_sha256(argv: Sequence[str]) -> str:
    """Hash a path-free argv only; runners must substitute host paths after this boundary."""

    if not argv or any(type(argument) is not str for argument in argv):  # noqa: E721
        raise CollectorError("argv must be a nonempty text sequence")
    if "<exact-output>" not in argv or any("/" in argument for argument in argv):
        raise CollectorError("canonical argv must be path-free and include <exact-output>")
    return "sha256:" + hashlib.sha256("\0".join(argv).encode("utf-8")).hexdigest()
