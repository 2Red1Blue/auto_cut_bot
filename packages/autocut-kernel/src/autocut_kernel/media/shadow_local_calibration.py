"""Content-bound local calibration cases, never accepted calibration authority.

The source provenance and independent anchors are inputs to this value. Their
authenticity belongs to the future committed measurement/acceptance path. No
Record, accepted error bound, future Receipt or derived request hash is a case
input. In particular, the complete service-profile hash is not the nested
native-port identity hash.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import cast

from .calibration import CalibrationAnchor, CalibrationProducer
from .local_audio_window import LocalAudioWindowSpec
from .local_speech_window import LocalSpeechWindowPolicy, LocalSpeechWindowRequest
from .local_speech_window_codec import (
    decode_local_audio_window_spec,
    decode_local_speech_window_policy,
)
from .root_evidence_codec import decode_media_evidence_json, decode_time_base
from .shadow_calibration_raw import (
    ShadowCalibrationPolicies,
    ShadowCalibrationProducerIdentity,
    ShadowCalibrationSource,
)
from .types import TickRange, canonical_sha256, require_pts, sha256_prefixed

SHADOW_LOCAL_CALIBRATION_CASE_SCHEMA = "shadow-local-calibration-case-v1"
# This is the algorithm's policy identity, not a measured/accepted bound.
SHADOW_LOCAL_ALIGNMENT_POLICY_SHA256 = canonical_sha256({
    "schema_version": "shadow-local-anchor-alignment-v1",
    "alignment": "ordered-one-to-one",
    "asr_observations": "projected-words",
    "vad_observations": "policy-merged-speech-intervals",
    "empty_alignment": "both-empty-only",
    "error": "maximum-absolute-endpoint-ticks",
})


class ShadowLocalCalibrationError(ValueError):
    """A local measurement's content or anchor alignment does not close."""


def _text(value: object) -> str:
    if type(value) is not str or not value.strip():
        raise ShadowLocalCalibrationError("case text must be exact and nonempty")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError as error:
        raise ShadowLocalCalibrationError("case text must be valid UTF-8") from error
    return value


def _object(value: object, fields: tuple[str, ...]) -> dict[str, object]:
    if type(value) is not dict or set(cast(dict[object, object], value)) != set(fields):
        raise ShadowLocalCalibrationError("case object has missing or unknown fields")
    return cast(dict[str, object], value)


def _array(value: object) -> list[object]:
    if type(value) is not list:
        raise ShadowLocalCalibrationError("case wire sequence must be an array")
    return cast(list[object], value)


def _anchor_mapping(anchor: CalibrationAnchor) -> dict[str, object]:
    return {
        "anchor_id": anchor.anchor_id, "producer": anchor.producer.value,
        "producer_id": anchor.producer_id, "clock_id": anchor.clock_id,
        "time_base": {"numerator": anchor.time_base.numerator,
                      "denominator": anchor.time_base.denominator},
        "expected_range": {"start_pts": anchor.expected_range.start_pts,
                           "end_pts": anchor.expected_range.end_pts},
    }


def _anchor(value: object) -> CalibrationAnchor:
    r = _object(value, ("anchor_id", "producer", "producer_id", "clock_id",
                        "time_base", "expected_range"))
    interval = _object(r["expected_range"], ("start_pts", "end_pts"))
    return CalibrationAnchor(
        _text(r["anchor_id"]), CalibrationProducer(_text(r["producer"])),
        _text(r["producer_id"]), _text(r["clock_id"]), decode_time_base(r["time_base"]),
        TickRange(require_pts(interval["start_pts"], "anchor start"),
                  require_pts(interval["end_pts"], "anchor end")),
    )


def _producer_identity(value: object) -> ShadowCalibrationProducerIdentity:
    r = _object(value, ("producer_kind", "producer_id", "producer_version",
                        "generation_policy_sha256", "detector_sha256", "calibration_policy_sha256",
                        "model_id", "model_revision", "model_sha256", "inference_kind", "service_sha256"))
    return ShadowCalibrationProducerIdentity(
        CalibrationProducer(_text(r["producer_kind"])), _text(r["producer_id"]),
        _text(r["producer_version"]), _text(r["generation_policy_sha256"]),
        _text(r["detector_sha256"]), _text(r["calibration_policy_sha256"]),
        _text(r["model_id"]), _text(r["model_revision"]), _text(r["model_sha256"]),
        _text(r["inference_kind"]), _text(r["service_sha256"]),
    )


