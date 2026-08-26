"""Synthetic diagnostic content; never a Store or KC acceptance fixture."""

import hashlib
import json
from copy import deepcopy
from dataclasses import FrozenInstanceError, fields, replace
from decimal import localcontext

import pytest
from autocut_kernel.semantic_chain.diagnostic_models import (
    ConfidenceMeasurement,
    ConflictClaim,
    ConflictDiagnostic,
    ConflictDiagnostics,
    ContinuityClaimValue,
    DiagnosticModelError,
    EvidenceDiagnostic,
    EvidenceDiagnostics,
    IdentityObservationValue,
    MergeProposalCause,
)
from autocut_kernel.semantic_chain.member_refs import SemanticMemberIdentity, SemanticObjectRef
from autocut_kernel.store import ArtifactScope


def _hash(text):
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


SCOPE = ArtifactScope("pipeline", "job", "diagnostic-value-test")
SOURCE = SemanticMemberIdentity("whole_series_source_manifest", "source", 1, SCOPE, _hash("source"))
PACK = SemanticMemberIdentity("vlm_semantic_pack", "pack", 1, SCOPE, _hash("pack"))
SECOND_PACK = replace(PACK, logical_id="second", content_hash=_hash("second-pack"))
WINDOW = SemanticObjectRef(SOURCE, "source_window", _hash("window"))
SECOND_WINDOW = replace(WINDOW, object_id=_hash("second-window"))
FACT = SemanticObjectRef(PACK, "vlm_fact", _hash("fact"))
ENTITY = SemanticObjectRef(PACK, "vlm_entity", _hash("entity"))
SECOND_ENTITY = SemanticObjectRef(SECOND_PACK, "vlm_entity", _hash("second-entity"))
EVENT = SemanticObjectRef(PACK, "vlm_event", _hash("event"))
GRAPH = SemanticMemberIdentity("narrative_graph", "graph", 1, SCOPE, _hash("graph"))
AFFECTED = SemanticObjectRef(GRAPH, "fact", FACT.object_id)
CLAIM = ContinuityClaimValue(WINDOW, PACK, "next", True, (FACT,))
OTHER_CLAIM = ContinuityClaimValue(SECOND_WINDOW, SECOND_PACK, "previous", False, ())
MEASUREMENT = ConfidenceMeasurement(ENTITY, "entity", "0.3", "0.5", _hash("policy"))
MERGE = MergeProposalCause(
    _hash("raw-proposal"), "merge_1", "可能是同一个人", (ENTITY, SECOND_ENTITY), (FACT, EVENT)
)
MERGE_CLAIMS = (
    ConflictClaim("entity_1", IdentityObservationValue(ENTITY)),
    ConflictClaim("entity_2", IdentityObservationValue(SECOND_ENTITY)),
)
TIMELINE_CLAIMS = (
    ConflictClaim("left", CLAIM),
    ConflictClaim("right", OTHER_CLAIM),
)
LOW = EvidenceDiagnostic(
    "low", "low_confidence", WINDOW, (ENTITY,), (AFFECTED,), MEASUREMENT, None, None
)
MISSING = EvidenceDiagnostic(
    "missing", "continuity_missing_context", WINDOW, (WINDOW, FACT), (AFFECTED,), None, CLAIM, None
)
SUMMARY = EvidenceDiagnostic(
    "summary", "summary_evidence_missing", WINDOW, (WINDOW,), (AFFECTED,), None, None, "原始摘要"
)
UNASSIGNED = EvidenceDiagnostic(
    "unassigned", "unassigned", AFFECTED, (FACT,), (AFFECTED,), None, None, None
)
MERGE_DIAGNOSTIC = ConflictDiagnostic(
    "duplicate",
    "possible_duplicate",
    WINDOW,
    (ENTITY, SECOND_ENTITY, FACT, EVENT),
    (AFFECTED,),
    MERGE.cause_id,
    ("entity_1", "entity_2"),
)
TIMELINE_DIAGNOSTIC = ConflictDiagnostic(
    "timeline",
    "timeline_order_conflict",
    WINDOW,
    (WINDOW, SECOND_WINDOW, FACT),
    (AFFECTED,),
    _hash("raw-continuity-issue"),
    ("left", "right"),
)
EVIDENCE_SET = EvidenceDiagnostics(
    "evidence",
    _hash("input"),
    _hash("raw-draft"),
    _hash("canonical-draft"),
    (LOW, MISSING, SUMMARY, UNASSIGNED),
)
CONFLICT_SET = ConflictDiagnostics(
    "conflicts",
    _hash("input"),
    _hash("raw-draft"),
    _hash("canonical-draft"),
    (MERGE_DIAGNOSTIC, TIMELINE_DIAGNOSTIC),
    (*MERGE_CLAIMS, *TIMELINE_CLAIMS),
    (MERGE,),
)
VALUES = (
    CLAIM,
    OTHER_CLAIM,
    IdentityObservationValue(ENTITY),
    *MERGE_CLAIMS,
    *TIMELINE_CLAIMS,
    MEASUREMENT,
    MERGE,
    LOW,
    MISSING,
    SUMMARY,
    UNASSIGNED,
    MERGE_DIAGNOSTIC,
    TIMELINE_DIAGNOSTIC,
    EVIDENCE_SET,
    CONFLICT_SET,
)


