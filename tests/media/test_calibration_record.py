from __future__ import annotations

import pytest
from autocut_kernel.media import (
    CalibrationAnchor,
    CalibrationAnchorMatch,
    CalibrationObservation,
    CalibrationProducer,
    CalibrationRecord,
    CalibrationRecordError,
    ProducerCalibrationMeasurement,
    TimeBase,
)
from autocut_kernel.media.types import TickRange

TIME_BASE = TimeBase(1, 1_000)


def _match(
    producer: CalibrationProducer,
    producer_id: str,
    expected: TickRange,
    observed: TickRange,
    *,
    ordinal: int = 1,
) -> CalibrationAnchorMatch:
    inference_kind = (
        "sensevoice-word-timestamp"
        if producer is CalibrationProducer.ASR
        else "fsmn-vad-direct"
    )
    return CalibrationAnchorMatch(
        CalibrationAnchor(f"anchor-{ordinal}", producer, producer_id, TIME_BASE, expected),
        CalibrationObservation(
            f"observation-{ordinal}",
            producer,
            producer_id,
            inference_kind,
            TIME_BASE,
            observed,
        ),
    )


def _measurement(
    producer: CalibrationProducer,
    producer_id: str,
    matches: tuple[CalibrationAnchorMatch, ...],
) -> ProducerCalibrationMeasurement:
    inference_kind = (
        "sensevoice-word-timestamp"
        if producer is CalibrationProducer.ASR
        else "fsmn-vad-direct"
    )
    return ProducerCalibrationMeasurement(
        producer,
        producer_id,
        inference_kind,
        TIME_BASE,
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


def test_record_requires_measured_positive_bounds_for_both_distinct_producers() -> None:
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

    record = CalibrationRecord(asr, vad)

    assert record.asr.accepted_bound_tick == 3
    assert record.vad.accepted_bound_tick == 2


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
            TIME_BASE,
            (nonzero,),
            2,
        )


def test_invalid_type_time_base_and_producer_identities_are_rejected() -> None:
    with pytest.raises(CalibrationRecordError, match="inference_kind"):
        CalibrationObservation(
            "observation-1",
            CalibrationProducer.ASR,
            "sensevoice-asr",
            "fsmn-vad-direct",
            TIME_BASE,
            TickRange(1, 2),
        )
    with pytest.raises(CalibrationRecordError, match="TimeBase"):
        CalibrationAnchor(
            "anchor-1",
            CalibrationProducer.ASR,
            "sensevoice-asr",
            object(),  # type: ignore[arg-type]
            TickRange(1, 2),
        )
    anchor = CalibrationAnchor(
        "anchor-2",
        CalibrationProducer.ASR,
        "sensevoice-asr",
        TIME_BASE,
        TickRange(10, 20),
    )
    observation = CalibrationObservation(
        "observation-2",
        CalibrationProducer.ASR,
        "sensevoice-asr",
        "sensevoice-word-timestamp",
        TimeBase(1, 90_000),
        TickRange(10, 20),
    )
    with pytest.raises(CalibrationRecordError, match="time_base must agree"):
        CalibrationAnchorMatch(anchor, observation)
    asr = _measurement(
        CalibrationProducer.ASR,
        "shared-producer",
        (_match(CalibrationProducer.ASR, "shared-producer", TickRange(10, 20), TickRange(9, 20)),),
    )
    vad = _measurement(
        CalibrationProducer.VAD,
        "shared-producer",
        (_match(CalibrationProducer.VAD, "shared-producer", TickRange(30, 40), TickRange(31, 40)),),
    )
    with pytest.raises(CalibrationRecordError, match="must be distinct"):
        CalibrationRecord(asr, vad)
