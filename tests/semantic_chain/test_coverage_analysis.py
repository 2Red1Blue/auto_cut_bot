"""Pure coverage checks with synthetic persisted DTOs, not DB acceptance."""

from dataclasses import FrozenInstanceError, replace
from decimal import Decimal

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes
from autocut_kernel.semantic_chain.coverage_analysis import (
    Stage1CoveragePolicy,
    analyze_observation_coverage,
)
from autocut_kernel.semantic_chain.stage1_draft import stage1_draft_prompt_inputs
from autocut_kernel.store.models import canonical_payload_hash
from autocut_kernel.vlm.models import derive_vlm_global_id

from tests.semantic_chain.test_stage1_draft import POLICY, _draft, _synthetic_inputs


def _replace_pack(inputs, index, transform):
    item = inputs.inputs[index]
    pack = transform(item.semantic_pack.semantic_pack)
    payload = canonical_json_bytes(pack.to_mapping()).decode()
    persisted = replace(
        item.semantic_pack,
        reference=replace(item.semantic_pack.reference, content_hash=canonical_payload_hash(payload)),
        payload_json=payload,
        semantic_pack=pack,
    )
    members = list(inputs.inputs)
    members[index] = replace(item, semantic_pack=persisted)
    return replace(inputs, inputs=tuple(members))


def _clean_inputs():
    inputs = _synthetic_inputs()
    for index in range(len(inputs.inputs)):
        inputs = _replace_pack(inputs, index, lambda pack: replace(
            pack,
            entities=tuple(replace(entity, support=replace(entity.support, confidence=Decimal("0.9"))) for entity in pack.entities),
            facts=tuple(replace(fact, support=replace(fact.support, confidence=Decimal("0.9"))) for fact in pack.facts),
            events=tuple(replace(event, support=replace(event.support, confidence=Decimal("0.9"))) for event in pack.events),
            window_summary=replace(pack.window_summary, confidence=Decimal("0.9")),
            continuity=replace(pack.continuity, ends_mid_event=False,
                               continues_into_next=False, exit_state_fact_refs=()),
        ))
    return inputs


def _analyze(inputs, payload=None, minimum="0.5"):
    if payload is None:
        payload = _draft(inputs)
        payload["merge_proposals"] = []
    return analyze_observation_coverage(
        inputs, canonical_json_bytes(payload), draft_policy=POLICY,
        coverage_policy=Stage1CoveragePolicy(minimum, "strict_global"),
    )


def test_complete_conservation_and_explicit_summary_support():
    inputs = _clean_inputs()
    result = _analyze(inputs)
    assert len(result.rows) == 7
    assert {row.resolution_status for row in result.rows} == {"resolved"}
    assert {row.disposition for row in result.rows} == {"narrative", "supporting"}
    for index, item in enumerate(inputs.inputs):
        pack = item.semantic_pack.semantic_pack
        assert sum(row.unit_type == "fact" and row.unit_id == pack.facts[0].fact_id for row in result.rows) == 1
        assert sum(row.unit_type == "event" and row.unit_id == pack.events[0].event_id for row in result.rows) == 1
        event = next(row for row in result.rows if row.unit_id == pack.events[0].event_id)
        assert event.disposition == ("narrative" if index == 0 else "supporting")
    assert not hasattr(result, "admission")
    assert not hasattr(result, "decision")


def test_empty_draft_can_keep_explicit_background_not_fake_narrative():
    inputs = _clean_inputs()
    payload = _draft(inputs)
    for name in ("beats", "obligations", "story_threads", "merge_proposals"):
        payload[name] = []
    result = _analyze(inputs, payload)
    assert len(result.rows) == 6
    assert all(row.disposition == "supporting" for row in result.rows)
    assert all(row.resolution_status == "resolved" for row in result.rows)


def test_unassigned_is_not_water_content_or_graph_presence_support():
    inputs = _clean_inputs()
    inputs = _replace_pack(inputs, 1, lambda pack: replace(
        pack, window_summary=replace(pack.window_summary, fact_refs=(), event_refs=()),
    ))
    result = _analyze(inputs)
    second = [row for row in result.rows if row.window_manifest_sha256 == inputs.inputs[1].source_window.window_manifest_sha256]
    assert len(second) == 3
    assert all(row.resolution_status == "unresolved" and row.disposition == "unassigned" for row in second)
    assert all("unassigned" in row.reason_codes for row in second)


