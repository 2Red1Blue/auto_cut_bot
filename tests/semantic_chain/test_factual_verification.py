"""Independent factual checks reject semantically changed, rehashed members."""

import json
from dataclasses import replace
from decimal import Decimal

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes
from autocut_kernel.semantic_chain.factual_verification import verify_factual_members
from autocut_kernel.store.models import canonical_payload_hash

from tests.semantic_chain.test_coverage_analysis import _clean_inputs, _replace_pack
from tests.semantic_chain.test_coverage_compiler import COVERAGE, _compile
from tests.semantic_chain.test_stage1_draft import POLICY, _draft


def _case(inputs=None):
    if inputs is None:
        inputs = _clean_inputs()
    payload = _draft(inputs)
    payload["merge_proposals"] = []
    raw = canonical_json_bytes(payload)
    result, _ledger = _compile(inputs, raw=raw)
    return inputs, raw, result.members


def _verify(inputs, raw, members):
    return {item.rule_id: item for item in verify_factual_members(
        inputs, raw, members=members, draft_policy=POLICY, coverage_policy=COVERAGE,
    )}


def _mutate(members, kind, callback):
    result = []
    for member in members:
        if member.artifact_type == kind:
            payload = json.loads(member.payload_json)
            callback(payload)
            raw = canonical_json_bytes(payload).decode()
            member = replace(member, payload_json=raw, content_hash=canonical_payload_hash(raw))
        result.append(member)
    return tuple(result)


def test_clean_projection_passes_only_the_five_actually_checked_rules():
    checks = _verify(*_case())
    assert set(checks) == {"KC-GRAPH-001", "KC-GRAPH-002", "KC-AUTH-001", "KC-AUTH-002", "KC-EVENT-001"}
    assert all(item.status == "pass" and not item.violation_codes for item in checks.values())
    assert "KC-IN-001" not in checks


@pytest.mark.parametrize("kind", ["entity", "fact", "event", "beat", "obligation", "story_thread"])
def test_rehashing_changed_node_content_does_not_make_it_truthful(kind):
    inputs, raw, members = _case()

    def change(payload):
        node = next(item for item in payload["nodes"] if item["node_type"] == kind)
        node["label"] = "forged but schema-valid label"

    changed = _mutate(members, "narrative_graph", change)
    assert _verify(inputs, raw, changed)["KC-GRAPH-001"].status == "fail"


@pytest.mark.parametrize("field,value", [("summary", "invented story"), ("ordinal", 50)])
def test_digest_preserves_summary_and_episode_order(field, value):
    inputs, raw, members = _case()
    changed = _mutate(members, "episode_digest_set", lambda payload: payload["digests"][0].update({field: value}))
    assert "digest_projection_mismatch" in _verify(inputs, raw, changed)["KC-GRAPH-001"].violation_codes


def test_digest_cannot_claim_an_unrelated_window_as_its_source():
    inputs, raw, members = _case()

    def change(payload):
        payload["digests"][0]["source_window_refs"][0]["object_id"] = "sha256:" + "b" * 64

    changed = _mutate(members, "episode_digest_set", change)
    checks = _verify(inputs, raw, changed)
    assert checks["KC-GRAPH-001"].status == checks["KC-AUTH-002"].status == "fail"


@pytest.mark.parametrize("field,value", [("content", "not the observed event"), ("episode_id", "episode-999")])
def test_card_semantics_cannot_be_substituted_even_after_rehash(field, value):
    inputs, raw, members = _case()
    changed = _mutate(members, "event_card_set", lambda payload: payload["events"][0].update({field: value}))
    assert _verify(inputs, raw, changed)["KC-EVENT-001"].status == "fail"


def test_card_coarse_mapping_is_compared_to_raw_vlm_not_only_schema_shape():
    inputs, raw, members = _case()

    def change(payload):
        payload["events"][0]["source_range_refs"][0]["mapped_interval"]["provider_uncertainty"]["tick"] += 1

    changed = _mutate(members, "event_card_set", change)
    assert _verify(inputs, raw, changed)["KC-EVENT-001"].status == "fail"


def test_same_object_id_under_a_different_pack_is_not_source_evidence():
    inputs, raw, members = _case()

    def change(payload):
        node = next(item for item in payload["nodes"] if item["node_type"] == "fact")
        node["evidence_refs"][0]["member_ref"]["content_hash"] = "sha256:" + "b" * 64

    changed = _mutate(members, "narrative_graph", change)
    checks = _verify(inputs, raw, changed)
    assert checks["KC-GRAPH-001"].status == checks["KC-AUTH-002"].status == "fail"


