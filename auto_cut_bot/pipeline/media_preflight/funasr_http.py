"""Strict loopback HTTP adapter for timed SenseVoice/FSMN evidence."""

# pyright: reportArgumentType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol, cast

import httpx
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

from .models import LocalMediaEvidenceError, LocalMediaSourceError, LocalMediaToolError
from .speech_port import (
    TimedSpeechEvidence,
    TimedSpeechEvidenceRequest,
    TimedSpeechInvocationTrace,
    TimedSpeechProducerIdentity,
)


class _Transport(Protocol):
    def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        body_path: Path,
        timeout_seconds: int,
        max_response_bytes: int,
    ) -> tuple[int, bytes]: ...


class _HttpxTransport:
    def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        body_path: Path,
        timeout_seconds: int,
        max_response_bytes: int,
    ) -> tuple[int, bytes]:
        try:
            with (
                body_path.open("rb") as body,
                httpx.stream(
                    "POST",
                    url,
                    headers=dict(headers),
                    content=body,
                    timeout=float(timeout_seconds),
                    follow_redirects=False,
                ) as response,
            ):
                raw = bytearray()
                for chunk in response.iter_bytes():
                    if len(raw) + len(chunk) > max_response_bytes:
                        raise LocalMediaToolError("response exceeded byte bound")
                    raw.extend(chunk)
            return response.status_code, bytes(raw)
        except httpx.TimeoutException as e:
            raise LocalMediaToolError("timed speech service timed out") from e
        except LocalMediaToolError:
            raise
        except (httpx.HTTPError, OSError) as e:
            raise LocalMediaToolError("timed speech invocation failed") from e


def _obj(v: object, fields: set[str], name: str) -> dict[str, object]:
    if type(v) is not dict or set(cast(dict[object, object], v)) != fields:
        raise LocalMediaEvidenceError(f"{name} schema is not closed")
    return cast(dict[str, object], v)


def _arr(v: object, name: str) -> list[dict[str, object]]:
    if type(v) is not list or any(type(x) is not dict for x in cast(list[object], v)):
        raise LocalMediaEvidenceError(f"{name} must be object array")
    return cast(list[dict[str, object]], v)


def _sha(v: bytes) -> str:
    return "sha256:" + hashlib.sha256(v).hexdigest()


