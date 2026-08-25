from __future__ import annotations

import pytest
from autocut_kernel.media import (
    CalibrationAnchor,
    CalibrationAnchorMatch,
    CalibrationMeasurementSummary,
    CalibrationObservation,
    CalibrationProducer,
    CalibrationRecordError,
    ProducerCalibrationMeasurement,
    TimeBase,
)
from autocut_kernel.media.types import TickRange

TIME_BASE = TimeBase(1, 1_000)
CLOCK_ID = "source-audio-clock"


def _inference_kind(producer: CalibrationProducer) -> str:
    return "sensevoice-word-timestamp" if producer is CalibrationProducer.ASR else "fsmn-vad-direct"


def _match(
    producer: CalibrationProducer,
    producer_id: str,
    expected: TickRange,
    observed: TickRange,
    *,
    clock_id: str = CLOCK_ID,
    time_base: TimeBase = TIME_BASE,
    ordinal: int = 1,
) -> CalibrationAnchorMatch:
    return CalibrationAnchorMatch(
        CalibrationAnchor(f"anchor-{ordinal}", producer, producer_id, clock_id, time_base, expected),
        CalibrationObservation(
            f"observation-{ordinal}",
            producer,
            producer_id,
            _inference_kind(producer),
            clock_id,
            time_base,
            observed,
        ),
    )


def _measurement(
    producer: CalibrationProducer,
    producer_id: str,
    matches: tuple[CalibrationAnchorMatch, ...],
    *,
    clock_id: str = CLOCK_ID,
    time_base: TimeBase = TIME_BASE,
) -> ProducerCalibrationMeasurement:
    return ProducerCalibrationMeasurement(
        producer,
        producer_id,
        _inference_kind(producer),
        clock_id,
        time_base,
        matches,
        max(match.absolute_tick for match in matches),
    )


def test_anchor_match_uses_exact_integer_early_late_and_absolute_errors() -> None:
    match = _match(
        CalibrationProducer.ASR,
        "sensevoice-asr",
        TickRange(10, 20),
        TickRange(8, 23),
    )

    assert match.early_tick == 2
    assert match.late_tick == 3
    assert match.absolute_tick == 3


def test_summary_requires_measured_positive_bounds_and_one_shared_clock() -> None:
    asr = _measurement(
        CalibrationProducer.ASR,
        "sensevoice-asr",
        (_match(CalibrationProducer.ASR, "sensevoice-asr", TickRange(10, 20), TickRange(8, 23)),),
    )
    vad = _measurement(
        CalibrationProducer.VAD,
        "fsmn-vad",
        (_match(CalibrationProducer.VAD, "fsmn-vad", TickRange(30, 40), TickRange(32, 39)),),
    )

    summary = CalibrationMeasurementSummary(asr, vad)

    assert summary.asr.accepted_bound_tick == 3
    assert summary.vad.accepted_bound_tick == 2
    assert summary.asr.clock_id == summary.vad.clock_id == CLOCK_ID


def test_zero_result_and_non_measured_bound_are_rejected() -> None:
    match = _match(
        CalibrationProducer.ASR,
        "sensevoice-asr",
        TickRange(10, 20),
        TickRange(10, 20),
    )
    with pytest.raises(CalibrationRecordError, match="positive"):
        _measurement(CalibrationProducer.ASR, "sensevoice-asr", (match,))

    nonzero = _match(
        CalibrationProducer.ASR,
        "sensevoice-asr",
        TickRange(10, 20),
        TickRange(9, 20),
    )
    with pytest.raises(CalibrationRecordError, match="measured absolute maximum"):
        ProducerCalibrationMeasurement(
            CalibrationProducer.ASR,
            "sensevoice-asr",
            "sensevoice-word-timestamp",
            CLOCK_ID,
            TIME_BASE,
            (nonzero,),
            2,
        )


def test_type_time_base_and_producer_identity_violations_are_rejected() -> None:
    with pytest.raises(CalibrationRecordError, match="inference_kind"):
        CalibrationObservation(
            "observation-1",
            CalibrationProducer.ASR,
            "sensevoice-asr",
            "fsmn-vad-direct",
            CLOCK_ID,
            TIME_BASE,
            TickRange(1, 2),
        )
    with pytest.raises(CalibrationRecordError, match="TimeBase"):
        CalibrationAnchor(
            "anchor-1",
            CalibrationProducer.ASR,
            "sensevoice-asr",
            CLOCK_ID,
            object(),  # type: ignore[arg-type]
            TickRange(1, 2),
        )
    anchor = CalibrationAnchor(
        "anchor-2",
        CalibrationProducer.ASR,
        "sensevoice-asr",
        CLOCK_ID,
        TIME_BASE,
        TickRange(10, 20),
    )
    observation = CalibrationObservation(
        "observation-2",
        CalibrationProducer.ASR,
        "sensevoice-asr",
        "sensevoice-word-timestamp",
        CLOCK_ID,
        TimeBase(1, 90_000),
        TickRange(10, 20),
    )
    with pytest.raises(CalibrationRecordError, match="time_base must agree"):
        CalibrationAnchorMatch(anchor, observation)


def test_summary_denies_equal_time_base_with_different_source_clocks() -> None:
    asr = _measurement(
        CalibrationProducer.ASR,
        "sensevoice-asr",
        (_match(CalibrationProducer.ASR, "sensevoice-asr", TickRange(10, 20), TickRange(9, 20)),),
    )
    vad = _measurement(
        CalibrationProducer.VAD,
        "fsmn-vad",
        (
            _match(
                CalibrationProducer.VAD,
                "fsmn-vad",
                TickRange(30, 40),
                TickRange(31, 40),
                clock_id="different-source-audio-clock",
            ),
        ),
        clock_id="different-source-audio-clock",
    )

    with pytest.raises(CalibrationRecordError, match="clock_id values must agree"):
        CalibrationMeasurementSummary(asr, vad)
