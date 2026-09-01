"""Durable V23 candidate-decision aggregate and strict codec contracts."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace

import pytest
from autocut_kernel.media import (
    V23_CANDIDATE_DECISION_SET_SCHEMA,
    TimedEvidenceValidationError,
    V23CandidateWindowCompileOutcome,
    V23CandidateWindowCompilePolicy,
    compile_v23_candidate_decision_set,
    decode_v23_candidate_decision_set,
    decode_v23_candidate_decision_set_json,
    verify_v23_candidate_decision_set,
)
from autocut_kernel.media.timed_evidence_codec import (
    decode_candidate_evidence_window_plan,
)
from autocut_kernel.media.types import MediaValidationError, TickRange

from tests.media.test_timed_evidence import _initial_plan
from tests.media.test_v23_candidate_evidence_window import _policy
from tests.vlm.test_semantic_pack_v4 import _parse, _wire


def _support(start: int, end: int) -> dict[str, object]:
    return {
        "support_kind": "video_observation",
        "interval_ms": {"start_ms": start, "end_ms": end, "uncertainty_ms": 0},
        "confidence": "0.90",
    }


def _three_candidate_pack():
    wire = deepcopy(_wire())
    intervals = ((20, 30), (70, 80), (0, 90))
    facts = []
    events = []
    for ordinal, (start, end) in enumerate(intervals, start=1):
        fact = deepcopy(wire["facts"][0])
        fact["local_fact_id"] = f"fact_{ordinal}"
        fact["support"] = _support(start, end)
        facts.append(fact)
        event = deepcopy(wire["events"][0])
        event["local_event_id"] = f"event_{ordinal}"
        event["fact_refs"] = [f"fact_{ordinal}"]
        event["support"] = _support(start, end)
        events.append(event)
    wire["facts"] = facts
    wire["events"] = events

    original = wire["candidate_hypotheses"][0]
    candidates = []
    definitions = (
        ("candidate_1", ("event_1",), "event_1", ("event_1",), (20, 30)),
        (
            "candidate_2",
            ("event_1", "event_2"),
            "event_1",
            ("event_2",),
            (20, 80),
        ),
        ("candidate_3", ("event_3",), "event_3", ("event_3",), (0, 90)),
    )
    for ordinal, (local_id, supporting, anchor, payoff, interval) in enumerate(
        definitions, start=1
    ):
        candidate = deepcopy(original)
        candidate["local_candidate_id"] = local_id
        candidate["anchor_event_ref"] = anchor
        candidate["supporting_event_refs"] = list(supporting)
        candidate["context_event_refs"] = []
        candidate["payoff_event_refs"] = list(payoff)
        candidate["measurements"][0]["fact_refs"] = [f"fact_{ordinal}"]
        candidate["measurements"][0]["event_refs"] = [anchor]
        candidate["support"] = _support(*interval)
        candidates.append(candidate)
    wire["candidate_hypotheses"] = candidates
    return _parse(wire)


def _build(pack=None, policy: V23CandidateWindowCompilePolicy | None = None):
    pack = _three_candidate_pack() if pack is None else pack
    manifest = pack.events[0].support.manifest
    return compile_v23_candidate_decision_set(
        pack,
        manifest,
        manifest.frame_pts_index_set,
        _policy(max_duration=50) if policy is None else policy,
    )


def test_build_retains_every_candidate_and_every_routing_outcome() -> None:
    pack = _three_candidate_pack()

    decision_set = _build(pack)

    assert decision_set.schema_version == V23_CANDIDATE_DECISION_SET_SCHEMA
    assert decision_set.candidate_ids == tuple(
        sorted(candidate.candidate_id for candidate in pack.candidate_hypotheses)
    )
    assert tuple(item.candidate_id for item in decision_set.decisions) == (
        decision_set.candidate_ids
    )
    assert {item.outcome for item in decision_set.decisions} == {
        V23CandidateWindowCompileOutcome.ELIGIBLE,
        V23CandidateWindowCompileOutcome.EPISODE_ARC,
        V23CandidateWindowCompileOutcome.INDETERMINATE,
    }
    assert decision_set.eligible_count == 1
    assert decision_set.noneligible_count == 2


def test_empty_candidate_pack_is_a_closed_empty_decision_set() -> None:
    wire = _wire()
    wire["candidate_hypotheses"] = []
    pack = _parse(wire)

    decision_set = _build(pack)

    assert decision_set.candidate_ids == ()
    assert decision_set.decisions == ()
    assert decision_set.eligible_count == 0


def test_empty_candidate_pack_still_rejects_every_invalid_compile_binding() -> None:
    wire = _wire()
    wire["candidate_hypotheses"] = []
    pack = _parse(wire)
    manifest = pack.events[0].support.manifest
    frame_index = manifest.frame_pts_index_set
    policy = _policy(max_duration=50)

    foreign_manifest = replace(
        manifest,
        preprocess_policy_sha256="sha256:" + "9" * 64,
    )
    with pytest.raises(TimedEvidenceValidationError, match="window manifest"):
        compile_v23_candidate_decision_set(pack, foreign_manifest, frame_index, policy)

    wrong_clock_policy = replace(
        policy,
        time_base=replace(policy.time_base, denominator=policy.time_base.denominator + 1),
    )
    with pytest.raises(TimedEvidenceValidationError, match="time base"):
        compile_v23_candidate_decision_set(pack, manifest, frame_index, wrong_clock_policy)

    foreign_frame_index = replace(
        frame_index,
        frame_pts_index_set_id="foreign-frame-index",
    )
    with pytest.raises(TimedEvidenceValidationError, match="frame PTS index"):
        compile_v23_candidate_decision_set(pack, manifest, foreign_frame_index, policy)


def test_build_and_strict_codec_round_trip_are_deterministic() -> None:
    first = _build()
    second = _build()

    decoded = decode_v23_candidate_decision_set(first.to_mapping())

    assert decoded == first == second
    assert decoded.canonical_hash == first.canonical_hash == second.canonical_hash
    assert (
        decode_v23_candidate_decision_set_json(
            json.dumps(first.to_mapping(), separators=(",", ":"), sort_keys=True).encode(),
            max_bytes=1_000_000,
        )
        == first
    )


@pytest.mark.parametrize("mutation", ["omit", "duplicate", "reorder", "substitute"])
def test_decoder_rejects_incomplete_or_noncanonical_candidate_coverage(mutation: str) -> None:
    mapping = _build().to_mapping()
    decisions = mapping["decisions"]
    assert isinstance(decisions, list)
    if mutation == "omit":
        decisions.pop()
    elif mutation == "duplicate":
        decisions.append(deepcopy(decisions[0]))
    elif mutation == "reorder":
        decisions.reverse()
    else:
        decisions[0]["candidate_id"] = "sha256:" + "f" * 64

    with pytest.raises((MediaValidationError, TimedEvidenceValidationError)):
        decode_v23_candidate_decision_set(mapping)


def test_decoder_rejects_cross_binding_tampering_before_recompute() -> None:
    mapping = _build().to_mapping()
    mapping["decisions"][0]["semantic_pack_sha256"] = "sha256:" + "e" * 64

    with pytest.raises(TimedEvidenceValidationError, match="exact decision-set bindings"):
        decode_v23_candidate_decision_set(mapping)


def test_live_dependency_recompute_rejects_changed_semantic_pack() -> None:
    pack = _three_candidate_pack()
    decision_set = _build(pack)
    wire = _wire()
    wire["events"][0]["summary"] = "Changed committed Event semantics."
    changed = _parse(wire)
    manifest = changed.events[0].support.manifest

    with pytest.raises(TimedEvidenceValidationError, match="semantic pack"):
        verify_v23_candidate_decision_set(
            decision_set,
            changed,
            manifest,
            manifest.frame_pts_index_set,
            _policy(max_duration=50),
        )


def test_live_dependency_recompute_accepts_the_exact_committed_inputs() -> None:
    pack = _three_candidate_pack()
    decision_set = _build(pack)
    manifest = pack.events[0].support.manifest

    assert (
        verify_v23_candidate_decision_set(
            decision_set,
            pack,
            manifest,
            manifest.frame_pts_index_set,
            _policy(max_duration=50),
        )
        is decision_set
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda mapping: mapping.__setitem__("unknown", 1),
        lambda mapping: mapping.pop("compile_policy"),
        lambda mapping: mapping["compile_policy"].__setitem__("max_seed_duration_pts", 1.5),
        lambda mapping: mapping["compile_policy"].__setitem__("max_source_coverage_ppm", True),
        lambda mapping: mapping["decisions"][0].__setitem__("outcome", "invented"),
        lambda mapping: mapping["decisions"][0].__setitem__("window", []),
    ],
)
def test_mapping_decoder_is_closed_and_exact(mutation) -> None:
    mapping = _build().to_mapping()
    mutation(mapping)

    with pytest.raises((MediaValidationError, TimedEvidenceValidationError)):
        decode_v23_candidate_decision_set(mapping)


@pytest.mark.parametrize(
    "raw,max_bytes",
    [
        (b'{"schema_version":"a","schema_version":"b"}', 1_000),
        (b"\xff", 1_000),
        (b'{"value":NaN}', 1_000),
        (b"{}", 1),
        (b"", 1_000),
    ],
)
def test_json_decoder_rejects_ambiguous_invalid_or_oversized_bytes(
    raw: bytes, max_bytes: int
) -> None:
    with pytest.raises(MediaValidationError):
        decode_v23_candidate_decision_set_json(raw, max_bytes=max_bytes)


def test_json_decoder_rejects_duplicate_key_in_otherwise_valid_decision_set() -> None:
    decision_set = _build()
    raw = json.dumps(decision_set.to_mapping(), separators=(",", ":"), sort_keys=True).encode()
    field = json.dumps(
        {"schema_version": decision_set.schema_version},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()[1:-1]
    assert raw.count(field) == 1
    duplicate = raw.replace(field, field + b"," + field)

    with pytest.raises(MediaValidationError, match="duplicate"):
        decode_v23_candidate_decision_set_json(
            duplicate,
            max_bytes=len(duplicate),
        )


def test_decision_set_constructor_rejects_inconsistent_source_range() -> None:
    decision_set = _build()

    with pytest.raises(TimedEvidenceValidationError, match="exact decision-set bindings"):
        replace(decision_set, source_range=TickRange(1000, 1090))


def test_existing_v3_candidate_plan_codec_still_round_trips_unchanged() -> None:
    plan, _manifest, _policy_value = _initial_plan()

    decoded = decode_candidate_evidence_window_plan(plan.to_mapping())

    assert decoded == plan
    assert decoded.to_mapping() == plan.to_mapping()
    assert decoded.canonical_hash == plan.canonical_hash