class FunASRHttpTimedSpeechEvidencePort:
    def __init__(self, *, transport: _Transport | None = None) -> None:
        self._transport = transport or _HttpxTransport()

    def produce(self, r: TimedSpeechEvidenceRequest) -> TimedSpeechEvidence:
        manifest = json.dumps(r.to_mapping(), sort_keys=True, separators=(",", ":")).encode()
        headers = {
            "Content-Type": "application/octet-stream",
            "X-Timed-Speech-Manifest": base64.b64encode(manifest).decode(),
            "X-Timed-Speech-Request-SHA256": r.identity_sha256,
        }
        status, raw = self._transport.post(
            r.endpoint_url,
            headers=headers,
            body_path=r.source_path,
            timeout_seconds=r.timeout_seconds,
            max_response_bytes=r.max_response_bytes,
        )
        if len(raw) > r.max_response_bytes:
            raise LocalMediaToolError("response exceeded byte bound")
        if status != 200:
            raise LocalMediaToolError(f"timed speech HTTP {status} ({_sha(raw)})")
        try:
            p = json.loads(raw.decode())
        except Exception as e:
            raise LocalMediaEvidenceError("response is not UTF-8 JSON") from e
        return self._parse(
            _obj(
                p,
                {
                    "schema_version",
                    "request_identity_sha256",
                    "source",
                    "container",
                    "audio_clock",
                    "requested_range",
                    "timed_speech_policy_sha256",
                    "transcript_capability",
                    "producer_identities",
                    "timing_error_bounds",
                    "transcript",
                    "speech_activity",
                },
                "response",
            ),
            r,
            raw,
        )

    @classmethod
    def _parse(
        cls, p: dict[str, object], r: TimedSpeechEvidenceRequest, raw: bytes
    ) -> TimedSpeechEvidence:
        if (
            p["schema_version"] != "timed-speech-evidence-response-v1"
            or p["request_identity_sha256"] != r.identity_sha256
        ):
            raise LocalMediaSourceError("request identity drift")
        if p["source"] != {"source_id": r.source_id, "source_sha256": r.source_sha256} or p[
            "container"
        ] != {"media_type": "video/mp4", "safe_suffix": ".mp4"}:
            raise LocalMediaSourceError("source identity drift")
        if (
            p["audio_clock"] != r.to_mapping()["audio_clock"]
            or p["requested_range"] != r.to_mapping()["requested_range"]
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
                    "outcome",
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
            _obj(p["speech_activity"], {"coverage", "outcome", "segments"}, "speech"),
            r,
            context("vad"),
        )
        if (transcript.source_outcome, speech.source_outcome) not in {
            (TranscriptSourceOutcome.TRANSCRIPT_AVAILABLE, SpeechSourceOutcome.SPEECH_DETECTED),
            (TranscriptSourceOutcome.NO_LEXICAL_CONTENT, SpeechSourceOutcome.SPEECH_DETECTED),
            (TranscriptSourceOutcome.NO_SPEECH, SpeechSourceOutcome.NONE_DETECTED),
        }:
            raise LocalMediaEvidenceError("ASR/VAD outcome disagreement")
        ids = cls._identities(p["producer_identities"], r)
        cls._bounds(p["timing_error_bounds"], r)
        return TimedSpeechEvidence(
            transcript,
            speech,
            ids,
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
            ),
        )

    @staticmethod
    def _coverage(v: object, r: TimedSpeechEvidenceRequest) -> Coverage:
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
        if v != expected:
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
        cls, i: dict[str, object], r: TimedSpeechEvidenceRequest, c: EvidenceContext
    ) -> TranscriptSet:
        if i["truncated"] is not False:
            raise LocalMediaEvidenceError("truncated transcript")
        comp = _obj(i["completeness"], {"segment", "word", "sentence"}, "completeness")
        expected_word = "complete" if r.word_timing_capability == "required" else "not_applicable"
        if comp != {"segment": "complete", "word": expected_word, "sentence": "complete"}:
            raise LocalMediaEvidenceError("completeness capability drift")
        words = tuple(
            TranscriptWord(
                str(x["word_id"]),
                r.source_id,
                r.source_sha256,
                r.clock_id,
                r.time_base,
                int(x["in_tick"]),
                int(x["out_tick"]),
                str(x["text"]),
            )
            for x in _arr(i["words"], "words")
        )
        sentences = tuple(
            TranscriptSentence(
                str(x["sentence_id"]),
                r.source_id,
                r.source_sha256,
                r.clock_id,
                r.time_base,
                int(x["in_tick"]),
                int(x["out_tick"]),
                tuple(cast(list[str], x["word_ids"])),
                str(x["text"]),
            )
            for x in _arr(i["sentences"], "sentences")
        )
        segments = tuple(
            TranscriptSegment(
                str(x["segment_id"]),
                r.source_id,
                r.source_sha256,
                r.clock_id,
                r.time_base,
                int(x["in_tick"]),
                int(x["out_tick"]),
                tuple(cast(list[str], x["sentence_ids"])),
                str(x["text"]),
            )
            for x in _arr(i["segments"], "segments")
        )
        outcome = TranscriptSourceOutcome(str(i["outcome"]))
        if outcome not in {
            TranscriptSourceOutcome.TRANSCRIPT_AVAILABLE,
            TranscriptSourceOutcome.NO_LEXICAL_CONTENT,
            TranscriptSourceOutcome.NO_SPEECH,
        }:
            raise LocalMediaEvidenceError("indeterminate transcript")
        if (
            r.word_timing_capability == "required"
            and outcome is TranscriptSourceOutcome.TRANSCRIPT_AVAILABLE
            and (
                not words
                or tuple(y for s in sentences for y in s.word_ids)
                != tuple(w.word_id for w in words)
            )
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
                EvidenceCompleteness.COMPLETE,
            ),
            segments,
            words,
            sentences,
        )

    @classmethod
    def _speech(
        cls, i: dict[str, object], r: TimedSpeechEvidenceRequest, c: EvidenceContext
    ) -> SpeechActivitySet:
        seg = []
        for x in _arr(i["segments"], "vad segments"):
            if x.get("confidence_ppm") is not None:
                raise LocalMediaEvidenceError("FSMN confidence must be null")
            seg.append(
                SpeechActivitySegment(
                    str(x["speech_segment_id"]),
                    r.source_id,
                    r.source_sha256,
                    r.clock_id,
                    r.time_base,
                    int(x["in_tick"]),
                    int(x["out_tick"]),
                    None,
                )
            )
        outcome = SpeechSourceOutcome(str(i["outcome"]))
        if outcome not in {SpeechSourceOutcome.SPEECH_DETECTED, SpeechSourceOutcome.NONE_DETECTED}:
            raise LocalMediaEvidenceError("indeterminate VAD")
        return SpeechActivitySet(
            "timed-speech:speech", c, cls._coverage(i["coverage"], r), outcome, tuple(seg)
        )

    @staticmethod
    def _identities(
        v: object, r: TimedSpeechEvidenceRequest
    ) -> tuple[TimedSpeechProducerIdentity, TimedSpeechProducerIdentity]:
        items = _arr(v, "identities")
        if len(items) != 2:
            raise LocalMediaEvidenceError("two identities required")
        result = []
        for x, e in zip(items, r.expected_producers, strict=True):
            identity = TimedSpeechProducerIdentity(**x)  # type: ignore[arg-type]
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
    def _bounds(v: object, r: TimedSpeechEvidenceRequest) -> None:
        bounds = _obj(v, {"asr", "vad"}, "bounds")
        for e in r.expected_producers:
            x = _obj(bounds[e.producer_kind], {"early_tick", "late_tick", "time_base"}, "bound")
            if (
                not 0 < int(x["early_tick"]) <= e.timing_error_bound_tick
                or not 0 < int(x["late_tick"]) <= e.timing_error_bound_tick
            ):
                raise LocalMediaEvidenceError("invalid timing error bound")
