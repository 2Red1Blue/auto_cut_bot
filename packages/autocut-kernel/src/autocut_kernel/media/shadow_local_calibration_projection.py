"""Replay actual local-window raw bytes against independent ordered anchors.

Zero errors and empty observations are measurements, not accepted bounds. This
module neither infers missing anchors nor promotes a full-source calibration to
the local-extraction path. The shared projector owns timestamp conversion,
word grouping and VAD merging; this module only aligns its actual observations.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from .calibration import CalibrationAnchor, CalibrationAnchorMatch, CalibrationObservation
from .local_speech_window import LocalSpeechWindowRequest
from .local_speech_window_codec import decode_local_speech_window_response
from .local_speech_window_projection import project_local_speech_window
from .root_evidence import SpeechActivitySet, TranscriptSet
from .shadow_calibration_raw import ShadowCalibrationProducerIdentity
from .shadow_local_calibration import (
    ShadowLocalCalibrationCase,
    ShadowLocalCalibrationError,
    build_shadow_local_request,
)
from .types import TickRange


def _match(
    anchors: tuple[CalibrationAnchor, ...],
    intervals: tuple[tuple[str, TickRange], ...],
    identity: ShadowCalibrationProducerIdentity,
    case: ShadowLocalCalibrationCase,
) -> tuple[CalibrationAnchorMatch, ...]:
    if len(anchors) != len(intervals):
        raise ShadowLocalCalibrationError("local anchor alignment must be complete ordered one-to-one")
    return tuple(
        CalibrationAnchorMatch(anchor, CalibrationObservation(
            observation_id, identity.producer, identity.producer_id, identity.inference_kind,
            case.extraction.clock_id, case.extraction.time_base, interval,
        ))
        for anchor, (observation_id, interval) in zip(anchors, intervals, strict=True)
    )


@dataclass(frozen=True, slots=True)
class ShadowLocalCalibrationProjection:
    """Immutable raw-bound measurements; construction replays, not authorizes.

    Do not retain the mutable native JSON objects from DecodedLocalSpeechWindow.
    The original bytes and immutable projected values are the durable inputs.
    No caller can supply replacement observations, matches or a self-pass flag.
    """

    case: ShadowLocalCalibrationCase
    request: LocalSpeechWindowRequest
    raw_response: bytes
    transcript: TranscriptSet = field(init=False)
    speech_activity: SpeechActivitySet = field(init=False)
    asr_matches: tuple[CalibrationAnchorMatch, ...] = field(init=False)
    vad_matches: tuple[CalibrationAnchorMatch, ...] = field(init=False)

    def __post_init__(self) -> None:
        if type(self.case) is not ShadowLocalCalibrationCase or type(self.request) is not LocalSpeechWindowRequest:
            raise ShadowLocalCalibrationError("projection requires exact local case and request")
        if self.request != build_shadow_local_request(self.case, max_response_bytes=self.request.max_response_bytes):
            raise ShadowLocalCalibrationError("request is not derived from this exact local case")
        try:
            evidence = project_local_speech_window(decode_local_speech_window_response(self.raw_response, self.request))
        except ValueError as error:
            raise ShadowLocalCalibrationError("local calibration raw response failed replay") from error
        asr, vad = self.case.producer_identities
        asr_matches = _match(
            self.case.asr_anchors,
            tuple((word.word_id, TickRange(word.in_tick, word.out_tick)) for word in evidence.transcript.words),
            asr, self.case,
        )
        vad_matches = _match(
            self.case.vad_anchors,
            tuple((segment.speech_segment_id, TickRange(segment.in_tick, segment.out_tick))
                  for segment in evidence.speech_activity.segments),
            vad, self.case,
        )
        object.__setattr__(self, "transcript", evidence.transcript)
        object.__setattr__(self, "speech_activity", evidence.speech_activity)
        object.__setattr__(self, "asr_matches", asr_matches)
        object.__setattr__(self, "vad_matches", vad_matches)

    @property
    def case_sha256(self) -> str:
        return self.case.canonical_hash

    @property
    def request_sha256(self) -> str:
        return self.request.canonical_hash

    @property
    def response_sha256(self) -> str:
        return "sha256:" + hashlib.sha256(self.raw_response).hexdigest()


def project_shadow_local_calibration(
    raw: bytes, *, case: ShadowLocalCalibrationCase, request: LocalSpeechWindowRequest,
) -> ShadowLocalCalibrationProjection:
    return ShadowLocalCalibrationProjection(case, request, raw)