@pytest.mark.parametrize("value", VALUES, ids=lambda value: type(value).__name__)
def test_closed_roundtrip_hash_matches_independent_json_oracle(value):
    mapping = value.to_mapping()
    assert type(value).from_mapping(mapping) == value
    expected = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(mapping, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    assert value.canonical_hash == expected
    assert not hasattr(value, "admission") and not hasattr(value, "accepted")


def test_claim_wire_discriminator_is_derived_not_constructor_authority():
    assert MERGE_CLAIMS[0].claim_type == "identity_observation"
    assert TIMELINE_CLAIMS[0].claim_type == "continuity"
    assert {field.name for field in fields(ConflictClaim)} == {"claim_id", "payload"}
    wire = TIMELINE_CLAIMS[0].to_mapping()
    wire["claim_type"] = "identity_observation"
    with pytest.raises(DiagnosticModelError):
        ConflictClaim.from_mapping(wire)


@pytest.mark.parametrize("value", [0, 1, "true", None, [], {}])
def test_continuity_boolean_does_not_coerce(value):
    with pytest.raises(DiagnosticModelError):
        replace(CLAIM, continues=value)
    wire = CLAIM.to_mapping()
    wire["continues"] = value
    with pytest.raises(DiagnosticModelError):
        ContinuityClaimValue.from_mapping(wire)


@pytest.mark.parametrize("change", ["hash", "scope", "revision", "logical", "kind"])
def test_continuity_state_facts_require_the_exact_pack(change):
    owner = {
        "hash": replace(PACK, content_hash=_hash("different")),
        "scope": replace(PACK, scope=replace(SCOPE, key="different")),
        "revision": replace(PACK, revision=2),
        "logical": replace(PACK, logical_id="different"),
        "kind": replace(PACK, artifact_type="event_card_set"),
    }[change]
    with pytest.raises(DiagnosticModelError):
        replace(CLAIM, state_fact_refs=(replace(FACT, member_ref=owner),))


def test_continuity_keeps_false_empty_state_and_true_nonempty_state_distinct():
    assert OTHER_CLAIM.to_mapping()["state_fact_refs"] == []
    for changes in (
        {"continues": False},
        {"state_fact_refs": ()},
        {"direction": "entry"},
        {"state_fact_refs": (ENTITY,)},
        {"pack_ref": SOURCE},
    ):
        with pytest.raises(DiagnosticModelError):
            replace(CLAIM, **changes)


def test_observation_reference_preserves_full_identity_without_same_person_claim():
    mapping = IdentityObservationValue(ENTITY).to_mapping()
    assert mapping == {"observation_ref": ENTITY.to_mapping()}
    assert IdentityObservationValue(
        replace(ENTITY, member_ref=SECOND_PACK)
    ) != IdentityObservationValue(ENTITY)
    with pytest.raises(DiagnosticModelError):
        IdentityObservationValue(FACT)
    mapping["same_person"] = True
    with pytest.raises(DiagnosticModelError):
        IdentityObservationValue.from_mapping(mapping)


@pytest.mark.parametrize(
    "kind,ref", [("entity", ENTITY), ("fact", FACT), ("event", EVENT), ("window_summary", WINDOW)]
)
def test_measurement_has_exact_observation_kind_owner_and_stable_cause_hash(kind, ref):
    value = replace(MEASUREMENT, observation_kind=kind, observation_ref=ref)
    assert ConfidenceMeasurement.from_mapping(value.to_mapping()) == value
    assert (
        value.canonical_hash != replace(value, policy_sha256=_hash("other-policy")).canonical_hash
    )
    with pytest.raises(DiagnosticModelError):
        replace(value, observation_ref=AFFECTED)


@pytest.mark.parametrize(
    "value", [True, 0.3, "0.30", "1e-1", "NaN", "-0", "-0.1", "1.1", ".1", "01", "", "\ud800"]
)
@pytest.mark.parametrize("field", ["value", "threshold"])
def test_measurement_rejects_noncanonical_or_out_of_range_decimal(field, value):
    with pytest.raises(DiagnosticModelError):
        replace(MEASUREMENT, **{field: value})


@pytest.mark.parametrize("value,threshold", [("0.5", "0.5"), ("0.6", "0.5"), ("0", "0")])
def test_measurement_requires_actual_less_than_not_claimed_pass(value, threshold):
    with pytest.raises(DiagnosticModelError):
        replace(MEASUREMENT, value=value, threshold=threshold)


def test_measurement_does_not_round_using_ambient_decimal_context():
    value = "0.12345678901234567890123456789"
    with localcontext() as context:
        context.prec = 2
        result = replace(MEASUREMENT, value=value, threshold="0.1234567890123456789012345679")
    assert result.value == value


@pytest.mark.parametrize("diagnostic", [LOW, MISSING, SUMMARY, UNASSIGNED])
def test_evidence_derived_rule_kind_and_error_severity(diagnostic):
    assert diagnostic.rule_id == ("KC-COV-003" if diagnostic is UNASSIGNED else "KC-GRAPH-002")
    assert diagnostic.kind == ("low_confidence" if diagnostic is LOW else "insufficient_evidence")
    assert diagnostic.severity == "error"
    for field in ("kind", "rule_id", "severity"):
        wire = diagnostic.to_mapping()
        wire[field] = "pass"
        with pytest.raises(DiagnosticModelError):
            EvidenceDiagnostic.from_mapping(wire)


@pytest.mark.parametrize(
    "diagnostic,field,value",
    [
        (LOW, "measurement", None),
        (LOW, "measurement", {}),
        (LOW, "continuity_claim", CLAIM),
        (LOW, "summary", "unrelated"),
        (LOW, "evidence_refs", (FACT,)),
        (MISSING, "continuity_claim", None),
        (MISSING, "continuity_claim", OTHER_CLAIM),
        (MISSING, "measurement", MEASUREMENT),
        (MISSING, "summary", "unrelated"),
        (MISSING, "evidence_refs", (FACT,)),
        (SUMMARY, "summary", None),
        (SUMMARY, "summary", ""),
        (SUMMARY, "measurement", MEASUREMENT),
        (SUMMARY, "continuity_claim", CLAIM),
        (SUMMARY, "scope_ref", FACT),
        (SUMMARY, "evidence_refs", (FACT,)),
        (UNASSIGNED, "measurement", MEASUREMENT),
        (UNASSIGNED, "continuity_claim", CLAIM),
        (UNASSIGNED, "summary", "unrelated"),
        (UNASSIGNED, "reason_code", "computed_default_pass"),
    ],
)
def test_conditional_details_cannot_be_missing_or_irrelevantly_mixed(diagnostic, field, value):
    with pytest.raises(DiagnosticModelError):
        replace(diagnostic, **{field: value})


def test_inherited_low_cause_points_to_real_entity_not_affected_fact_score():
    assert LOW.measurement.observation_ref == ENTITY
    assert LOW.affected_refs == (AFFECTED,)
    assert LOW.measurement.observation_ref != LOW.affected_refs[0]


def test_merge_preserves_caller_bound_cause_hash_not_self_hash():
    assert MERGE.cause_id == _hash("raw-proposal")
    assert MERGE.cause_id != MERGE.canonical_hash
    changed = replace(MERGE, rationale="different raw text must be checked upstream")
    assert changed.cause_id == MERGE.cause_id and changed.canonical_hash != MERGE.canonical_hash
    for field, refs in (
        ("entity_refs", (ENTITY,)),
        ("entity_refs", (ENTITY, FACT)),
        ("evidence_refs", (ENTITY,)),
        ("evidence_refs", (AFFECTED,)),
    ):
        with pytest.raises(DiagnosticModelError):
            replace(MERGE, **{field: refs})


@pytest.mark.parametrize(
    "change",
    [
        "unknown",
        "duplicate",
        "only_one",
        "wrong_kind",
        "missing_entity",
        "extra_entity",
        "missing_cause",
        "orphan_claim",
        "orphan_cause",
    ],
)
def test_conflict_claim_and_proposal_local_closure(change):
    diagnostic, claims, causes = MERGE_DIAGNOSTIC, MERGE_CLAIMS, (MERGE,)
    with pytest.raises(DiagnosticModelError):
        if change == "unknown":
            diagnostic = replace(diagnostic, competing_claim_ids=("entity_1", "unknown"))
        elif change == "duplicate":
            diagnostic = replace(diagnostic, competing_claim_ids=("entity_1", "entity_1"))
        elif change == "only_one":
            diagnostic = replace(diagnostic, competing_claim_ids=("entity_1",))
        elif change == "wrong_kind":
            claims = (replace(claims[0], payload=CLAIM), claims[1])
        elif change == "missing_entity":
            claims = (claims[0], replace(claims[1], payload=claims[0].payload))
        elif change == "extra_entity":
            foreign = replace(ENTITY, object_id=_hash("third"))
            causes = (replace(MERGE, entity_refs=(*MERGE.entity_refs, foreign)),)
        elif change == "missing_cause":
            causes = ()
        elif change == "orphan_claim":
            claims = (*claims, ConflictClaim("unused", IdentityObservationValue(ENTITY)))
        else:
            causes = (*causes, replace(MERGE, cause_id=_hash("other"), merge_id="other"))
        replace(CONFLICT_SET, items=(diagnostic,), claims=claims, merge_causes=causes)


@pytest.mark.parametrize(
    "change", ["same_bool", "same_direction", "same_window", "identity_claim", "third_claim"]
)
def test_timeline_conflict_has_two_actual_opposing_window_side_claims(change):
    claims = TIMELINE_CLAIMS
    if change == "same_bool":
        other = replace(
            OTHER_CLAIM, continues=True, state_fact_refs=(replace(FACT, member_ref=SECOND_PACK),)
        )
    elif change == "same_direction":
        other = replace(OTHER_CLAIM, direction="next")
    elif change == "same_window":
        other = replace(OTHER_CLAIM, source_window_ref=WINDOW)
    elif change == "identity_claim":
        other = IdentityObservationValue(ENTITY)
    else:
        other = OTHER_CLAIM
        claims = (*claims, ConflictClaim("third", CLAIM))
    claims = (claims[0], replace(claims[1], payload=other), *claims[2:])
    diagnostic = replace(
        TIMELINE_DIAGNOSTIC, competing_claim_ids=tuple(claim.claim_id for claim in claims)
    )
    with pytest.raises(DiagnosticModelError):
        replace(CONFLICT_SET, items=(diagnostic,), claims=claims, merge_causes=())


@pytest.mark.parametrize(
    "artifact_type,object_type",
    [
        ("coverage_ledger", "taint_seed"),
        ("coverage_admission", "admission"),
        ("evidence_diagnostics", "diagnostic"),
        ("conflict_diagnostics", "claim"),
        ("dependency_closure_proof", "closure"),
        ("transcript_set", "word"),
        ("vlm_semantic_pack", "candidate"),
        ("narrative_graph", "event"),
        ("generation_invocation", "draft"),
    ],
)
def test_no_backward_self_future_nonsemantic_or_fabricated_audit_reference(
    artifact_type, object_type
):
    owner = replace(PACK, artifact_type=artifact_type)
    ref = SemanticObjectRef(owner, object_type, _hash("object"))
    for field, value in (("scope_ref", ref), ("evidence_refs", (ref,)), ("affected_refs", (ref,))):
        with pytest.raises(DiagnosticModelError):
            replace(UNASSIGNED, **{field: value})


@pytest.mark.parametrize("value", VALUES, ids=lambda value: type(value).__name__)
def test_every_direct_collection_is_immutable_strict_and_rejects_duplicates(value):
    for field in fields(value):
        current = getattr(value, field.name)
        if type(current) is tuple:
            for bad in (list(current), (None,)):
                with pytest.raises(DiagnosticModelError):
                    replace(value, **{field.name: bad})
            if current:
                with pytest.raises(DiagnosticModelError):
                    replace(value, **{field.name: (*current, current[0])})


@pytest.mark.parametrize("value", VALUES, ids=lambda value: type(value).__name__)
def test_direct_text_fields_are_nonempty_actual_utf8_strings(value):
    for field in fields(value):
        if type(getattr(value, field.name)) is str:
            for bad in (True, 1, "", "\ud800"):
                with pytest.raises(DiagnosticModelError):
                    replace(value, **{field.name: bad})


def _paths(value, path=()):
    if type(value) is dict:
        yield path
        for key, child in value.items():
            yield from _paths(child, (*path, key))
    elif type(value) is list:
        for index, child in enumerate(value):
            yield from _paths(child, (*path, index))


@pytest.mark.parametrize("value", [EVIDENCE_SET, CONFLICT_SET])
def test_every_nested_wire_object_is_closed_without_optional_omission(value):
    original = value.to_mapping()
    for path in _paths(original):
        for mode in ("add", "remove"):
            wire = deepcopy(original)
            node = wire
            for key in path:
                node = node[key]
            if mode == "add":
                node["accepted"] = True
            else:
                node.pop(next(iter(node)))
            with pytest.raises(DiagnosticModelError):
                type(value).from_mapping(wire)


def test_frozen_nested_values_fresh_mappings_and_canonical_set_order():
    for value in VALUES:
        field = fields(value)[0]
        with pytest.raises(FrozenInstanceError):
            setattr(value, field.name, getattr(value, field.name))
    assert (
        replace(
            CONFLICT_SET,
            items=tuple(reversed(CONFLICT_SET.items)),
            claims=tuple(reversed(CONFLICT_SET.claims)),
        )
        == CONFLICT_SET
    )
    assert replace(EVIDENCE_SET, items=tuple(reversed(EVIDENCE_SET.items))) == EVIDENCE_SET
    wire = CONFLICT_SET.to_mapping()
    parsed = ConflictDiagnostics.from_mapping(wire)
    wire["claims"][0]["payload"]["observation_ref"]["member_ref"]["scope"]["key"] = "changed"
    assert parsed == CONFLICT_SET and parsed.to_mapping() != wire


def test_empty_sets_are_content_not_admission_and_draft_hashes_remain_distinct():
    for value in (
        replace(EVIDENCE_SET, items=()),
        replace(CONFLICT_SET, items=(), claims=(), merge_causes=()),
    ):
        assert type(value).from_mapping(value.to_mapping()) == value
        assert value.raw_draft_sha256 != value.canonical_draft_sha256
        assert not hasattr(value, "passed") and not hasattr(value, "rule_results")


@pytest.mark.parametrize(
    "model",
    [
        ContinuityClaimValue,
        IdentityObservationValue,
        ConflictClaim,
        MergeProposalCause,
        ConfidenceMeasurement,
        EvidenceDiagnostic,
        ConflictDiagnostic,
        EvidenceDiagnostics,
        ConflictDiagnostics,
    ],
)
@pytest.mark.parametrize("value", [None, [], (), True, "{}"])
def test_wrong_wire_containers_are_never_coerced(model, value):
    with pytest.raises(DiagnosticModelError):
        model.from_mapping(value)
