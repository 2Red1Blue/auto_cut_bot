"""Independent truth checks, with synthetic Store inputs and hash-closed attacks.

The compiler is used only to prepare positive pending values. No test claims
database commitment, an accepted Admission or a successful provider call.
"""

import json
from dataclasses import replace
from decimal import Decimal

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes, canonical_json_hash
from autocut_kernel.semantic_chain.coverage_analysis import Stage1CoveragePolicy
from autocut_kernel.semantic_chain.coverage_verification import verify_coverage_members
from autocut_kernel.semantic_chain.stage1_draft import stage1_draft_prompt_inputs
from autocut_kernel.semantic_chain.stage1_members import decode_coverage_members
from autocut_kernel.vlm.models import derive_vlm_global_id

from tests.semantic_chain.test_continuity_analysis import _inputs
from tests.semantic_chain.test_coverage_analysis import _clean_inputs, _replace_pack
from tests.semantic_chain.test_coverage_compiler import COVERAGE, _compile
from tests.semantic_chain.test_stage1_draft import POLICY, _draft

RULES = (
    "KC-COV-001",
    "KC-COV-002",
    "KC-COV-003",
    "KC-COV-004",
    "KC-COV-005",
    "KC-EXCLUDE-001",
    "KC-GATE-001",
)


def _case(inputs=None, *, merge=False, policy=COVERAGE, empty=False):
    inputs = _clean_inputs() if inputs is None else inputs
    payload = _draft(inputs)
    if not merge:
        payload["merge_proposals"] = []
    if empty:
        for key in ("beats", "obligations", "story_threads", "merge_proposals"):
            payload[key] = []
    raw = canonical_json_bytes(payload)
    result, _ = _compile(inputs, raw=raw, policy=policy)
    return inputs, raw, result.members, policy


def _verify(case, *, members=None, raw=None, policy=None):
    inputs, original, pending, coverage = case
    return verify_coverage_members(
        inputs,
        original if raw is None else raw,
        members=pending if members is None else members,
        draft_policy=POLICY,
        coverage_policy=coverage if policy is None else policy,
    )


def _status(checks):
    return {check.rule_id: check.status for check in checks}


def _rewrite(members, kind, change):
    """Rehash every changed member and all downstream member identities.

    The decoder assertion proves negative tests exercise truth checks, rather than
    merely broken payload hashes, stale upstream refs or malformed DTOs.
    """
    payloads = {member.artifact_type: json.loads(member.payload_json) for member in members}
    change(payloads[kind])
    hashes = {}

    def rebind(value):
        if isinstance(value, dict):
            if "artifact_type" in value and "content_hash" in value:
                owner = value["artifact_type"]
                if owner in hashes:
                    value["content_hash"] = hashes[owner]
            for child in value.values():
                rebind(child)
        elif isinstance(value, list):
            for child in value:
                rebind(child)

    result = []
    for member in members:
        payload = payloads[member.artifact_type]
        rebind(payload)
        digest = canonical_json_hash(payload)
        result.append(
            replace(
                member, content_hash=digest, payload_json=canonical_json_bytes(payload).decode()
            )
        )
        hashes[member.artifact_type] = digest
    result = tuple(result)
    decode_coverage_members(result, scope=members[0].scope)
    return result


def _low(kind="entity"):
    def lower(pack):
        if kind == "window_summary":
            return replace(
                pack, window_summary=replace(pack.window_summary, confidence=Decimal("0.1"))
            )
        collection = getattr(pack, {"entity": "entities", "fact": "facts", "event": "events"}[kind])
        first = replace(
            collection[0], support=replace(collection[0].support, confidence=Decimal("0.1"))
        )
        return replace(
            pack,
            **{
                {"entity": "entities", "fact": "facts", "event": "events"}[kind]: (
                    first,
                    *collection[1:],
                )
            },
        )

    return _replace_pack(_clean_inputs(), 0, lower)


def test_exact_seven_clean_checks_and_determinism():
    case = _case()
    checks = _verify(case)
    assert tuple(check.rule_id for check in checks) == RULES
    assert all(check.status == "pass" and not check.violation_codes for check in checks)
    assert _verify(case) == checks


@pytest.mark.parametrize("kind", ["entity", "fact", "event", "window_summary"])
def test_real_low_observation_retains_contract_checks_but_fails_gate(kind):
    checks = _verify(_case(_low(kind)))
    assert all(check.status == "pass" for check in checks[:-1])
    assert checks[-1].status == "fail"
    assert "actual_direct_taint" in checks[-1].violation_codes


