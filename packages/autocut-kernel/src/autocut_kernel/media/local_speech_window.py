"""Shared provider-window values, never Source authorization or Admission."""

from __future__ import annotations

from dataclasses import dataclass

from .local_audio_window import LocalAudioWindowSpec
from .types import canonical_sha256, sha256_prefixed


class LocalSpeechWindowError(ValueError):
    """The measured local-window wire cannot be independently closed."""


def _integer(value: object, name: str, *, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum:
        raise LocalSpeechWindowError(f"{name} must be an integer >= {minimum}")
    return value


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value.strip():
        raise LocalSpeechWindowError(f"{name} must be nonempty exact text")
    value.encode("utf-8")
    return value


@dataclass(frozen=True, slots=True)
class DecodedLocalPcmReport:
    """Path-free measured extraction facts; no committed/accepted authority."""

    source_sha256: str
    spec_sha256: str
    decoder_identity_sha256: str
    pcm_sha256: str
    wav_sha256: str
    wav_byte_length: int
    sample_rate: int
    channels: int
    sample_count: int
    decoded_frames: int

    def __post_init__(self) -> None:
        for name in ("source_sha256", "spec_sha256", "decoder_identity_sha256",
                     "pcm_sha256", "wav_sha256"):
            sha256_prefixed(getattr(self, name), name)
        for name in ("wav_byte_length", "sample_rate", "channels", "sample_count", "decoded_frames"):
            _integer(getattr(self, name), name)

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": "decoded-local-pcm-report-v1",
            "source_sha256": self.source_sha256, "spec_sha256": self.spec_sha256,
            "decoder_identity_sha256": self.decoder_identity_sha256,
            "pcm_sha256": self.pcm_sha256, "wav_sha256": self.wav_sha256,
            "wav_byte_length": self.wav_byte_length, "sample_rate": self.sample_rate,
            "channels": self.channels, "sample_count": self.sample_count,
            "decoded_frames": self.decoded_frames,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    def validate_for(self, spec: LocalAudioWindowSpec) -> None:
        if type(spec) is not LocalAudioWindowSpec:
            raise LocalSpeechWindowError("report requires an exact extraction spec")
        if (self.source_sha256, self.spec_sha256, self.decoder_identity_sha256,
                self.sample_rate, self.channels, self.sample_count) != (
            spec.source_sha256, spec.canonical_hash, spec.decoder_identity_sha256,
            spec.sample_rate, spec.channels, spec.expected_samples,
        ):
            raise LocalSpeechWindowError("extraction report source/spec/decoder/sample identity drift")
        pcm_size = self.sample_count * self.channels * 4
        if (not pcm_size < self.wav_byte_length <= pcm_size + 4096
                or pcm_size > spec.max_pcm_bytes or self.decoded_frames > spec.max_decode_frames):
            raise LocalSpeechWindowError("extraction report byte/frame limits do not close")


@dataclass(frozen=True, slots=True)
class LocalSpeechWindowPolicy:
    """Projection of one loaded profile, independently compared by the server."""

    service_profile_sha256: str
    asr_producer_id: str
    asr_generation_policy_sha256: str
    vad_producer_id: str
    vad_generation_policy_sha256: str
    utterance_gap_milliseconds: int
    vad_merge_gap_milliseconds: int

    def __post_init__(self) -> None:
        for name in ("service_profile_sha256", "asr_generation_policy_sha256",
                     "vad_generation_policy_sha256"):
            sha256_prefixed(getattr(self, name), name)
        _text(self.asr_producer_id, "ASR producer")
        _text(self.vad_producer_id, "VAD producer")
        if self.asr_producer_id == self.vad_producer_id:
            raise LocalSpeechWindowError("ASR and VAD producers must be distinct")
        _integer(self.utterance_gap_milliseconds, "utterance gap", minimum=0)
        _integer(self.vad_merge_gap_milliseconds, "VAD merge gap", minimum=0)

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": "local-speech-window-policy-v1",
            "service_profile_sha256": self.service_profile_sha256,
            "asr_producer_id": self.asr_producer_id,
            "asr_generation_policy_sha256": self.asr_generation_policy_sha256,
            "vad_producer_id": self.vad_producer_id,
            "vad_generation_policy_sha256": self.vad_generation_policy_sha256,
            "utterance_gap_milliseconds": self.utterance_gap_milliseconds,
            "vad_merge_gap_milliseconds": self.vad_merge_gap_milliseconds,
        }


@dataclass(frozen=True, slots=True)
class LocalSpeechWindowRequest:
    extraction: LocalAudioWindowSpec
    policy: LocalSpeechWindowPolicy
    binding_sha256: str
    max_response_bytes: int

    def __post_init__(self) -> None:
        if type(self.extraction) is not LocalAudioWindowSpec or type(self.policy) is not LocalSpeechWindowPolicy:
            raise LocalSpeechWindowError("window requires exact extraction and profile policy")
        sha256_prefixed(self.binding_sha256, "Command/window binding")
        _integer(self.max_response_bytes, "response byte bound")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": "local-speech-window-request-v1",
            "extraction": self.extraction.to_mapping(), "policy": self.policy.to_mapping(),
            "binding_sha256": self.binding_sha256, "max_response_bytes": self.max_response_bytes,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())
