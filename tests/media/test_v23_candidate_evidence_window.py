"""V23 compatibility projection from rich V4 semantics to bounded physical evidence."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest
from autocut_kernel.media import (
    TimedEvidenceValidationError,
    V23CandidateWindowCompileOutcome,
    V23CandidateWindowCompilePolicy,
    V23CandidateWindowCompileReason,
    compile_v23_candidate_evidence_window,
    verify_v23_candidate_evidence_window_decision,
)
from autocut_kernel.media.types import TickRange, TimeBase
from autocut_kernel.media.v23_candidate_evidence_window import _snap_outward

from tests.vlm import frame_pts_set
from tests.vlm.test_semantic_pack_v4 import _parse, _wire


def _foreign_manifest_same_ticks(manifest):
    """Keep numeric ticks/time base while changing every Source identity field."""

    foreign_hash = "sha256:" + "9" * 64
    foreign_context = replace(
        manifest.frame_pts_index_set.context,
        source_id="foreign-source",
        source_sha256=foreign_hash,
        clock_id="foreign-video-clock",
    )
    foreign_coverage = replace(
        manifest.frame_pts_index_set.coverage,
        source_id="foreign-source",
        source_sha256=foreign_hash,
        clock_id="foreign-video-clock",
    )
    foreign_index = replace(
        manifest.frame_pts_index_set,
        frame_pts_index_set_id="foreign-frame-index",
        context=foreign_context,
        coverage=foreign_coverage,
    )
    return replace(
        manifest,
        source_id="foreign-source",
        source_sha256=foreign_hash,
        source_clock_id="foreign-video-clock",
        frame_pts_index_set=foreign_index,
    )


def _policy(
    *,
    max_gap: int = 10,
    max_duration: int = 80,
    max_source_coverage_ppm: int = 800_000,
) -> V23CandidateWindowCompilePolicy:
    pack = _parse()
    return V23CandidateWindowCompilePolicy(
        strategy_version="v23-direct-event-window-v1",
        time_base=pack.events[0].support.source_interval.source_time_base,
        initial_left_expansion_pts=5,
        initial_right_expansion_pts=5,
        max_direct_event_gap_pts=max_gap,
        max_seed_duration_pts=max_duration,
        max_source_coverage_ppm=max_source_coverage_ppm,
    )


def _pack_with_events(
    *,
    direct_intervals: tuple[tuple[int, int], ...],
    context_intervals: tuple[tuple[int, int], ...] = (),
    candidate_interval: tuple[int, int] | None = None,
):
    wire = deepcopy(_wire())
    all_intervals = direct_intervals + context_intervals
    facts = []
    events = []
    for ordinal, (start, end) in enumerate(all_intervals, start=1):
        fact = deepcopy(wire["facts"][0])
        fact["local_fact_id"] = f"fact_{ordinal}"
        fact["support"]["interval_ms"] = {
            "start_ms": start,
            "end_ms": end,
            "uncertainty_ms": 0,
        }
        facts.append(fact)

        event = deepcopy(wire["events"][0])
        event["local_event_id"] = f"event_{ordinal}"
        event["fact_refs"] = [f"fact_{ordinal}"]
        event["support"]["interval_ms"] = {
            "start_ms": start,
            "end_ms": end,
            "uncertainty_ms": 0,
        }
        events.append(event)

    wire["facts"] = facts
    wire["events"] = events
    direct_ids = [f"event_{ordinal}" for ordinal in range(1, len(direct_intervals) + 1)]
    context_ids = [
        f"event_{ordinal}" for ordinal in range(len(direct_intervals) + 1, len(all_intervals) + 1)
    ]
    candidate = wire["candidate_hypotheses"][0]
    candidate["anchor_event_ref"] = direct_ids[0]
    candidate["supporting_event_refs"] = direct_ids
    candidate["context_event_refs"] = context_ids
    candidate["payoff_event_refs"] = [direct_ids[-1]]
    if candidate_interval is None:
        candidate_interval = (direct_intervals[0][0], direct_intervals[-1][1])
    candidate["support"]["interval_ms"] = {
        "start_ms": candidate_interval[0],
        "end_ms": candidate_interval[1],
        "uncertainty_ms": 0,
    }
    return _parse(wire)


def _compile(pack, policy: V23CandidateWindowCompilePolicy | None = None):
    candidate = pack.candidate_hypotheses[0]
    manifest = candidate.support.manifest
    return compile_v23_candidate_evidence_window(
        candidate,
        pack,
        manifest,
        manifest.frame_pts_index_set,
        _policy() if policy is None else policy,
    )


def test_context_and_full_candidate_support_do_not_expand_physical_window() -> None:
    pack = _pack_with_events(
        direct_intervals=((20, 30),),
        context_intervals=((80, 90),),
        candidate_interval=(5, 95),
    )

    decision = _compile(pack)

    assert decision.outcome is V23CandidateWindowCompileOutcome.ELIGIBLE
    assert decision.reason is V23CandidateWindowCompileReason.BOUNDED_DIRECT_EVENTS
    assert len(decision.direct_event_supports) == 1
    assert decision.direct_event_supports[0].event_ref == pack.events[0].event_id
    assert decision.direct_event_hull == TickRange(1019, 1031)
    assert decision.window is not None
    assert decision.window.coarse_range == TickRange(1019, 1031)
    assert decision.window.current_range == TickRange(1010, 1050)
    assert (
        decision.window.current_range
        != pack.candidate_hypotheses[0].support.source_interval.coarse_range
    )


def test_anchor_support_and_payoff_duplicates_form_one_dependency() -> None:
    pack = _parse()

    decision = _compile(pack)

    assert len(decision.direct_event_supports) == 1
    assert decision.direct_event_supports[0].event_ref == pack.events[0].event_id


def test_disconnected_direct_events_are_episode_arc_and_never_reach_exact_span() -> None:
    pack = _pack_with_events(direct_intervals=((20, 30), (70, 80)))

    decision = _compile(pack, _policy(max_gap=10))

    assert decision.outcome is V23CandidateWindowCompileOutcome.EPISODE_ARC
    assert decision.reason is V23CandidateWindowCompileReason.DISCONNECTED_DIRECT_EVENT_REGIONS
    assert decision.window is None
    assert len(decision.merged_uncertainty_regions) == 2


@pytest.mark.parametrize(
    ("policy", "reason"),
    [
        (
            _policy(max_duration=20),
            V23CandidateWindowCompileReason.UNCERTAINTY_HULL_EXCEEDS_DURATION,
        ),
        (
            _policy(max_source_coverage_ppm=200_000),
            V23CandidateWindowCompileReason.UNCERTAINTY_HULL_EXCEEDS_SOURCE_RATIO,
        ),
    ],
)
def test_oversized_local_projection_is_indeterminate(policy, reason) -> None:
    pack = _pack_with_events(direct_intervals=((20, 30), (35, 50)))

    decision = _compile(pack, policy)

    assert decision.outcome is V23CandidateWindowCompileOutcome.INDETERMINATE
    assert decision.reason is reason
    assert decision.window is None


@pytest.mark.parametrize(
    ("policy", "reason"),
    [
        (
            _policy(max_duration=30),
            V23CandidateWindowCompileReason.PHYSICAL_WINDOW_EXCEEDS_DURATION,
        ),
        (
            _policy(max_source_coverage_ppm=300_000),
            V23CandidateWindowCompileReason.PHYSICAL_WINDOW_EXCEEDS_SOURCE_RATIO,
        ),
    ],
)
def test_expansion_and_frame_snap_cannot_escape_locality_limits(policy, reason) -> None:
    pack = _pack_with_events(direct_intervals=((20, 30),))

    decision = _compile(pack, policy)

    assert decision.outcome is V23CandidateWindowCompileOutcome.INDETERMINATE
    assert decision.reason is reason
    assert decision.window is None


def test_sparse_frame_index_cannot_silently_truncate_direct_support() -> None:
    pack = _pack_with_events(direct_intervals=((5, 15),))

    decision = _compile(pack)

    assert decision.outcome is V23CandidateWindowCompileOutcome.INDETERMINATE
    assert (
        decision.reason
        is V23CandidateWindowCompileReason.FRAME_INDEX_CANNOT_COVER_DIRECT_EVENT_HULL
    )
    assert decision.window is None


@pytest.mark.parametrize(
    ("second_interval", "max_gap", "outcome"),
    [
        ((34, 44), 0, V23CandidateWindowCompileOutcome.ELIGIBLE),
        ((35, 45), 0, V23CandidateWindowCompileOutcome.EPISODE_ARC),
        ((35, 45), 1, V23CandidateWindowCompileOutcome.ELIGIBLE),
    ],
)
def test_mapping_uncertainty_controls_region_connectivity(
    second_interval, max_gap, outcome
) -> None:
    pack = _pack_with_events(direct_intervals=((20, 30), second_interval))

    decision = _compile(pack, _policy(max_gap=max_gap))

    assert decision.outcome is outcome


def test_mapping_uncertainty_is_clipped_at_the_source_boundary() -> None:
    pack = _pack_with_events(direct_intervals=((0, 5),))

    decision = _compile(pack)

    assert decision.direct_event_supports[0].uncertainty_expanded_range.start_pts == 1000
    assert decision.source_range.start_pts == 1000


def test_decision_binds_event_semantics_not_only_candidate_payload() -> None:
    first_pack = _parse()
    wire = _wire()
    wire["events"][0]["summary"] = "The same candidate now depends on changed event semantics."
    second_pack = _parse(wire)

    first = _compile(first_pack)
    second = _compile(second_pack)

    assert first.vlm_candidate_sha256 == second.vlm_candidate_sha256
    assert first.semantic_pack_sha256 != second.semantic_pack_sha256
    assert first.direct_support_dependency_sha256 != second.direct_support_dependency_sha256


def test_candidate_must_be_an_exact_member_of_the_supplied_pack() -> None:
    first_pack = _parse()
    wire = _wire()
    wire["candidate_hypotheses"][0]["reason"] = "A different immutable candidate."
    other_candidate = _parse(wire).candidate_hypotheses[0]
    manifest = first_pack.candidate_hypotheses[0].support.manifest

    with pytest.raises(TimedEvidenceValidationError, match="not a member"):
        compile_v23_candidate_evidence_window(
            other_candidate,
            first_pack,
            manifest,
            manifest.frame_pts_index_set,
            _policy(),
        )


def test_wrong_source_with_same_time_base_is_rejected_before_projection() -> None:
    pack = _parse()
    candidate = pack.candidate_hypotheses[0]
    other_manifest = _foreign_manifest_same_ticks(candidate.support.manifest)

    with pytest.raises(TimedEvidenceValidationError, match="does not bind"):
        compile_v23_candidate_evidence_window(
            candidate,
            pack,
            other_manifest,
            other_manifest.frame_pts_index_set,
            _policy(),
        )


def test_manifest_frame_index_and_policy_clock_are_exactly_fenced() -> None:
    pack = _parse()
    candidate = pack.candidate_hypotheses[0]
    manifest = candidate.support.manifest
    other_manifest = _foreign_manifest_same_ticks(manifest)

    with pytest.raises(TimedEvidenceValidationError, match="does not bind"):
        compile_v23_candidate_evidence_window(
            candidate,
            pack,
            manifest,
            other_manifest.frame_pts_index_set,
            _policy(),
        )
    with pytest.raises(TimedEvidenceValidationError, match="time base"):
        compile_v23_candidate_evidence_window(
            candidate,
            pack,
            manifest,
            manifest.frame_pts_index_set,
            replace(_policy(), time_base=TimeBase(1, 90_000)),
        )


def test_recompute_verifier_accepts_exact_result_and_rejects_tampering() -> None:
    pack = _parse()
    candidate = pack.candidate_hypotheses[0]
    manifest = candidate.support.manifest
    policy = _policy()
    decision = compile_v23_candidate_evidence_window(
        candidate,
        pack,
        manifest,
        manifest.frame_pts_index_set,
        policy,
    )

    assert (
        verify_v23_candidate_evidence_window_decision(
            decision,
            candidate,
            pack,
            manifest,
            manifest.frame_pts_index_set,
            policy,
        )
        is decision
    )
    assert decision == compile_v23_candidate_evidence_window(
        candidate,
        pack,
        manifest,
        manifest.frame_pts_index_set,
        policy,
    )
    assert (
        decision.canonical_hash
        == compile_v23_candidate_evidence_window(
            candidate,
            pack,
            manifest,
            manifest.frame_pts_index_set,
            policy,
        ).canonical_hash
    )

    tampered = replace(
        decision,
        direct_support_dependency_sha256="sha256:" + "f" * 64,
    )
    with pytest.raises(TimedEvidenceValidationError, match="independent recomputation"):
        verify_v23_candidate_evidence_window_decision(
            tampered,
            candidate,
            pack,
            manifest,
            manifest.frame_pts_index_set,
            policy,
        )


def test_decision_rejects_a_window_with_mismatched_dependency_hashes() -> None:
    decision = _compile(_parse())
    assert decision.window is not None
    tampered_window = replace(
        decision.window,
        frame_pts_index_set_sha256="sha256:" + "f" * 64,
    )

    with pytest.raises(TimedEvidenceValidationError, match="exact compile decision"):
        replace(decision, window=tampered_window)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("source_id", "other-source"),
        ("source_sha256", "sha256:" + "e" * 64),
        ("source_clock_id", "other-clock"),
        ("source_time_base", TimeBase(1, 90_000)),
        ("vlm_request_identity_sha256", "sha256:" + "d" * 64),
    ],
)
def test_decision_rejects_every_mismatched_window_identity(field_name, value) -> None:
    decision = _compile(_parse())
    assert decision.window is not None

    with pytest.raises(TimedEvidenceValidationError, match="exact compile decision"):
        replace(decision, window=replace(decision.window, **{field_name: value}))


def test_frozen_decision_normalizes_sequence_inputs_to_immutable_tuples() -> None:
    decision = _compile(_parse())

    normalized = replace(
        decision,
        direct_event_supports=list(decision.direct_event_supports),
        merged_uncertainty_regions=list(decision.merged_uncertainty_regions),
    )

    assert type(normalized.direct_event_supports) is tuple
    assert type(normalized.merged_uncertainty_regions) is tuple
    assert normalized == decision


def test_degenerate_pts_lattice_returns_no_window_instead_of_raising() -> None:
    index = frame_pts_set(
        source_id="source-001",
        source_sha256="sha256:" + "a" * 64,
        clock_id="video-clock-0",
        time_base=TimeBase(1, 1_000),
        origin_tick=1_000,
        end_tick=1_100,
        ticks=(1_100,),
    )

    assert _snap_outward(index, TickRange(1_000, 1_100), 1_010, 1_090) is None


def test_policy_rejects_source_coverage_above_one_million_ppm() -> None:
    with pytest.raises(TimedEvidenceValidationError, match="must not exceed"):
        _policy(max_source_coverage_ppm=1_000_001)


def test_decision_self_rejects_out_of_range_support_and_inconsistent_hull() -> None:
    decision = _compile(_parse())
    support = decision.direct_event_supports[0]
    escaped_support = replace(
        support,
        coarse_range=TickRange(900, 910),
        uncertainty_expanded_range=TickRange(899, 911),
    )

    with pytest.raises(TimedEvidenceValidationError, match="stay in the Source range"):
        replace(decision, direct_event_supports=(escaped_support,))
    with pytest.raises(TimedEvidenceValidationError, match="equal the hull"):
        replace(decision, direct_event_hull=TickRange(1018, 1031))
