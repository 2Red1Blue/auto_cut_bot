"""Closed CandidateCatalog value tests, independent of Store authority."""

from __future__ import annotations

from dataclasses import fields, replace

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_hash
from autocut_kernel.media.types import TickRange, TimeBase
from autocut_kernel.semantic_chain.candidate_catalog import (
    Candidate,
    CandidateCatalog,
    CandidateCatalogError,
    CandidateCatalogPolicy,
    CandidateEventBinding,
    CandidateMeasurement,
    CandidateSupport,
)
from autocut_kernel.semantic_chain.candidate_duration import ConservativeDuration
from autocut_kernel.semantic_chain.member_refs import SemanticMemberIdentity, SemanticObjectRef
from autocut_kernel.store import ArtifactScope
from autocut_kernel.vlm.models import (
    MappedSourceInterval,
    VlmCandidateKind,
    VlmMeasurementKind,
    VlmProxyInterval,
)


def _identity(kind: str, digit: str) -> SemanticMemberIdentity:
    return SemanticMemberIdentity(kind, kind, 1, ArtifactScope("pipeline", "job", "candidate-test"), "sha256:" + digit * 64)


def _candidate() -> Candidate:
    pack, source, graph, cards = (_identity("vlm_semantic_pack", "a"), _identity("whole_series_source_manifest", "b"), _identity("narrative_graph", "c"), _identity("event_card_set", "d"))
    event = "sha256:" + "e" * 64
    fact = "sha256:" + "f" * 64
    binding = CandidateEventBinding(SemanticObjectRef(pack, "vlm_event", event), SemanticObjectRef(graph, "event", event), SemanticObjectRef(cards, "event", event))
    support = CandidateSupport(
        VlmProxyInterval(TickRange(1, 9), 0),
        MappedSourceInterval(TickRange(10, 18), 0, TimeBase(1, 1_000), 0, TimeBase(1, 1_000)),
        ("sha256:" + "1" * 64,), "0.9", "sha256:" + "2" * 64, ConservativeDuration(1, 125),
    )
    return Candidate(
        SemanticObjectRef(pack, "vlm_candidate", "sha256:" + "3" * 64),
        SemanticObjectRef(source, "source", "source-1"), SemanticObjectRef(source, "source_window", "sha256:" + "2" * 64),
        "window-1", "highlight", "candidate_1", "reason", "anchor", "payoff", None, None,
        binding, (binding,), (), (binding,), ("dialogue", "action"), ("hook", "payoff"), ("dialogue",),
        (CandidateMeasurement("reveal_strength", "0.9", "0.9", (SemanticObjectRef(pack, "vlm_fact", fact),), (SemanticObjectRef(pack, "vlm_event", event),)),), support,
    )


def test_catalog_roundtrip_preserves_nonexclusive_vlm_capabilities_and_exact_owners():
    candidate = _candidate()
    catalog = CandidateCatalog(
        "sha256:" + "4" * 64, "sha256:" + "5" * 64, "sha256:" + "6" * 64,
        _identity("event_card_set", "d"), _identity("narrative_graph", "c"), _identity("coverage_ledger", "7"),
        CandidateCatalogPolicy("candidate-catalog-v1", "0.5", ("reveal_strength",)).canonical_hash, (candidate,),
    )
    decoded = CandidateCatalog.from_mapping(catalog.to_mapping())
    assert decoded == catalog
    assert decoded.candidates[0].editing_modes == ("dialogue", "action")
    assert decoded.candidates[0].narrative_functions == ("hook", "payoff")


def catalog_for(candidate):
    return CandidateCatalog(
        "sha256:" + "4" * 64, "sha256:" + "5" * 64, "sha256:" + "6" * 64,
        _identity("event_card_set", "d"), _identity("narrative_graph", "c"), _identity("coverage_ledger", "7"),
        CandidateCatalogPolicy("candidate-catalog-v1", "0.5", ()).canonical_hash, (candidate,),
    )


