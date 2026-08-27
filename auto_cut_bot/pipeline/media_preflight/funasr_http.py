"""Strict loopback HTTP adapter for timed SenseVoice/FSMN evidence."""

# pyright: reportArgumentType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import replace
from typing import Any, cast

from autocut_kernel.media import (
    Coverage,
    CoverageOutcome,
    EvidenceCompleteness,
    EvidenceContext,
    MediaKind,
    SpeechActivitySegment,
    SpeechActivitySet,
    SpeechSourceOutcome,
    TranscriptCompleteness,
    TranscriptSegment,
    TranscriptSentence,
    TranscriptSet,
    TranscriptSourceOutcome,
    TranscriptWord,
)
from autocut_kernel.media.types import canonical_sha256

from auto_cut_bot.pipeline.debug import ModelIoDebugContext, ModelIoDebugSink

from .http_transport import FileHttpTransport, HttpxFileTransport
from .models import (
    LocalMediaEvidenceError,
    LocalMediaPolicyError,
    LocalMediaPreflightError,
    LocalMediaSourceError,
    LocalMediaToolError,
)
from .speech_port import (
    TimedSpeechEvidence,
    TimedSpeechInvocationTrace,
    TimedSpeechProducerIdentity,
    TimedSpeechTimingErrorBound,
)


def _obj(v: object, fields: set[str], name: str) -> dict[str, object]:
    if type(v) is not dict or set(cast(dict[object, object], v)) != fields:
        raise LocalMediaEvidenceError(f"{name} schema is not closed")
    return cast(dict[str, object], v)


def _arr(v: object, name: str) -> list[dict[str, object]]:
    if type(v) is not list or any(type(x) is not dict for x in cast(list[object], v)):
        raise LocalMediaEvidenceError(f"{name} must be object array")
    return cast(list[dict[str, object]], v)


def _text(v: object, name: str) -> str:
    if type(v) is not str or not v.strip():  # noqa: E721
        raise LocalMediaEvidenceError(f"{name} must be non-empty text")
    return v


def _integer(v: object, name: str) -> int:
    if type(v) is not int:  # noqa: E721
        raise LocalMediaEvidenceError(f"{name} must be an integer")
    return v


def _boolean(v: object, name: str) -> bool:
    if type(v) is not bool:  # noqa: E721
        raise LocalMediaEvidenceError(f"{name} must be a boolean")
    return v


def _text_array(v: object, name: str) -> tuple[str, ...]:
    if type(v) is not list:  # noqa: E721
        raise LocalMediaEvidenceError(f"{name} must be a text array")
    result = tuple(_text(item, name) for item in cast(list[object], v))
    if len(result) != len(set(result)):
        raise LocalMediaEvidenceError(f"{name} must be deduplicated")
    return result


def _sha(v: bytes) -> str:
    return "sha256:" + hashlib.sha256(v).hexdigest()