def test_all_units_assigned_does_not_ground_an_unreferenced_window_summary():
    inputs = _clean_inputs()
    inputs = _replace_pack(inputs, 0, lambda pack: replace(
        pack, window_summary=replace(pack.window_summary, fact_refs=(), event_refs=()),
    ))
    result = _analyze(inputs)
    first_window = inputs.inputs[0].source_window.window_manifest_sha256
    unit_rows = [row for row in result.rows if row.window_manifest_sha256 == first_window and row.unit_type != "source_window"]
    assert all(row.resolution_status == "resolved" and row.disposition == "narrative" for row in unit_rows)
    window = next(row for row in result.rows if row.unit_type == "source_window" and row.unit_id == first_window)
    assert window.resolution_status == "unresolved"
    assert "summary_evidence_missing" in window.reason_codes


def test_eventless_window_retains_fact_without_fabricating_event():
    inputs = _clean_inputs()
    payload = _draft(inputs)
    payload["merge_proposals"] = []
    inputs = _replace_pack(inputs, 1, lambda pack: replace(
        pack, events=(), candidate_hypotheses=(),
        window_summary=replace(pack.window_summary, event_refs=()),
    ))
    payload["input_binding_sha256"] = stage1_draft_prompt_inputs(inputs, policy=POLICY)["input_binding_sha256"]
    result = _analyze(inputs, payload)
    assert sum(row.unit_type == "event" for row in result.rows) == 1
    assert sum(row.unit_type == "fact" for row in result.rows) == 2
    assert all(row.resolution_status == "resolved" for row in result.rows)


def test_merge_preserves_every_observation_and_unknown_cause():
    inputs = _clean_inputs()
    result = _analyze(inputs, _draft(inputs))
    assert len(result.rows) == 7
    assert all(row.resolution_status == "unresolved" for row in result.rows)
    assert all("identity_unresolved" in row.reason_codes and row.cause_ids for row in result.rows)
    assert len({cause for row in result.rows for cause in row.cause_ids}) == 1


def test_low_confidence_is_read_from_raw_evidence_and_propagates():
    inputs = _clean_inputs()
    inputs = _replace_pack(inputs, 0, lambda pack: replace(
        pack, entities=(replace(pack.entities[0], support=replace(pack.entities[0].support, confidence=Decimal("0.1"))),),
    ))
    result = _analyze(inputs)
    affected = [row for row in result.rows if row.unit_type == "obligation" or row.window_manifest_sha256 == inputs.inputs[0].source_window.window_manifest_sha256]
    assert len(affected) == 4
    assert all("low_confidence" in row.reason_codes for row in affected)
    assert all(row.resolution_status == "unresolved" for row in affected)


@pytest.mark.parametrize("kind", ["fact", "event", "summary"])
def test_each_raw_confidence_channel_is_checked(kind):
    inputs = _clean_inputs()

    def lower(pack):
        if kind == "summary":
            return replace(pack, window_summary=replace(pack.window_summary, confidence=Decimal("0.1")))
        values = getattr(pack, kind + "s")
        return replace(pack, **{kind + "s": tuple(replace(item, support=replace(item.support, confidence=Decimal("0.1"))) for item in values)})

    inputs = _replace_pack(inputs, 1, lower)
    result = _analyze(inputs)
    window = next(row for row in result.rows if row.unit_type == "source_window" and row.unit_id == inputs.inputs[1].source_window.window_manifest_sha256)
    assert window.resolution_status == "unresolved"
    assert "low_confidence" in window.reason_codes


def test_standalone_unmentioned_fact_is_never_dropped():
    inputs = _clean_inputs()

    def standalone(pack):
        fact = replace(
            pack.facts[0], local_fact_id="fact_extra",
            fact_id=derive_vlm_global_id("fact", "fact_extra", pack.request_identity_sha256),
        )
        return replace(pack, facts=tuple(sorted((*pack.facts, fact), key=lambda value: value.local_fact_id)))

    inputs = _replace_pack(inputs, 1, standalone)
    result = _analyze(inputs)
    assert len(result.rows) == 8
    extra_id = next(fact.fact_id for fact in inputs.inputs[1].semantic_pack.semantic_pack.facts if fact.local_fact_id == "fact_extra")
    extra = next(row for row in result.rows if row.unit_id == extra_id)
    assert extra.resolution_status == "unresolved"
    assert extra.disposition == "unassigned"