@pytest.mark.parametrize("mutation", ["missing", "extra", "changed_evidence"])
def test_graph_edges_must_match_raw_and_draft_declarations(mutation):
    inputs, raw, members = _case()

    def change(payload):
        if mutation == "missing":
            payload["edges"].pop()
        elif mutation == "extra":
            edge = dict(payload["edges"][0])
            edge["edge_id"] = "invented-edge"
            payload["edges"].append(edge)
        else:
            payload["edges"][0]["evidence_refs"] = []

    changed = _mutate(members, "narrative_graph", change)
    assert _verify(inputs, raw, changed)["KC-GRAPH-001"].status == "fail"


@pytest.mark.parametrize("channel", ["entity", "fact", "event", "summary"])
def test_minimum_evidence_is_read_from_raw_scores_not_producer_confidence(channel):
    inputs = _clean_inputs()

    def lower(pack):
        if channel == "summary":
            return replace(pack, window_summary=replace(pack.window_summary, confidence=Decimal("0.1")))
        key = "entities" if channel == "entity" else channel + "s"
        item = getattr(pack, key)[0]
        return replace(pack, **{key: (replace(item, support=replace(item.support, confidence=Decimal("0.1"))),)})

    inputs, raw, members = _case(_replace_pack(inputs, 0, lower))
    checks = _verify(inputs, raw, members)
    assert checks["KC-GRAPH-001"].status == "pass"  # honest low-score projection
    assert checks["KC-GRAPH-002"].status == "fail"


def test_unsupported_summary_remains_missing_evidence_despite_all_assigned_facts():
    inputs = _replace_pack(_clean_inputs(), 0, lambda pack: replace(
        pack, window_summary=replace(pack.window_summary, fact_refs=(), event_refs=()),
    ))
    checks = _verify(*_case(inputs))
    assert checks["KC-GRAPH-001"].status == "pass"
    assert "summary_missing_evidence" in checks["KC-GRAPH-002"].violation_codes


@pytest.mark.parametrize("grant_change", ["purpose", "source_hash"])
def test_source_grant_is_rechecked_independently(grant_change):
    inputs, raw, members = _case()
    if grant_change == "purpose":
        grant = replace(inputs.source_grant, policy=replace(inputs.source_grant.policy, authorized_purposes=("render_source",)))
    else:
        grant = replace(inputs.source_grant, sources=(replace(inputs.source_grant.sources[0], content_sha256="sha256:" + "b" * 64),))
    changed = replace(inputs, source_grant=grant)
    # The raw draft binding includes the grant; changing it must not be bypassed.
    with pytest.raises(ValueError):
        _verify(changed, raw, members)


def test_changed_draft_is_not_validated_against_old_graph():
    inputs, raw, members = _case()
    payload = json.loads(raw)
    payload["beats"][0]["summary"] = "different narrative claim"
    checks = _verify(inputs, canonical_json_bytes(payload), members)
    assert checks["KC-GRAPH-001"].status == "fail"


def test_rebound_hashes_do_not_hide_a_graph_reference_to_a_missing_event_card():
    inputs, raw, members = _case()
    old_card = next(member for member in members if member.artifact_type == "event_card_set")
    old_digest = next(member for member in members if member.artifact_type == "episode_digest_set")
    members = _mutate(members, "event_card_set", lambda payload: payload["events"].pop())
    new_card = next(member for member in members if member.artifact_type == "event_card_set")

    def rebind(member, old_hash, new_hash):
        content = member.payload_json.replace(old_hash, new_hash)
        return replace(member, payload_json=content, content_hash=canonical_payload_hash(content))

    members = tuple(rebind(member, old_card.content_hash, new_card.content_hash)
                    if member.artifact_type in ("episode_digest_set", "narrative_graph") else member
                    for member in members)
    new_digest = next(member for member in members if member.artifact_type == "episode_digest_set")
    members = tuple(rebind(member, old_digest.content_hash, new_digest.content_hash)
                    if member.artifact_type == "narrative_graph" else member for member in members)
    checks = _verify(inputs, raw, members)
    assert "unresolved_output_reference" in checks["KC-GRAPH-001"].violation_codes
    assert checks["KC-EVENT-001"].status == "fail"