class FunASRHttpTimedSpeechEvidencePort:
    def __init__(
        self,
        *,
        transport: FileHttpTransport | None = None,
        shared_token: str | None = None,
        debug_sink: ModelIoDebugSink | None = None,
    ) -> None:
        self._transport = transport or HttpxFileTransport()
        token = shared_token if shared_token is not None else os.environ.get("FUNASR_SHARED_TOKEN")
        if type(token) is not str or not token:  # noqa: E721
            raise LocalMediaPolicyError("FUNASR_SHARED_TOKEN must be non-empty")
        self._shared_token = token
        self._debug_sink = debug_sink

    def produce(self, request: Any) -> TimedSpeechEvidence:
        r = request
        try:
            source_size = r.source_path.stat().st_size
        except OSError as error:
            raise LocalMediaSourceError("timed speech source materialization is unavailable") from error
        if source_size < 0 or source_size > r.effective_max_source_bytes:
            raise LocalMediaSourceError("source exceeds the frozen effective source-byte limit")
        manifest = json.dumps(r.to_mapping(), sort_keys=True, separators=(",", ":")).encode()
        headers = {
            "Content-Type": "application/octet-stream",
            "X-Timed-Speech-Manifest": base64.b64encode(manifest).decode(),
            "X-Timed-Speech-Request-SHA256": r.identity_sha256,
            "Authorization": f"Bearer {self._shared_token}",
        }
        context = ModelIoDebugContext(
            provider="funasr-fsmn-http",
            provider_idempotency_key=r.identity_sha256,
            model="sensevoice-small-fsmn-vad",
            call_kind="timed_speech_evidence",
        )
        if self._debug_sink is not None:
            self._debug_sink.capture_request(
                context,
                operation="produce",
                body={
                    "endpoint_url": r.endpoint_url,
                    "headers": headers,
                    "source_id": r.source_id,
                    "source_sha256": r.source_sha256,
                    "source_byte_length": source_size,
                    "timeout_seconds": r.timeout_seconds,
                },
            )
        try:
            status, raw = self._transport.post(
                r.endpoint_url,
                headers=headers,
                body_path=r.source_path,
                timeout_seconds=r.timeout_seconds,
                max_response_bytes=r.max_response_bytes,
            )
        except Exception as error:
            if self._debug_sink is not None:
                self._debug_sink.capture_terminal(
                    context,
                    operation="produce",
                    terminal={"error_type": type(error).__name__},
                )
            raise
        if self._debug_sink is not None:
            self._debug_sink.capture_terminal(
                context,
                operation="produce",
                terminal={
                    "http_status": status,
                    "raw_response_sha256": _sha(raw),
                    "raw_response_byte_length": len(raw),
                },
                raw_output=raw,
            )
        if len(raw) > r.max_response_bytes:
            raise LocalMediaToolError("response exceeded byte bound")
        if status != 200:
            if status == 503:
                raise LocalMediaToolError(
                    f"timed speech service busy ({_sha(raw)})", code="TIMED_SPEECH_BUSY"
                )
            raise LocalMediaToolError(f"timed speech HTTP {status} ({_sha(raw)})")
        try:
            p = json.loads(raw.decode())
        except Exception as e:
            raise LocalMediaEvidenceError("response is not UTF-8 JSON") from e
        try:
            base_fields = {
                "schema_version",
                "request_identity_sha256",
                "source",
                "source_byte_limits",
                "container",
                "audio_clock",
                "requested_range",
                "timed_speech_policy_sha256",
                "transcript_capability",
                "producer_identities",
                "timing_error_bounds",
                "transcript",
                "speech_activity",
            }
            raw_extras = cast(object, getattr(r, "response_extra_fields", frozenset()))
            if type(raw_extras) is not frozenset or any(  # noqa: E721
                type(item) is not str for item in cast(frozenset[object], raw_extras)
            ):
                raise LocalMediaPolicyError("timed speech response extension is invalid")
            extras = cast(frozenset[str], raw_extras)
            return self._parse(
                _obj(
                    p,
                    base_fields | extras,
                    "response",
                ),
                r,
                raw,
            )
        except LocalMediaPreflightError:
            raise
        except (KeyError, TypeError, ValueError) as e:
            raise LocalMediaEvidenceError("timed speech response is malformed") from e

    @classmethod
    def _parse(
        cls, p: dict[str, object], r: Any, raw: bytes
    ) -> TimedSpeechEvidence:
        if (
            p["schema_version"] != getattr(
                r, "response_schema_version", "timed-speech-evidence-response-v1"
            )
            or p["request_identity_sha256"] != r.identity_sha256
        ):
            raise LocalMediaSourceError("request identity drift")
        validator = getattr(r, "validate_response_authority", None)
        if validator is not None:
            validator(p.get("runtime_authority"))
        source = _obj(p["source"], {"source_id", "source_sha256"}, "source")
        source_byte_limits = _obj(
            p["source_byte_limits"],
            {
                "kernel_max_source_bytes",
                "service_max_request_bytes",
                "effective_max_source_bytes",
            },
            "source_byte_limits",
        )
        strict_source_byte_limits = {
            "kernel_max_source_bytes": _integer(
                source_byte_limits["kernel_max_source_bytes"],
                "source_byte_limits.kernel_max_source_bytes",
            ),
            "service_max_request_bytes": _integer(
                source_byte_limits["service_max_request_bytes"],
                "source_byte_limits.service_max_request_bytes",
            ),
            "effective_max_source_bytes": _integer(
                source_byte_limits["effective_max_source_bytes"],
                "source_byte_limits.effective_max_source_bytes",
            ),
        }
        container = _obj(p["container"], {"media_type", "safe_suffix"}, "container")
        if source != {"source_id": r.source_id, "source_sha256": r.source_sha256} or container != {
            "media_type": "video/mp4",
            "safe_suffix": ".mp4",
        }:
            raise LocalMediaSourceError("source identity drift")
        if strict_source_byte_limits != r.to_mapping()["source_byte_limits"]:
            raise LocalMediaSourceError("source-byte limit drift")
        audio_clock = _obj(
            p["audio_clock"],
            {"clock_id", "time_base", "origin_tick", "duration_tick"},
            "audio_clock",
        )
        time_base = _obj(audio_clock["time_base"], {"numerator", "denominator"}, "time_base")
        strict_clock = {
            "clock_id": _text(audio_clock["clock_id"], "audio_clock.clock_id"),
            "time_base": {
                "numerator": _integer(time_base["numerator"], "time_base.numerator"),
                "denominator": _integer(time_base["denominator"], "time_base.denominator"),
            },
            "origin_tick": _integer(audio_clock["origin_tick"], "audio_clock.origin_tick"),
            "duration_tick": _integer(audio_clock["duration_tick"], "audio_clock.duration_tick"),
        }
        requested_range = _obj(p["requested_range"], {"in_tick", "out_tick"}, "requested_range")
        strict_range = {
            "in_tick": _integer(requested_range["in_tick"], "requested_range.in_tick"),
            "out_tick": _integer(requested_range["out_tick"], "requested_range.out_tick"),
        }
        if (
            strict_clock != r.to_mapping()["audio_clock"]
            or strict_range != r.to_mapping()["requested_range"]
            or p["timed_speech_policy_sha256"] != r.policy_sha256
        ):
            raise LocalMediaSourceError("clock/range/policy drift")
        if p["transcript_capability"] != r.to_mapping()["transcript_capability"]:
            raise LocalMediaSourceError("capability drift")

        def context(kind: str) -> EvidenceContext:
            expected = next(x for x in r.expected_producers if x.producer_kind == kind)
            return EvidenceContext(
                r.source_id,
                r.source_sha256,
                MediaKind.AUDIO,
                r.clock_id,
                r.time_base,
                r.origin_tick,
                r.duration_tick,
                expected.producer_id,
                expected.generation_policy_sha256,
            )

        transcript = cls._transcript(
            _obj(
                p["transcript"],
                {
                    "coverage",
                    "lexical_outcome",
                    "completeness",
                    "segments",
                    "words",
                    "sentences",
                    "boundary_touch",
                    "truncated",
                },
                "transcript",
            ),
            r,
            context("asr"),
        )
        speech = cls._speech(
            _obj(
                p["speech_activity"],
                {"coverage", "speech_outcome", "segments"},
                "speech",
            ),
            r,
            context("vad"),
        )
        outcomes = (transcript.source_outcome, speech.source_outcome)
        if outcomes == (
            TranscriptSourceOutcome.NO_LEXICAL_CONTENT,
            SpeechSourceOutcome.NONE_DETECTED,
        ):
            transcript = replace(transcript, source_outcome=TranscriptSourceOutcome.NO_SPEECH)
        elif outcomes not in {
            (TranscriptSourceOutcome.TRANSCRIPT_AVAILABLE, SpeechSourceOutcome.SPEECH_DETECTED),
            (TranscriptSourceOutcome.NO_LEXICAL_CONTENT, SpeechSourceOutcome.SPEECH_DETECTED),
        }:
            raise LocalMediaEvidenceError("lexical/VAD outcome disagreement")
        ids = cls._identities(p["producer_identities"], r)
        bounds = cls._bounds(p["timing_error_bounds"], r)
        service_sha256 = ids[0].service_sha256
        return TimedSpeechEvidence(
            transcript,
            speech,
            ids,
            bounds,
            TimedSpeechInvocationTrace(
                r.endpoint_url,
                r.identity_sha256,
                _sha(raw),
                canonical_sha256(
                    [
                        x.__dict__
                        if hasattr(x, "__dict__")
                        else {n: getattr(x, n) for n in x.__dataclass_fields__}
                        for x in ids
                    ]
                ),
                service_sha256,
            ),
        )

    @staticmethod
    def _coverage(v: object, r: Any) -> Coverage:
        raw = _obj(
            v,
            {"source_id", "source_sha256", "clock_id", "time_base", "in_tick", "out_tick", "outcome"},
            "coverage",
        )
        time_base = _obj(raw["time_base"], {"numerator", "denominator"}, "coverage.time_base")
        expected = {
            "source_id": r.source_id,
            "source_sha256": r.source_sha256,
            "clock_id": r.clock_id,
            "time_base": {
                "numerator": r.time_base.numerator,
                "denominator": r.time_base.denominator,
            },
            "in_tick": r.requested_in_tick,
            "out_tick": r.requested_out_tick,
            "outcome": "complete",
        }
        strict = {
            "source_id": _text(raw["source_id"], "coverage.source_id"),
            "source_sha256": _text(raw["source_sha256"], "coverage.source_sha256"),
            "clock_id": _text(raw["clock_id"], "coverage.clock_id"),
            "time_base": {
                "numerator": _integer(time_base["numerator"], "coverage.time_base.numerator"),
                "denominator": _integer(
                    time_base["denominator"], "coverage.time_base.denominator"
                ),
            },
            "in_tick": _integer(raw["in_tick"], "coverage.in_tick"),
            "out_tick": _integer(raw["out_tick"], "coverage.out_tick"),
            "outcome": _text(raw["outcome"], "coverage.outcome"),
        }
        if strict != expected:
            raise LocalMediaEvidenceError("coverage must be exact and complete")
        return Coverage(
            r.source_id,
            r.source_sha256,
            r.clock_id,
            r.time_base,
            r.requested_in_tick,
            r.requested_out_tick,
            CoverageOutcome.COMPLETE,
        )

    @classmethod
    def _transcript(
        cls, i: dict[str, object], r: Any, c: EvidenceContext
    ) -> TranscriptSet:
        if _boolean(i["truncated"], "transcript.truncated"):
            raise LocalMediaEvidenceError("truncated transcript")
        touch = _obj(i["boundary_touch"], {"left", "right"}, "boundary_touch")
        touch_left = _boolean(touch["left"], "boundary_touch.left")
        touch_right = _boolean(touch["right"], "boundary_touch.right")
        comp = _obj(i["completeness"], {"segment", "word", "sentence"}, "completeness")
        expected_word = "complete" if r.word_timing_capability == "required" else "not_applicable"
        if comp != {
            "segment": "complete",
            "word": expected_word,
            "sentence": "not_applicable",
        }:
            raise LocalMediaEvidenceError("completeness capability drift")
        words = tuple(
            TranscriptWord(
                _text(x["word_id"], "word.word_id"),
                r.source_id,
                r.source_sha256,
                r.clock_id,
                r.time_base,
                _integer(x["in_tick"], "word.in_tick"),
                _integer(x["out_tick"], "word.out_tick"),
                _text(x["text"], "word.text"),
            )
            for x in (
                _obj(item, {"word_id", "in_tick", "out_tick", "text"}, "word")
                for item in _arr(i["words"], "words")
            )
        )
        sentences = tuple(
            TranscriptSentence(
                _text(x["sentence_id"], "sentence.sentence_id"),
                r.source_id,
                r.source_sha256,
                r.clock_id,
                r.time_base,
                _integer(x["in_tick"], "sentence.in_tick"),
                _integer(x["out_tick"], "sentence.out_tick"),
                _text_array(x["word_ids"], "sentence.word_ids"),
                _text(x["text"], "sentence.text"),
            )
            for x in (
                _obj(
                    item,
                    {"sentence_id", "in_tick", "out_tick", "word_ids", "text"},
                    "sentence",
                )
                for item in _arr(i["sentences"], "sentences")
            )
        )
        segments = tuple(
            TranscriptSegment(
                _text(x["segment_id"], "segment.segment_id"),
                r.source_id,
                r.source_sha256,
                r.clock_id,
                r.time_base,
                _integer(x["in_tick"], "segment.in_tick"),
                _integer(x["out_tick"], "segment.out_tick"),
                _text_array(x["sentence_ids"], "segment.sentence_ids"),
                _text(x["text"], "segment.text"),
            )
            for x in (
                _obj(
                    item,
                    {"segment_id", "in_tick", "out_tick", "sentence_ids", "text"},
                    "segment",
                )
                for item in _arr(i["segments"], "segments")
            )
        )
        try:
            outcome = TranscriptSourceOutcome(
                _text(i["lexical_outcome"], "transcript.lexical_outcome")
            )
        except ValueError as error:
            raise LocalMediaEvidenceError("invalid lexical outcome") from error
        if outcome not in {
            TranscriptSourceOutcome.TRANSCRIPT_AVAILABLE,
            TranscriptSourceOutcome.NO_LEXICAL_CONTENT,
        }:
            raise LocalMediaEvidenceError("indeterminate lexical outcome")
        if (
            r.word_timing_capability == "required"
            and outcome is TranscriptSourceOutcome.TRANSCRIPT_AVAILABLE
            and not words
        ):
            raise LocalMediaEvidenceError("required word timing is missing")
        return TranscriptSet(
            "timed-speech:transcript",
            c,
            cls._coverage(i["coverage"], r),
            outcome,
            TranscriptCompleteness(
                EvidenceCompleteness.COMPLETE,
                EvidenceCompleteness(expected_word),
                EvidenceCompleteness.NOT_APPLICABLE,
            ),
            segments,
            words,
            sentences,
            touch_left,
            touch_right,
            False,
        )

    @classmethod
    def _speech(
        cls, i: dict[str, object], r: Any, c: EvidenceContext
    ) -> SpeechActivitySet:
        seg = []
        for item in _arr(i["segments"], "vad segments"):
            x = _obj(
                item,
                {"speech_segment_id", "in_tick", "out_tick", "confidence_ppm"},
                "vad segment",
            )
            if x["confidence_ppm"] is not None:
                raise LocalMediaEvidenceError("FSMN confidence must be null")
            seg.append(
                SpeechActivitySegment(
                    _text(x["speech_segment_id"], "vad.speech_segment_id"),
                    r.source_id,
                    r.source_sha256,
                    r.clock_id,
                    r.time_base,
                    _integer(x["in_tick"], "vad.in_tick"),
                    _integer(x["out_tick"], "vad.out_tick"),
                    None,
                )
            )
        try:
            outcome = SpeechSourceOutcome(
                _text(i["speech_outcome"], "speech.speech_outcome")
            )
        except ValueError as error:
            raise LocalMediaEvidenceError("invalid speech outcome") from error
        if outcome not in {SpeechSourceOutcome.SPEECH_DETECTED, SpeechSourceOutcome.NONE_DETECTED}:
            raise LocalMediaEvidenceError("indeterminate VAD")
        return SpeechActivitySet(
            "timed-speech:speech", c, cls._coverage(i["coverage"], r), outcome, tuple(seg)
        )

    @staticmethod
    def _identities(
        v: object, r: Any
    ) -> tuple[TimedSpeechProducerIdentity, TimedSpeechProducerIdentity]:
        items = _arr(v, "identities")
        if len(items) != 2:
            raise LocalMediaEvidenceError("two identities required")
        result = []
        for x, e in zip(items, r.expected_producers, strict=True):
            fields = {
                "producer_kind",
                "provider_id",
                "provider_version",
                "funasr_version",
                "torch_version",
                "device",
                "model_id",
                "model_revision",
                "model_sha256",
                "producer_id",
                "producer_version",
                "generation_policy_sha256",
                "detector_sha256",
                "calibration_policy_sha256",
                "calibration_record_sha256",
                "service_sha256",
                "inference_kind",
            }
            closed = _obj(x, fields, "producer identity")
            values = {name: _text(closed[name], f"identity.{name}") for name in fields}
            identity = TimedSpeechProducerIdentity(**values)  # type: ignore[arg-type]
            for n in e.__dataclass_fields__:
                if n != "timing_error_bound_tick" and getattr(identity, n) != getattr(e, n):
                    raise LocalMediaSourceError("producer identity drift")
            if (
                identity.provider_id,
                identity.provider_version,
                identity.funasr_version,
                identity.torch_version,
                identity.device,
            ) != (r.provider_id, r.provider_version, r.funasr_version, r.torch_version, r.device):
                raise LocalMediaSourceError("profile identity drift")
            result.append(identity)
        return cast(tuple[TimedSpeechProducerIdentity, TimedSpeechProducerIdentity], tuple(result))

    @staticmethod
    def _bounds(
        v: object, r: Any
    ) -> tuple[TimedSpeechTimingErrorBound, TimedSpeechTimingErrorBound]:
        bounds = _obj(v, {"asr", "vad"}, "bounds")
        result = []
        for e in r.expected_producers:
            x = _obj(bounds[e.producer_kind], {"early_tick", "late_tick", "time_base"}, "bound")
            early = _integer(x["early_tick"], "bound.early_tick")
            late = _integer(x["late_tick"], "bound.late_tick")
            bound_time_base = _obj(x["time_base"], {"numerator", "denominator"}, "bound.time_base")
            if (
                not 0 < early <= e.timing_error_bound_tick
                or not 0 < late <= e.timing_error_bound_tick
                or _integer(bound_time_base["numerator"], "bound.time_base.numerator")
                != r.time_base.numerator
                or _integer(bound_time_base["denominator"], "bound.time_base.denominator")
                != r.time_base.denominator
            ):
                raise LocalMediaEvidenceError("invalid timing error bound")
            result.append(TimedSpeechTimingErrorBound(e.producer_kind, early, late, r.time_base))
        return cast(
            tuple[TimedSpeechTimingErrorBound, TimedSpeechTimingErrorBound], tuple(result)
        )