def test_signed_native_pts_round_trip():
    original = _candidate()
    support = replace(original.support, proxy_interval=VlmProxyInterval(TickRange(-9, -1), 0),
                      source_interval=replace(original.support.source_interval, coarse_range=TickRange(-10, -2)))
    catalog = catalog_for(replace(original, support=support))
    assert CandidateCatalog.from_mapping(catalog.to_mapping()) == catalog
    assert catalog.canonical_hash == canonical_json_hash(catalog.to_mapping())


@pytest.mark.parametrize("target", ["graph_revision", "card_hash", "scope"])
def test_catalog_rejects_foreign_internal_owners(target):
    original = _candidate()
    if target == "scope":
        catalog = catalog_for(original)
        with pytest.raises(CandidateCatalogError):
            replace(catalog, coverage_ledger_member_ref=replace(catalog.coverage_ledger_member_ref, scope=ArtifactScope("foreign", "job", "different")))
        return
    event = original.anchor_event
    if target == "graph_revision":
        event = replace(event, graph_event_ref=replace(event.graph_event_ref, member_ref=replace(event.graph_event_ref.member_ref, revision=99)))
    else:
        event = replace(event, event_card_ref=replace(event.event_card_ref, member_ref=replace(event.event_card_ref.member_ref, content_hash="sha256:" + "0" * 64)))
    with pytest.raises(CandidateCatalogError):
        catalog_for(replace(original, anchor_event=event))


@pytest.mark.parametrize("target", ["source_pts", "proxy_pts", "source_base", "proxy_base", "error", "uncertainty"])
def test_support_rejects_json_unsafe_native_numbers(target):
    support = _candidate().support
    with pytest.raises(ValueError):
        if target == "source_pts":
            replace(support, source_interval=replace(support.source_interval, coarse_range=TickRange(0, 2**53)))
        elif target == "proxy_pts":
            replace(support, proxy_interval=VlmProxyInterval(TickRange(0, 2**53), 0))
        elif target == "source_base":
            replace(support, source_interval=replace(support.source_interval, source_time_base=TimeBase(1, 2**53)))
        elif target == "proxy_base":
            replace(support, source_interval=replace(support.source_interval, proxy_time_base=TimeBase(1, 2**53)))
        elif target == "error":
            replace(support, source_interval=replace(support.source_interval, mapping_error_bound_source_pts=2**53))
        else:
            replace(support, proxy_interval=VlmProxyInterval(support.proxy_interval.proxy_range, 2**53))


def test_str_enum_and_mutable_capabilities_are_not_canonical_wire_values():
    original = _candidate()
    with pytest.raises(CandidateCatalogError):
        replace(original, candidate_kind=VlmCandidateKind.HIGHLIGHT)
    with pytest.raises(CandidateCatalogError):
        replace(original.measurements[0], measurement_kind=VlmMeasurementKind.REVEAL_STRENGTH)
    with pytest.raises(CandidateCatalogError):
        replace(original, editing_modes=["dialogue", "action"])


def test_closed_codec_requires_every_field_and_rejects_self_approval():
    candidate = _candidate()
    values = (candidate, candidate.support, candidate.measurements[0], candidate.anchor_event, catalog_for(candidate), CandidateCatalogPolicy("candidate-catalog-v1", "0.5", ()))
    for value in values:
        wire = value.to_mapping()
        for field in fields(value):
            if field.name in wire:
                changed = dict(wire)
                del changed[field.name]
                with pytest.raises(ValueError):
                    type(value).from_mapping(changed)
        with pytest.raises(ValueError):
            type(value).from_mapping({**wire, "pass": True})


@pytest.mark.parametrize("change", ["mode-order", "owner", "decimal", "unknown"])
def test_candidate_closed_codec_rejects_capability_and_provenance_drift(change: str):
    mapping = _candidate().to_mapping()
    if change == "mode-order":
        mapping["editing_modes"] = ["action", "dialogue"]
    elif change == "owner":
        mapping["anchor_event"]["graph_event_ref"]["member_ref"]["artifact_type"] = "event_card_set"
    elif change == "decimal":
        mapping["measurements"][0]["value"] = "1e-999999999"
    else:
        mapping["editing_modes"] = ["dialogue", "untrusted"]
    with pytest.raises(CandidateCatalogError):
        Candidate.from_mapping(mapping)
