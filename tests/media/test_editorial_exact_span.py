"""Stage 3 semantic intent to candidate-local exact-query projection."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_hash
from autocut_kernel.media.types import TickRange, TimeBase, canonical_sha256
from autocut_kernel.physical_edit.dialogue_guard import DialogueRequirement
from autocut_kernel.physical_edit.editorial_exact_span import (
    EDITORIAL_EXACT_SPAN_STRATEGY,
    EditorialExactSpanError,
    EditorialExactSpanIndeterminateError,
    EditorialExactSpanPolicy,
    derive_editorial_exact_span_query,
    minimum_video_ticks,
)
from autocut_kernel.pipeline.committed_timed_media import read_committed_timed_media_evidence
from autocut_kernel.pipeline.editorial_timed_media_inputs import (
    EditorialTimedAlternativeBinding,
    EditorialTimedCandidateBinding,
    read_committed_editorial_timed_media_inputs,
)
from autocut_kernel.semantic_chain.candidate_catalog import Candidate
from autocut_kernel.semantic_chain.editorial_models import SpanPolicy
from autocut_kernel.vlm.models import VlmEditingMode

from tests.authority.editorial_media_fixture import editorial_timed_media_case


def _selected_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    case = editorial_timed_media_case(tmp_path, monkeypatch)
    store, stage3_request, stage3_outcome, batch_request, batch_outcome, resolver, limits = case
    joined = read_committed_editorial_timed_media_inputs(
        store,
        stage3_request=stage3_request,
        stage3_outcome=stage3_outcome,
        media_batch_request=batch_request,
        media_batch_outcome=batch_outcome,
        authority_profile_resolver=resolver,
        limits=limits,
    )
    choice = joined.editorial.values.admission.feasibility.material_search.choices[0]
    row = next(
        item
        for item in joined.alternatives
        if (
            item.story_id,
            item.requirement.evidence_requirement_id,
            item.alternative.alternative_id,
        )
        == (choice.story_id, choice.requirement_id, choice.alternative_key)
    )
    selected = next(
        item for item in row.candidates if item.candidate_ref.canonical_hash == choice.candidate_keys[0]
    )
    child = batch_request.children[selected.episode_index]
    persisted = read_committed_timed_media_evidence(
        store,
        child.request,
        child.outcome,
        authority_profile_resolver=resolver,
        limits=limits,
    )
    pack = joined.predecessors.semantic.inputs[selected.episode_index].semantic_pack.semantic_pack
    raw = pack.candidate_hypotheses[selected.candidate_ordinal]
    candidate = next(
        item
        for item in joined.predecessors.portfolio.values.business.candidate_catalog.candidates
        if item.candidate_id == selected.candidate_ref.object_id
    )
    return choice, row, selected, candidate, pack, raw, persisted.candidates[selected.candidate_ordinal]


def _policy(*, tick: int = 10, time_base: TimeBase = TimeBase(1, 10)) -> EditorialExactSpanPolicy:
    return EditorialExactSpanPolicy(EDITORIAL_EXACT_SPAN_STRATEGY, tick, time_base)


def _derive(
    choice: object,
    row: EditorialTimedAlternativeBinding,
    selected: EditorialTimedCandidateBinding,
    candidate: Candidate,
    pack: object,
    raw: object,
    timed: object,
    *,
    intent: str = "tight",
    policy: EditorialExactSpanPolicy | None = None,
):
    return derive_editorial_exact_span_query(
        admitted_choice=choice,  # type: ignore[arg-type]
        beat=row.beat,
        requirement=row.requirement,
        alternative=row.alternative,
        selected_candidate_ref=selected.candidate_ref,
        candidate=candidate,
        semantic_pack=pack,  # type: ignore[arg-type]
        raw_candidate=raw,  # type: ignore[arg-type]
        timed_evidence=timed,  # type: ignore[arg-type]
        span_intent=intent,  # type: ignore[arg-type]
        policy=_policy() if policy is None else policy,
    )


def test_tight_query_uses_direct_event_not_wider_candidate_support(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    choice, row, selected, candidate, pack, raw, timed = _selected_case(tmp_path, monkeypatch)
    event = next(item for item in pack.events if item.event_id == raw.anchor_event_ref)

    query = _derive(choice, row, selected, candidate, pack, raw, timed)

    assert query.request.anchor_video_range.tick_range == event.support.source_interval.coarse_range
    assert query.request.anchor_video_range.tick_range != raw.support.source_interval.coarse_range
    assert query.request.desired_video_range.tick_range == timed.candidate_window.current_range
    assert query.anchor_event_sha256 == canonical_sha256(event.to_mapping())
    assert query == _derive(choice, row, selected, candidate, pack, raw, timed)


def test_minimum_seconds_are_rounded_up_on_the_native_video_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    choice, row, selected, candidate, pack, raw, timed = _selected_case(tmp_path, monkeypatch)
    requirement = replace(row.requirement, minimum_usable_seconds=3)
    beat = replace(row.beat, evidence_requirements=(requirement,))
    changed = replace(row, beat=beat, requirement=requirement)

    query = _derive(choice, changed, selected, candidate, pack, raw, timed)

    base = timed.frame_pts_index.context.time_base
    expected = (3 * base.denominator + base.numerator - 1) // base.numerator
    assert query.request.minimum_video_duration_tick == expected
    assert minimum_video_ticks(1, TimeBase(1001, 30_000)) == 30


def test_blueprint_complete_dialogue_is_distinct_from_vlm_editing_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    choice, row, selected, candidate, pack, raw, timed = _selected_case(tmp_path, monkeypatch)
    query = _derive(choice, row, selected, candidate, pack, raw, timed)
    expected = any(
        item.requirement_kind == "dialogue_integrity" and item.mode == "complete"
        for item in row.requirement.physical_requirements
    )
    assert query.request.dialogue_requirement is (
        DialogueRequirement.COMPLETE if expected else DialogueRequirement.NOT_REQUIRED
    )

    mixed_raw = replace(raw, editing_modes=(VlmEditingMode.DIALOGUE, VlmEditingMode.ACTION))
    mixed_pack = replace(
        pack,
        candidate_hypotheses=tuple(
            mixed_raw if item == raw else item for item in pack.candidate_hypotheses
        ),
    )
    mixed_candidate = replace(candidate, editing_modes=("dialogue", "action"))
    mixed_timed = replace(
        timed,
        candidate_window=replace(
            timed.candidate_window,
            vlm_candidate_sha256=canonical_sha256(mixed_raw.to_mapping()),
        ),
    )
    mixed_timed = replace(
        mixed_timed,
        window_assessment=replace(
            mixed_timed.window_assessment,
            candidate_window_sha256=mixed_timed.candidate_window.canonical_hash,
        ),
    )
    mixed_query = _derive(
        choice, row, selected, mixed_candidate, mixed_pack, mixed_raw, mixed_timed
    )
    assert mixed_query.dominant_editing_mode is VlmEditingMode.DIALOGUE

    no_complete = replace(
        row.requirement,
        physical_requirements=(),
        physical_requirements_hash=canonical_json_hash([]),
    )
    no_complete_row = replace(
        row,
        beat=replace(row.beat, evidence_requirements=(no_complete,)),
        requirement=no_complete,
    )
    known_speech = _derive(
        choice,
        no_complete_row,
        selected,
        mixed_candidate,
        mixed_pack,
        mixed_raw,
        mixed_timed,
    )
    assert known_speech.dominant_editing_mode is VlmEditingMode.DIALOGUE
    assert known_speech.request.dialogue_requirement is DialogueRequirement.NOT_REQUIRED
    assert known_speech.dialogue_protection_kind == "known_speech"


def test_scene_and_context_intents_change_only_semantic_anchor_width(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    choice, row, selected, candidate, pack, raw, timed = _selected_case(tmp_path, monkeypatch)
    beat = replace(
        row.beat,
        span_policy=SpanPolicy("tight", ("tight", "scene", "context"), ("tight", "scene", "context")),
    )
    row = replace(row, beat=beat)

    scene = _derive(choice, row, selected, candidate, pack, raw, timed, intent="scene")
    context = _derive(choice, row, selected, candidate, pack, raw, timed, intent="context")

    assert scene.request.anchor_video_range.tick_range == TickRange(0, 50)
    assert context.request.anchor_video_range.tick_range == TickRange(14, 36)
    assert scene.request.desired_video_range == context.request.desired_video_range


def test_query_rejects_foreign_or_uncovered_semantic_owners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    choice, row, selected, candidate, pack, raw, timed = _selected_case(tmp_path, monkeypatch)
    foreign = next(item for item in row.candidates if item.candidate_ref != selected.candidate_ref)
    with pytest.raises(EditorialExactSpanError, match="outside the selected alternative"):
        _derive(choice, row, replace(selected, candidate_ref=foreign.candidate_ref), candidate, pack, raw, timed)
    with pytest.raises(EditorialExactSpanError, match="outside the selected alternative"):
        _derive(replace(choice, story_id="foreign-story"), row, selected, candidate, pack, raw, timed)

    changed_event = replace(
        next(item for item in pack.events if item.event_id == raw.anchor_event_ref),
        support=replace(
            next(item for item in pack.events if item.event_id == raw.anchor_event_ref).support,
            core_owner_window_manifest_sha256="sha256:" + "f" * 64,
        ),
    )
    changed_pack = replace(
        pack,
        events=tuple(changed_event if item.event_id == changed_event.event_id else item for item in pack.events),
    )
    with pytest.raises(EditorialExactSpanError, match="outside candidate-local"):
        _derive(choice, row, selected, candidate, changed_pack, raw, timed)


def test_context_does_not_round_past_or_invent_available_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    choice, row, selected, candidate, pack, raw, timed = _selected_case(tmp_path, monkeypatch)
    beat = replace(row.beat, span_policy=SpanPolicy("context", ("context",), ("context",)))
    row = replace(row, beat=beat)
    too_small = _policy(tick=1, time_base=TimeBase(1, 1_000_000))
    with pytest.raises(EditorialExactSpanIndeterminateError, match="smaller than one"):
        _derive(choice, row, selected, candidate, pack, raw, timed, intent="context", policy=too_small)