@pytest.mark.parametrize("case_kind", ["merge", "conflict", "missing", "unassigned", "summary"])
def test_honest_unknowns_and_gaps_are_not_false_contract_failures(case_kind):
    if case_kind == "merge":
        case = _case(merge=True)
    elif case_kind in ("conflict", "missing"):
        inputs = _inputs(
            {"start": 1000, "following": True}, {"start": 1100 if case_kind == "conflict" else 1300}
        )
        case = _case(inputs, policy=Stage1CoveragePolicy("0", "strict_global"))
    else:
        inputs = _replace_pack(
            _clean_inputs(),
            1,
            lambda pack: replace(
                pack,
                window_summary=replace(pack.window_summary, fact_refs=(), event_refs=()),
            ),
        )
        case = _case(inputs, empty=case_kind == "unassigned")
    checks = _verify(case)
    assert all(check.status == "pass" for check in checks[:-1])
    assert checks[-1].status == "fail"


def test_no_compiler_projector_or_coverage_analyzer_is_called(monkeypatch):
    case = _case(merge=True)

    def forbidden(*args, **kwargs):
        raise AssertionError("producer called by independent verifier")

    for path in (
        "autocut_kernel.semantic_chain.coverage_analysis.analyze_observation_coverage",
        "autocut_kernel.semantic_chain.coverage_compiler.analyze_observation_coverage",
        "autocut_kernel.semantic_chain.coverage_compiler.compile_stage1_coverage",
        "autocut_kernel.semantic_chain.coverage_compiler.project_narrative",
        "autocut_kernel.semantic_chain.narrative_projection.project_narrative",
    ):
        monkeypatch.setattr(path, forbidden)
    assert all(check.status == "pass" for check in _verify(case)[:-1])


@pytest.mark.parametrize("kind", ["fact", "event", "obligation"])
def test_self_consistent_counts_cannot_hide_removed_raw_unit(kind):
    case = _case()

    def remove(payload):
        row = next(row for row in payload["rows"] if row["unit_type"] == kind)
        payload["rows"].remove(row)
        payload["conservation"][kind]["input_count"] -= 1
        payload["conservation"][kind]["ledger_count"] -= 1

    changed = _rewrite(case[2], "coverage_ledger", remove)
    assert _status(_verify(case, members=changed))["KC-COV-001"] == "fail"


@pytest.mark.parametrize("field", ["fact_refs", "event_refs"])
def test_window_raw_membership_is_checked_independently(field):
    case = _case()
    changed = _rewrite(case[2], "coverage_ledger", lambda p: p["windows"][0].update({field: []}))
    assert _status(_verify(case, members=changed))["KC-COV-002"] == "fail"


def test_required_window_cannot_disappear_with_self_consistent_counts():
    case = _case()

    def remove(payload):
        window = payload["windows"].pop()
        payload["rows"] = [
            row
            for row in payload["rows"]
            if row["unit_type"] != "source_window"
            or row["unit_ref"]["window_id"] != window["window_id"]
        ]
        for key in ("input_count", "ledger_count"):
            payload["conservation"]["source_window"][key] -= 1

    changed = _rewrite(case[2], "coverage_ledger", remove)
    assert _status(_verify(case, members=changed))["KC-COV-002"] == "fail"


def test_wrong_exact_raw_owner_is_not_same_evidence():
    case = _case()

    def substitute(payload):
        row = next(row for row in payload["rows"] if row["unit_type"] == "fact")
        row["evidence_refs"][0]["member_ref"]["content_hash"] = "sha256:" + "a" * 64

    changed = _rewrite(case[2], "coverage_ledger", substitute)
    assert _status(_verify(case, members=changed))["KC-COV-003"] == "fail"


def test_assignment_to_nonexistent_but_well_typed_node_is_rejected():
    case = _case()

    def substitute(payload):
        row = next(row for row in payload["rows"] if row["unit_type"] == "obligation")
        row["assignment_refs"][0]["object_id"] = "invented-beat"

    changed = _rewrite(case[2], "coverage_ledger", substitute)
    assert _status(_verify(case, members=changed))["KC-COV-003"] == "fail"


