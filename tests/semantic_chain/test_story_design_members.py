"""Cross-member identity tests, not evidence or Store acceptance fixtures."""

import json
from dataclasses import replace

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes
from autocut_kernel.semantic_chain.candidate_duration import ConservativeDuration
from autocut_kernel.semantic_chain.story_design_compiler import compose_story_design_members
from autocut_kernel.semantic_chain.story_design_members import decode_story_design_business_members
from autocut_kernel.store.models import canonical_payload_hash

from tests.semantic_chain.test_story_design_compiler import composition_case


def test_four_member_decoder_roundtrip_keeps_all_proposals_and_targets():
    projection, support, job = composition_case()
    compiled = compose_story_design_members(projection, support, job_policy=job)
    values = decode_story_design_business_members(compiled.business_members, scope=projection.member.scope)
    assert values.candidate_catalog == projection.catalog
    assert values.proposal_set == support
    assert values.source_usage_ledger.target_story_ids == values.portfolio.target_story_ids
    assert len(values.proposal_set.proposals) == 3


@pytest.mark.parametrize("target", ["missing", "duplicate", "order", "revision", "scope", "logical_id", "bytes"])
def test_incomplete_or_foreign_members_rejected(target):
    projection, support, job = composition_case()
    members = list(compose_story_design_members(projection, support, job_policy=job).business_members)
    if target == "missing":
        members.pop()
    elif target == "duplicate":
        members.append(members[0])
    elif target == "order":
        members.reverse()
    elif target == "revision":
        members[1] = replace(members[1], revision=2)
    elif target == "scope":
        members[1] = replace(members[1], scope=replace(members[1].scope, key="other"))
    elif target == "logical_id":
        members[1] = replace(members[1], logical_id="other")
    else:
        members[1] = replace(members[1], payload_json='{"forged":true}')
    with pytest.raises(ValueError):
        decode_story_design_business_members(tuple(members), scope=projection.member.scope)


@pytest.mark.parametrize("index", range(4))
def test_rehashed_unknown_fields_do_not_become_valid_members(index):
    projection, support, job = composition_case()
    members = list(compose_story_design_members(projection, support, job_policy=job).business_members)
    wire = json.loads(members[index].payload_json)
    wire["pass"] = True
    raw = canonical_json_bytes(wire).decode()
    members[index] = replace(members[index], payload_json=raw, content_hash=canonical_payload_hash(raw))
    with pytest.raises(ValueError):
        decode_story_design_business_members(tuple(members), scope=projection.member.scope)


@pytest.mark.parametrize("target", ["source", "duration", "raw_owner", "card_owner", "unknown_candidate", "ghost_event", "context_only"])
def test_rehashed_coherent_dag_cannot_supply_foreign_material_witness(target):
    projection, support, job = composition_case(context_only=target == "context_only")
    first = support.proposals[0]
    row = first.requirements[0]
    alternative = row.alternatives[0]
    if target == "source":
        alternative = replace(alternative, source_ref=replace(alternative.source_ref, object_id="foreign-source"))
    elif target == "duration":
        alternative = replace(alternative, conservative_duration=ConservativeDuration(21, 1))
    elif target == "unknown_candidate":
        alternative = replace(alternative, candidate_ref=replace(alternative.candidate_ref, object_id="unknown"))
    else:
        fact = alternative.fact_witnesses[0]
        if target == "raw_owner":
            fact = replace(fact, vlm_fact_ref=replace(fact.vlm_fact_ref, member_ref=replace(fact.vlm_fact_ref.member_ref, revision=99)))
        elif target in {"ghost_event", "context_only"}:
            fact = replace(fact, via_event_refs=(replace(fact.via_event_refs[0], object_id="sha256:" + "9" * 64),))
        else:
            event = fact.via_event_refs[0]
            fact = replace(fact, via_event_refs=(replace(event, member_ref=replace(event.member_ref, revision=99)),))
        alternative = replace(alternative, fact_witnesses=(fact,))
    # Keep all internal owners consistent, so the test reaches the Catalog join
    # rather than failing only the MaterialSupportEvaluation DTO consistency.
    changed = replace(row, alternatives=(alternative,))
    support = replace(support, proposals=tuple(replace(item, requirements=(changed,)) for item in support.proposals))
    compiled = compose_story_design_members(projection, support, job_policy=job)
    assert compiled.business_members
    with pytest.raises(ValueError, match="material"):
        decode_story_design_business_members(compiled.business_members, scope=projection.member.scope)