def test_unassigned_requirement_is_not_claimed_fulfilled():
    inputs = _clean_inputs()
    payload = _draft(inputs)
    payload["beats"] = []
    payload["story_threads"] = []
    payload["merge_proposals"] = []
    result = _analyze(inputs, payload)
    obligation = next(row for row in result.rows if row.unit_type == "obligation")
    assert obligation.resolution_status == "unresolved"
    assert obligation.disposition == "unassigned"
    assert not hasattr(obligation, "success_criteria_satisfied")


def test_direct_resolution_does_not_assert_transitive_causal_isolation():
    inputs = _clean_inputs()

    def causal_pair(pack):
        fact_b = replace(pack.facts[0], local_fact_id="fact_b", fact_id=derive_vlm_global_id("fact", "fact_b", pack.request_identity_sha256))
        event_a = replace(pack.events[0], support=replace(pack.events[0].support, confidence=Decimal("0.1")))
        event_b = replace(pack.events[0], local_event_id="event_b", event_id=derive_vlm_global_id("event", "event_b", pack.request_identity_sha256), fact_refs=(fact_b.fact_id,), cause_event_refs=(event_a.event_id,))
        event_a = replace(event_a, effect_event_refs=(event_b.event_id,))
        return replace(pack, facts=(*pack.facts, fact_b), events=(event_a, event_b),
                       window_summary=replace(pack.window_summary, event_refs=tuple(sorted((event_a.event_id, event_b.event_id)))))

    inputs = _replace_pack(inputs, 1, causal_pair)
    result = _analyze(inputs)
    events = inputs.inputs[1].semantic_pack.semantic_pack.events
    a = next(row for row in result.rows if row.unit_id == events[0].event_id)
    b = next(row for row in result.rows if row.unit_id == events[1].event_id)
    window = next(row for row in result.rows if row.unit_type == "source_window" and row.unit_id == inputs.inputs[1].source_window.window_manifest_sha256)
    assert a.resolution_status == "unresolved"
    assert b.resolution_status == "resolved"  # direct evidence, NOT an isolation proof
    assert window.resolution_status == "unresolved"
    assert not hasattr(b, "isolation_status")


def test_unmatched_tail_is_not_silently_closed():
    inputs = _clean_inputs()
    inputs = _replace_pack(inputs, 1, lambda pack: replace(
        pack, continuity=replace(pack.continuity, ends_mid_event=True,
                                 continues_into_next=True, exit_state_fact_refs=(pack.facts[0].fact_id,)),
    ))
    result = _analyze(inputs)
    affected = [row for row in result.rows if row.window_manifest_sha256 == inputs.inputs[1].source_window.window_manifest_sha256]
    assert all("continuity_missing_context" in row.reason_codes for row in affected)
    assert all(row.cause_ids for row in affected)


def test_authorization_checks_source_hash_not_just_purpose():
    inputs = _clean_inputs()
    grant = replace(inputs.source_grant, sources=(replace(inputs.source_grant.sources[0], content_sha256="sha256:" + "b" * 64),))
    inputs = replace(inputs, source_grant=grant)
    with pytest.raises(ValueError, match="exact semantic_analysis grant"):
        _analyze(inputs)


def test_draft_is_redecoded_not_accepted_as_an_authority_token():
    inputs = _clean_inputs()
    payload = _draft(inputs)
    payload["input_binding_sha256"] = "sha256:" + "b" * 64
    with pytest.raises(ValueError):
        _analyze(inputs, payload)


def test_determinism_policy_binding_and_immutable_output():
    inputs = _clean_inputs()
    result = _analyze(inputs)
    assert result == _analyze(inputs)
    assert result.canonical_hash != _analyze(inputs, minimum="0.8").canonical_hash
    with pytest.raises(FrozenInstanceError):
        result.rows[0].disposition = "supporting"
    mapping = result.to_mapping()
    mapping["rows"].clear()
    assert result.rows


@pytest.mark.parametrize("value", [0.5, True, "NaN", "0.50", "-0.1", "1.1"])
def test_no_confidence_coercion_or_defaults(value):
    with pytest.raises(ValueError):
        Stage1CoveragePolicy(value, "strict_global")


def test_unimplemented_scoped_mode_cannot_be_requested():
    with pytest.raises(ValueError):
        Stage1CoveragePolicy("0.5", "dependency_scoped")