@pytest.mark.parametrize(
    "field,value", [("value", "0.2"), ("threshold", "0.6"), ("policy_sha256", "sha256:" + "a" * 64)]
)
def test_rehashed_measurement_still_must_equal_actual_raw_confidence(field, value):
    case = _case(_low())
    changed = _rewrite(
        case[2],
        "evidence_diagnostics",
        lambda p: p["items"][0]["measurement"].update({field: value}),
    )
    assert _status(_verify(case, members=changed))["KC-COV-003"] == "fail"


def test_diagnostic_affected_members_cannot_drop_inherited_cause():
    case = _case(_low())

    def omit(payload):
        item = payload["items"][0]
        item["affected_refs"] = [
            ref for ref in item["affected_refs"] if ref["object_type"] != "event"
        ]

    changed = _rewrite(case[2], "evidence_diagnostics", omit)
    assert _status(_verify(case, members=changed))["KC-COV-003"] == "fail"


def test_raw_merge_rationale_is_not_authenticated_by_its_claimed_hash():
    case = _case(merge=True)
    changed = _rewrite(
        case[2],
        "conflict_diagnostics",
        lambda p: p["merge_causes"][0].update(rationale="invented rationale"),
    )
    assert _status(_verify(case, members=changed))["KC-COV-004"] == "fail"


def test_continuity_directions_are_checked_against_real_neighbor_claims():
    case = _case(
        _inputs({"start": 1000, "following": True}, {"start": 1100}),
        policy=Stage1CoveragePolicy("0", "strict_global"),
    )

    def swap(payload):
        for claim in payload["claims"]:
            value = claim["payload"]
            value["direction"] = "previous" if value["direction"] == "next" else "next"

    changed = _rewrite(case[2], "conflict_diagnostics", swap)
    assert _status(_verify(case, members=changed))["KC-COV-004"] == "fail"


@pytest.mark.parametrize("field", ["root_refs", "frontier_refs"])
def test_actual_cause_roots_and_unknown_frontier_cannot_shrink(field):
    case = _case(merge=True)

    def omit(payload):
        seed = payload["taint_seeds"][0]
        # Canonical entity origins are extra to the owning coverage row, so the
        # model remains valid while actual unknown-cause reachability is lost.
        seed[field] = [ref for ref in seed[field] if ref["object_type"] != "entity"]

    changed = _rewrite(case[2], "coverage_ledger", omit)
    assert _status(_verify(case, members=changed))["KC-COV-003"] == "fail"


def test_unknown_frontier_cannot_be_hidden_by_changing_declared_reason():
    case = _case(merge=True)

    def erase(payload):
        for seed in payload["taint_seeds"]:
            seed.update(reason_codes=["low_confidence"], frontier_refs=[], frontier_window_ids=[])

    changed = _rewrite(case[2], "coverage_ledger", erase)
    checks = _status(_verify(case, members=changed))
    assert checks["KC-COV-003"] == checks["KC-GATE-001"] == "fail"


def test_all_resolved_row_flags_cannot_hide_real_taint():
    case = _case(_low())
    clean = _case()
    clean_rows = json.loads(clean[2][-1].payload_json)["rows"]
    dispositions = {row["coverage_id"]: row["disposition"] for row in clean_rows}

    def erase(payload):
        for row in payload["rows"]:
            row.update(
                resolution_status="resolved",
                disposition=dispositions.get(row["coverage_id"], "supporting"),
                diagnostic_refs=[],
                taint_seed_id=None,
            )
        payload["taint_seeds"] = []

    changed = _rewrite(case[2], "coverage_ledger", erase)
    checks = _verify(case, members=changed)
    assert _status(checks)["KC-COV-003"] == "fail"
    assert checks[-1].violation_codes == ("actual_direct_taint",)


def test_raw_bytes_identity_is_distinct_from_canonical_draft():
    case = _case()
    changed_raw = json.dumps(json.loads(case[1]), indent=2).encode()
    checks = _status(_verify(case, raw=changed_raw))
    assert checks["KC-COV-003"] == checks["KC-COV-004"] == "fail"


def test_explicit_policy_change_recomputes_real_threshold():
    case = _case(_low())
    checks = _verify(case, policy=Stage1CoveragePolicy("0", "strict_global"))
    assert _status(checks)["KC-COV-003"] == "fail"
    assert "actual_direct_taint" not in checks[-1].violation_codes


