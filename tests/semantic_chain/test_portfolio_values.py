"""Acyclic content identities, not accepted or persisted Portfolio fixtures."""

from dataclasses import replace

import pytest
from autocut_kernel.semantic_chain.member_refs import SemanticMemberIdentity, SemanticObjectRef
from autocut_kernel.semantic_chain.portfolio_search import search_portfolio
from autocut_kernel.semantic_chain.portfolio_values import (
    InitialSourceUsageLedger,
    PortfolioValueError,
    StoryPortfolio,
    StorySelection,
)

from tests.semantic_chain.test_portfolio_search import proposal, ref


def portfolio():
    owner = replace(ref("candidate", "unused").member_ref, artifact_type="proposal_set", logical_id="proposal_set")
    rows = (proposal(0, "X"), proposal(1, "X"), proposal(2, "Y"))
    search = search_portfolio(rows, selected_story_count=2, source_reuse="forbid", max_search_states=100)
    selected = tuple(StorySelection(index, SemanticObjectRef(owner, "proposal", rows[index].proposal_id)) for index in search.proposal_indexes)
    return StoryPortfolio(owner, "sha256:" + "b" * 64, selected, search.assignment, search.visited_states)


def ledger():
    selected = portfolio()
    owner = SemanticMemberIdentity("portfolio", "portfolio", 1, selected.proposal_set_ref.scope, selected.canonical_hash)
    return InitialSourceUsageLedger(owner, selected.target_story_ids)


def test_acyclic_ids_roundtrip_and_order():
    selected = portfolio()
    assert StoryPortfolio.from_mapping(selected.to_mapping()) == selected
    assert tuple(item.proposal_index for item in selected.selections) == (0, 2)
    assert selected.target_story_ids == tuple(item.story_id for item in selected.selections)
    usage = ledger()
    assert InitialSourceUsageLedger.from_mapping(usage.to_mapping()) == usage
    assert usage.portfolio_ref.content_hash == selected.canonical_hash
    assert all(row["status"] == "pending" and row["reservations"] == [] for row in usage.to_mapping()["rows"])
    assert "admission_ref" not in selected.to_mapping()


@pytest.mark.parametrize("field", ["content_hash", "revision", "logical_id", "scope"])
def test_story_id_binds_complete_proposal_set_identity(field):
    selected = portfolio().selections[0]
    owner = selected.proposal_ref.member_ref
    value = {"content_hash": "sha256:" + "c" * 64, "revision": 2, "logical_id": "other", "scope": replace(owner.scope, key="other")}[field]
    changed = StorySelection(selected.proposal_index, replace(selected.proposal_ref, member_ref=replace(owner, **{field: value})))
    assert changed.story_id != selected.story_id


@pytest.mark.parametrize("target", ["story_id", "targets", "target_hash", "selected_order", "assignment_missing", "unknown", "mixed_catalog"])
def test_portfolio_closed_wire_rejects_tamper(target):
    wire = portfolio().to_mapping()
    if target == "story_id":
        wire["selection_records"][0]["story_id"] = "sha256:" + "f" * 64
    elif target == "targets":
        wire["target_story_ids"].reverse()
    elif target == "target_hash":
        wire["target_story_ids_hash"] = "sha256:" + "f" * 64
    elif target == "selected_order":
        wire["selection_records"].reverse()
    elif target == "assignment_missing":
        wire["requirement_assignments"].pop()
    elif target == "mixed_catalog":
        wire["requirement_assignments"][0]["alternative"]["candidate_ref"]["member_ref"]["revision"] = 9
    else:
        wire["pass"] = True
    with pytest.raises(ValueError):
        StoryPortfolio.from_mapping(wire)


@pytest.mark.parametrize("target", ["finalized", "index_bool", "advanced", "reserved", "row_status", "row_order", "target_hash"])
def test_initial_ledger_cannot_create_fake_progress_or_reservation(target):
    wire = ledger().to_mapping()
    if target == "finalized":
        wire["finalized"] = True
    elif target == "index_bool":
        wire["next_priority_index"] = False
    elif target == "advanced":
        wire["next_priority_index"] = 1
    elif target == "reserved":
        wire["rows"][0]["reservations"] = [{"source": "X"}]
    elif target == "row_status":
        wire["rows"][0]["status"] = "ready"
    elif target == "row_order":
        wire["rows"].reverse()
    else:
        wire["target_story_ids_hash"] = "sha256:" + "f" * 64
    with pytest.raises(PortfolioValueError):
        InitialSourceUsageLedger.from_mapping(wire)


def test_every_wire_field_required_and_empty_portfolio_rejected():
    for value in (portfolio(), ledger(), portfolio().selections[0]):
        wire = value.to_mapping()
        for key in wire:
            changed = dict(wire)
            del changed[key]
            with pytest.raises(ValueError):
                type(value).from_mapping(changed)
    with pytest.raises(PortfolioValueError):
        replace(portfolio(), selections=())
    with pytest.raises(PortfolioValueError):
        replace(ledger(), target_story_ids=())


def test_same_candidate_cannot_claim_different_sources_in_values_or_wire():
    selected = portfolio()
    first, second = selected.requirement_assignments
    contradictory = replace(second, alternative=replace(
        second.alternative, candidate_ref=first.alternative.candidate_ref,
    ))
    with pytest.raises(PortfolioValueError, match="different sources"):
        replace(selected, requirement_assignments=(first, contradictory))
    wire = selected.to_mapping()
    wire["requirement_assignments"][1] = contradictory.to_mapping()
    with pytest.raises(PortfolioValueError, match="different sources"):
        StoryPortfolio.from_mapping(wire)


def test_same_candidate_and_source_may_repeat_without_claiming_policy_admission():
    selected = portfolio()
    first, second = selected.requirement_assignments
    repeated = replace(second, alternative=first.alternative)
    value = replace(selected, requirement_assignments=(first, repeated))
    assert StoryPortfolio.from_mapping(value.to_mapping()) == value
