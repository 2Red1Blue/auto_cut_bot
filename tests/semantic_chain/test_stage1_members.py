"""Exact six-member decoding and check-value boundaries, not persistence."""

from dataclasses import replace

import pytest
from autocut_kernel.semantic_chain.stage1_checks import Stage1Check
from autocut_kernel.semantic_chain.stage1_members import decode_coverage_members
from autocut_kernel.store.models import canonical_payload_hash

from tests.semantic_chain.test_coverage_compiler import _compile


def test_member_decode_is_order_independent_and_does_not_grant_admission():
    result, _ = _compile()
    scope = result.members[0].scope
    first = decode_coverage_members(result.members, scope=scope)
    assert first == decode_coverage_members(tuple(reversed(result.members)), scope=scope)
    assert first.coverage_ledger.actual_counts.fact == 2
    assert not hasattr(first, "admission")
    with pytest.raises(ValueError):
        first.identity("coverage_admission")


@pytest.mark.parametrize("change", ["missing", "duplicate", "type", "logical_id", "scope", "revision", "hash"])
def test_member_shape_and_binding_fail_closed(change):
    result, _ = _compile()
    members = result.members
    scope = members[0].scope
    if change == "missing":
        members = members[:-1]
    elif change == "duplicate":
        members = (*members[:-1], members[0])
    else:
        fields = {
            "type": {"artifact_type": "foreign"}, "logical_id": {"logical_id": "different"},
            "scope": {"scope": replace(scope, key="foreign")}, "revision": {"revision": 2},
            "hash": {"content_hash": "sha256:" + "b" * 64},
        }
        members = (replace(members[0], **fields[change]), *members[1:])
    with pytest.raises(ValueError):
        decode_coverage_members(members, scope=scope)


@pytest.mark.parametrize("rule,status,codes", [
    ("bad", "pass", ()), ("KC-IN-001", "good", ()), ("KC-IN-001", "pass", ("bad",)),
    ("KC-IN-001", "fail", ()), ("KC-IN-001", "indeterminate", ()),
    ("KC-IN-001", "fail", ("UPPER",)), ("KC-IN-001", "fail", ("x", "x")),
    ("KC-IN-001", "fail", ("z", "a")), ("KC-IN-001", "fail", ["x"]),
])
def test_rule_value_has_no_implicit_pass_or_hidden_reason(rule, status, codes):
    with pytest.raises(ValueError):
        Stage1Check(rule, status, codes)


def test_rule_mapping_does_not_expose_mutable_internal_values():
    check = Stage1Check("KC-IN-001", "indeterminate", ("store_read_not_performed",))
    wire = check.to_mapping()
    wire["violation_codes"].clear()
    assert check.violation_codes == ("store_read_not_performed",)


def test_stage1_check_from_mapping_is_strict_on_shape_and_primitive_types():
    wire = Stage1Check("KC-IN-001", "fail", ("store_read_not_performed",)).to_mapping()
    assert Stage1Check.from_mapping(wire).to_mapping() == wire
    for field in tuple(wire):
        with pytest.raises(ValueError):
            Stage1Check.from_mapping({key: value for key, value in wire.items() if key != field})
    for malformed in (
        {**wire, "extra": None},
        {**wire, "rule_id": 1},
        {**wire, "status": True},
        {**wire, "violation_codes": ("store_read_not_performed",)},
        {**wire, "violation_codes": [True]},
        {**wire, "violation_codes": ["store_read_not_performed", "store_read_not_performed"]},
    ):
        with pytest.raises(ValueError):
            Stage1Check.from_mapping(malformed)


@pytest.mark.parametrize("nested", [False, True])
def test_duplicate_json_keys_are_rejected_even_when_store_hash_is_unchanged(nested):
    result, _ = _compile()
    members = list(result.members)
    original = next(member for member in members if member.artifact_type == "narrative_graph")
    if nested:
        raw = original.payload_json.replace('"label":', '"label":"shadowed-forged-value","label":', 1)
    else:
        raw = '{"graph_id":"shadowed-forged-value",' + original.payload_json[1:]
    assert canonical_payload_hash(raw) == original.content_hash
    members[members.index(original)] = replace(original, payload_json=raw)
    with pytest.raises(ValueError, match="duplicate JSON object key"):
        decode_coverage_members(tuple(members), scope=original.scope)