@dataclass(frozen=True, slots=True)
class ShadowLocalCalibrationCase:
    source: ShadowCalibrationSource
    source_provenance_sha256: str
    extraction: LocalAudioWindowSpec
    policy: LocalSpeechWindowPolicy
    native_profile_identity_sha256: str
    policies: ShadowCalibrationPolicies
    producer_identities: tuple[ShadowCalibrationProducerIdentity, ShadowCalibrationProducerIdentity]
    alignment_policy_sha256: str
    asr_anchors: tuple[CalibrationAnchor, ...]
    vad_anchors: tuple[CalibrationAnchor, ...]

    def __post_init__(self) -> None:
        if (type(self.source) is not ShadowCalibrationSource
                or type(self.extraction) is not LocalAudioWindowSpec
                or type(self.policy) is not LocalSpeechWindowPolicy
                or type(self.policies) is not ShadowCalibrationPolicies):
            raise ShadowLocalCalibrationError("case requires exact source/spec/policies")
        for value in (self.source_provenance_sha256, self.native_profile_identity_sha256,
                      self.alignment_policy_sha256):
            sha256_prefixed(_text(value), "local calibration identity")
        if self.alignment_policy_sha256 != SHADOW_LOCAL_ALIGNMENT_POLICY_SHA256:
            raise ShadowLocalCalibrationError("unsupported local anchor alignment policy")
        if self.native_profile_identity_sha256 == self.policy.service_profile_sha256:
            raise ShadowLocalCalibrationError("complete service profile and native identity must differ")
        if (self.source.source_id, self.source.source_sha256) != (
            self.extraction.source_id, self.extraction.source_sha256
        ):
            raise ShadowLocalCalibrationError("case source and extraction identity differ")
        if self.source.blob_byte_length > self.extraction.max_source_bytes:
            raise ShadowLocalCalibrationError("case source exceeds explicit extraction byte bound")
        if (self.policies.word_gap_ms, self.policies.vad_merge_gap_ms) != (
            self.policy.utterance_gap_milliseconds, self.policy.vad_merge_gap_milliseconds
        ):
            raise ShadowLocalCalibrationError("case and request timing policies differ")
        if (type(self.producer_identities) is not tuple or len(self.producer_identities) != 2
                or any(type(item) is not ShadowCalibrationProducerIdentity
                       for item in self.producer_identities)):
            raise ShadowLocalCalibrationError("case requires exact ordered ASR/VAD identities")
        asr, vad = self.producer_identities
        if (asr.producer, vad.producer, asr.producer_id, vad.producer_id,
                asr.generation_policy_sha256, vad.generation_policy_sha256) != (
            CalibrationProducer.ASR, CalibrationProducer.VAD,
            self.policy.asr_producer_id, self.policy.vad_producer_id,
            self.policy.asr_generation_policy_sha256, self.policy.vad_generation_policy_sha256,
        ):
            raise ShadowLocalCalibrationError("case producer identities differ from window policy")
        if asr.service_sha256 != vad.service_sha256:
            raise ShadowLocalCalibrationError("case producers must bind the same service implementation")
        for anchors, identity in ((self.asr_anchors, asr), (self.vad_anchors, vad)):
            if type(anchors) is not tuple or any(type(a) is not CalibrationAnchor for a in anchors):
                raise ShadowLocalCalibrationError("case anchors must be an exact immutable tuple")
            if len({a.anchor_id for a in anchors}) != len(anchors):
                raise ShadowLocalCalibrationError("case anchor IDs must be unique per producer")
            for anchor in anchors:
                if (anchor.producer, anchor.producer_id, anchor.clock_id, anchor.time_base) != (
                    identity.producer, identity.producer_id,
                    self.extraction.clock_id, self.extraction.time_base,
                ):
                    raise ShadowLocalCalibrationError("case anchor producer/clock drift")
                if not self.extraction.requested_range.contains(anchor.expected_range):
                    raise ShadowLocalCalibrationError("independent anchor escapes local requested range")
            if any(left.expected_range.end_pts > right.expected_range.start_pts
                   for left, right in zip(anchors, anchors[1:], strict=False)):
                raise ShadowLocalCalibrationError("case anchors must be ordered and nonoverlapping")
        # Existing producer/anchor DTOs predate strict UTF-8 text checks. Keep the
        # new case closed for direct construction too, without changing them.
        try:
            json.dumps(self.to_mapping(), ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (ValueError, UnicodeError) as error:
            raise ShadowLocalCalibrationError("case contains invalid JSON/UTF-8 text") from error

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": SHADOW_LOCAL_CALIBRATION_CASE_SCHEMA,
            "source": self.source.to_mapping(),
            "source_provenance_sha256": self.source_provenance_sha256,
            "extraction": self.extraction.to_mapping(), "policy": self.policy.to_mapping(),
            "native_profile_identity_sha256": self.native_profile_identity_sha256,
            "policies": {
                "timed_speech_policy_sha256": self.policies.timed_speech_policy_sha256,
                "word_gap_policy_sha256": self.policies.word_gap_policy_sha256,
                "vad_merge_policy_sha256": self.policies.vad_merge_policy_sha256,
                "word_gap_ms": self.policies.word_gap_ms,
                "vad_merge_gap_ms": self.policies.vad_merge_gap_ms,
            },
            "producer_identities": [identity.to_mapping() for identity in self.producer_identities],
            "alignment_policy_sha256": self.alignment_policy_sha256,
            "asr_anchors": [_anchor_mapping(a) for a in self.asr_anchors],
            "vad_anchors": [_anchor_mapping(a) for a in self.vad_anchors],
        }

    def to_bytes(self) -> bytes:
        return json.dumps(self.to_mapping(), sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False).encode("utf-8")

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    @classmethod
    def from_mapping(cls, value: object) -> ShadowLocalCalibrationCase:
        """Decode content only; no Source provenance or service execution proof."""
        try:
            r = _object(value, ("schema_version", "source", "source_provenance_sha256", "extraction",
                                "policy", "native_profile_identity_sha256", "policies",
                                "producer_identities", "alignment_policy_sha256", "asr_anchors", "vad_anchors"))
            if _text(r["schema_version"]) != SHADOW_LOCAL_CALIBRATION_CASE_SCHEMA:
                raise ShadowLocalCalibrationError("unsupported local calibration case schema")
            s = _object(r["source"], ("source_id", "source_sha256", "corpus_member_reference_sha256",
                                     "blob_id", "blob_sha256", "blob_byte_length", "blob_media_type"))
            source = ShadowCalibrationSource(
                _text(s["source_id"]), _text(s["source_sha256"]), _text(s["corpus_member_reference_sha256"]),
                _text(s["blob_id"]), _text(s["blob_sha256"]), require_pts(s["blob_byte_length"], "blob length"),
                _text(s["blob_media_type"]),
            )
            p = _object(r["policies"], ("timed_speech_policy_sha256", "word_gap_policy_sha256",
                                       "vad_merge_policy_sha256", "word_gap_ms", "vad_merge_gap_ms"))
            policies = ShadowCalibrationPolicies(
                _text(p["timed_speech_policy_sha256"]), _text(p["word_gap_policy_sha256"]),
                _text(p["vad_merge_policy_sha256"]), require_pts(p["word_gap_ms"], "word gap"),
                require_pts(p["vad_merge_gap_ms"], "VAD merge gap"),
            )
            producers = _array(r["producer_identities"])
            if len(producers) != 2:
                raise ShadowLocalCalibrationError("case requires exactly two producer identities")
            return cls(
                source, _text(r["source_provenance_sha256"]), decode_local_audio_window_spec(r["extraction"]),
                decode_local_speech_window_policy(r["policy"]), _text(r["native_profile_identity_sha256"]),
                policies, (_producer_identity(producers[0]), _producer_identity(producers[1])),
                _text(r["alignment_policy_sha256"]), tuple(_anchor(a) for a in _array(r["asr_anchors"])),
                tuple(_anchor(a) for a in _array(r["vad_anchors"])),
            )
        except ValueError as error:
            raise ShadowLocalCalibrationError("local calibration case failed closed decoding") from error


def decode_shadow_local_calibration_case(raw: bytes, *, max_bytes: int) -> ShadowLocalCalibrationCase:
    """Strict duplicate-free integer JSON with an explicit preparse byte limit."""
    try:
        return ShadowLocalCalibrationCase.from_mapping(decode_media_evidence_json(raw, max_bytes=max_bytes))
    except ValueError as error:
        raise ShadowLocalCalibrationError("invalid bounded local calibration case JSON") from error


def build_shadow_local_request(
    case: ShadowLocalCalibrationCase, *, max_response_bytes: int,
) -> LocalSpeechWindowRequest:
    if type(case) is not ShadowLocalCalibrationCase:
        raise ShadowLocalCalibrationError("request requires an exact local calibration case")
    return LocalSpeechWindowRequest(case.extraction, case.policy, case.canonical_hash, max_response_bytes)