def test_transitive_causal_taint_is_not_confused_with_direct_resolution():
    def causal_pair(pack):
        fact_b = replace(
            pack.facts[0],
            local_fact_id="fact_b",
            fact_id=derive_vlm_global_id("fact", "fact_b", pack.request_identity_sha256),
        )
        event_a = replace(
            pack.events[0], support=replace(pack.events[0].support, confidence=Decimal("0.1"))
        )
        event_b = replace(
            pack.events[0],
            local_event_id="event_b",
            event_id=derive_vlm_global_id("event", "event_b", pack.request_identity_sha256),
            fact_refs=(fact_b.fact_id,),
            cause_event_refs=(event_a.event_id,),
        )
        event_a = replace(event_a, effect_event_refs=(event_b.event_id,))
        return replace(
            pack,
            facts=(*pack.facts, fact_b),
            events=(event_a, event_b),
            window_summary=replace(
                pack.window_summary, event_refs=tuple(sorted((event_a.event_id, event_b.event_id)))
            ),
        )

    case = _case(_replace_pack(_clean_inputs(), 1, causal_pair))
    values = decode_coverage_members(case[2], scope=case[0].source_manifest.reference.scope)
    event_b = case[0].inputs[1].semantic_pack.semantic_pack.events[1].event_id
    row = next(
        row
        for row in values.coverage_ledger.rows
        if row.unit_type == "event" and row.unit_ref.object_id == event_b
    )
    assert row.resolution_status == "resolved"
    checks = _verify(case)
    assert all(check.status == "pass" for check in checks[:-1])
    assert checks[-1].status == "fail"  # actual ancestor is still tainted


@pytest.mark.parametrize(
    "entity_low,fact_low,event_low,summary_low",
    [
        (True, True, True, True),
        (False, True, True, False),
        (True, False, True, False),
        (True, True, False, True),
    ],
)
@pytest.mark.parametrize("empty", [False, True])
def test_multiple_actual_causes_preserve_direct_inheritance_and_assignments(
    entity_low,
    fact_low,
    event_low,
    summary_low,
    empty,
):
    def lower(pack):
        def support(value, low):
            return (
                replace(value, support=replace(value.support, confidence=Decimal("0.1")))
                if low
                else value
            )

        return replace(
            pack,
            entities=(support(pack.entities[0], entity_low),),
            facts=(support(pack.facts[0], fact_low),),
            events=(support(pack.events[0], event_low),),
            window_summary=replace(pack.window_summary, confidence=Decimal("0.1"))
            if summary_low
            else pack.window_summary,
        )

    checks = _verify(_case(_replace_pack(_clean_inputs(), 0, lower), empty=empty))
    assert all(check.status == "pass" for check in checks[:-1])
    assert checks[-1].status == "fail"


def test_unused_low_entity_still_taints_its_required_window():
    def extra(pack):
        entity = replace(
            pack.entities[0],
            local_entity_id="unused",
            entity_id=derive_vlm_global_id("entity", "unused", pack.request_identity_sha256),
            support=replace(pack.entities[0].support, confidence=Decimal("0.1")),
        )
        return replace(pack, entities=(*pack.entities, entity))

    case = _case(_replace_pack(_clean_inputs(), 0, extra))
    checks = _verify(case)
    assert all(check.status == "pass" for check in checks[:-1])
    assert "actual_direct_taint" in checks[-1].violation_codes


def test_eventless_missing_summary_keeps_standalone_fact_and_fails_gate():
    original = _clean_inputs()
    payload = _draft(original)
    payload["merge_proposals"] = []
    inputs = _replace_pack(
        original,
        1,
        lambda pack: replace(
            pack,
            events=(),
            candidate_hypotheses=(),
            window_summary=replace(pack.window_summary, event_refs=(), fact_refs=()),
        ),
    )
    payload["input_binding_sha256"] = stage1_draft_prompt_inputs(inputs, policy=POLICY)[
        "input_binding_sha256"
    ]
    raw = canonical_json_bytes(payload)
    result, _ = _compile(inputs, raw=raw)
    checks = _verify((inputs, raw, result.members, COVERAGE))
    assert all(check.status == "pass" for check in checks[:-1])
    assert checks[-1].status == "fail"


def test_malformed_shape_and_payload_hash_fail_closed():
    case = _case()
    with pytest.raises(ValueError):
        _verify(case, members=case[2][:-1])
    with pytest.raises(ValueError):
        _verify(
            case, members=(replace(case[2][0], content_hash="sha256:" + "a" * 64), *case[2][1:])
        )
    with pytest.raises(ValueError):
        _verify(case, raw=b"{}")
