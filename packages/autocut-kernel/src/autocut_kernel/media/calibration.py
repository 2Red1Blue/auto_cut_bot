"""Closed integer-only calibration measurements for native timed-speech producers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .types import MediaValidationError, TickRange, TimeBase, require_pts


class CalibrationRecordError(MediaValidationError):
    """Raised when native calibration evidence is not closed or reproducible."""


class CalibrationProducer(str, Enum):
    """The only producers eligible for a native timed-speech calibration record."""

    ASR = "asr"
    VAD = "vad"


_INFERENCE_KIND = {
    CalibrationProducer.ASR: "sensevoice-word-timestamp",
    CalibrationProducer.VAD: "fsmn-vad-direct",
}


def _text(value: object, field_name: str) -> str:
    if type(value) is not str or not value.strip():  # noqa: E721
        raise CalibrationRecordError(f"{field_name} must be non-empty text")
    return value


def _time_base(value: object, field_name: str) -> TimeBase:
    if type(value) is not TimeBase:  # noqa: E721
        raise CalibrationRecordError(f"{field_name} must be an exact TimeBase")
    return value


def _range(value: object, field_name: str) -> TickRange:
    if type(value) is not TickRange:  # noqa: E721
        raise CalibrationRecordError(f"{field_name} must be an exact TickRange")
    return value


def _producer(value: object, field_name: str) -> CalibrationProducer:
    if type(value) is not CalibrationProducer:  # noqa: E721
        raise CalibrationRecordError(f"{field_name} must be a CalibrationProducer")
    return value


def _positive_tick(value: object, field_name: str) -> int:
    try:
        tick = require_pts(value, field_name)
    except MediaValidationError as error:
        raise CalibrationRecordError(str(error)) from error
    if tick <= 0:
        raise CalibrationRecordError(f"{field_name} must be positive")
    return tick


@dataclass(frozen=True, slots=True)
class CalibrationAnchor:
    """One independently-reviewed expected native-timing interval."""

    anchor_id: str
    producer: CalibrationProducer
    producer_id: str
    time_base: TimeBase
    expected_range: TickRange

    def __post_init__(self) -> None:
        _text(self.anchor_id, "anchor.anchor_id")
        _producer(self.producer, "anchor.producer")
        _text(self.producer_id, "anchor.producer_id")
        _time_base(self.time_base, "anchor.time_base")
        _range(self.expected_range, "anchor.expected_range")


@dataclass(frozen=True, slots=True)
class CalibrationObservation:
    """One direct native producer observation, without interpolation or rounding."""

    observation_id: str
    producer: CalibrationProducer
    producer_id: str
    inference_kind: str
    time_base: TimeBase
    observed_range: TickRange

    def __post_init__(self) -> None:
        _text(self.observation_id, "observation.observation_id")
        producer = _producer(self.producer, "observation.producer")
        _text(self.producer_id, "observation.producer_id")
        if self.inference_kind != _INFERENCE_KIND[producer]:
            raise CalibrationRecordError("observation.inference_kind is invalid for producer")
        _time_base(self.time_base, "observation.time_base")
        _range(self.observed_range, "observation.observed_range")


@dataclass(frozen=True, slots=True)
class CalibrationAnchorMatch:
    """One complete, unambiguous expected-to-observed interval alignment."""

    anchor: CalibrationAnchor
    observation: CalibrationObservation

    def __post_init__(self) -> None:
        if type(self.anchor) is not CalibrationAnchor:  # noqa: E721
            raise CalibrationRecordError("match.anchor must be an exact CalibrationAnchor")
        if type(self.observation) is not CalibrationObservation:  # noqa: E721
            raise CalibrationRecordError("match.observation must be an exact CalibrationObservation")
        if self.anchor.producer is not self.observation.producer:
            raise CalibrationRecordError("match producer must agree")
        if self.anchor.producer_id != self.observation.producer_id:
            raise CalibrationRecordError("match producer_id must agree")
        if self.anchor.time_base != self.observation.time_base:
            raise CalibrationRecordError("match time_base must agree")

    @property
    def early_tick(self) -> int:
        """Maximum early endpoint error, in the declared source-native time base."""
        expected = self.anchor.expected_range
        observed = self.observation.observed_range
        return max(0, expected.start_pts - observed.start_pts, expected.end_pts - observed.end_pts)

    @property
    def late_tick(self) -> int:
        """Maximum late endpoint error, in the declared source-native time base."""
        expected = self.anchor.expected_range
        observed = self.observation.observed_range
        return max(0, observed.start_pts - expected.start_pts, observed.end_pts - expected.end_pts)

    @property
    def absolute_tick(self) -> int:
        """Maximum absolute endpoint error, derived only from exact integer ticks."""
        return max(self.early_tick, self.late_tick)


@dataclass(frozen=True, slots=True)
class ProducerCalibrationMeasurement:
    """Complete measured bounds for exactly one native ASR or VAD producer."""

    producer: CalibrationProducer
    producer_id: str
    inference_kind: str
    time_base: TimeBase
    matches: tuple[CalibrationAnchorMatch, ...]
    accepted_bound_tick: int

    def __post_init__(self) -> None:
        producer = _producer(self.producer, "measurement.producer")
        producer_id = _text(self.producer_id, "measurement.producer_id")
        if self.inference_kind != _INFERENCE_KIND[producer]:
            raise CalibrationRecordError("measurement.inference_kind is invalid for producer")
        time_base = _time_base(self.time_base, "measurement.time_base")
        if type(self.matches) is not tuple or not self.matches:  # noqa: E721
            raise CalibrationRecordError("measurement.matches must be a non-empty tuple")
        anchor_ids: set[str] = set()
        observation_ids: set[str] = set()
        for position, match in enumerate(self.matches):
            if type(match) is not CalibrationAnchorMatch:  # noqa: E721
                raise CalibrationRecordError(f"measurement.matches[{position}] is invalid")
            if match.anchor.producer is not producer or match.anchor.producer_id != producer_id:
                raise CalibrationRecordError("measurement match producer must agree")
            if match.anchor.time_base != time_base:
                raise CalibrationRecordError("measurement match time_base must agree")
            if match.anchor.anchor_id in anchor_ids or match.observation.observation_id in observation_ids:
                raise CalibrationRecordError("measurement matches must not duplicate anchors or observations")
            anchor_ids.add(match.anchor.anchor_id)
            observation_ids.add(match.observation.observation_id)
        accepted = _positive_tick(self.accepted_bound_tick, "measurement.accepted_bound_tick")
        if accepted != self.absolute_maximum_tick:
            raise CalibrationRecordError("measurement.accepted_bound_tick must equal the measured absolute maximum")

    @property
    def early_maximum_tick(self) -> int:
        return max(match.early_tick for match in self.matches)

    @property
    def late_maximum_tick(self) -> int:
        return max(match.late_tick for match in self.matches)

    @property
    def absolute_maximum_tick(self) -> int:
        return max(match.absolute_tick for match in self.matches)


@dataclass(frozen=True, slots=True)
class CalibrationRecord:
    """The two required non-zero native producer measurements for one validation run."""

    asr: ProducerCalibrationMeasurement
    vad: ProducerCalibrationMeasurement

    def __post_init__(self) -> None:
        if type(self.asr) is not ProducerCalibrationMeasurement:  # noqa: E721
            raise CalibrationRecordError("record.asr must be an exact ProducerCalibrationMeasurement")
        if type(self.vad) is not ProducerCalibrationMeasurement:  # noqa: E721
            raise CalibrationRecordError("record.vad must be an exact ProducerCalibrationMeasurement")
        if self.asr.producer is not CalibrationProducer.ASR:
            raise CalibrationRecordError("record.asr must use the ASR producer")
        if self.vad.producer is not CalibrationProducer.VAD:
            raise CalibrationRecordError("record.vad must use the VAD producer")
        if self.asr.producer_id == self.vad.producer_id:
            raise CalibrationRecordError("record ASR and VAD producer_id values must be distinct")
