"""Exact 3N+1 content decoding; synthetic audit rows are not authorization."""

import json
from dataclasses import replace

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes
from autocut_kernel.semantic_chain.editorial_result import decode_editorial_members
from autocut_kernel.store.models import ArtifactMember, canonical_payload_hash

from tests.semantic_chain.test_editorial_admission import admission_for
from tests.semantic_chain.test_editorial_evaluation import evaluate_case, evaluation_case, rewrite


def result_members(evaluation, request):
    admission = admission_for(evaluation, request)
    raw = canonical_json_bytes(admission.to_mapping()).decode()
    first = evaluation.expected_business.members[0]
    return (*evaluation.expected_business.members, ArtifactMember("semantic_feasibility_admission",
        "semantic_feasibility_admission", first.revision, first.scope, canonical_payload_hash(raw), raw))


@pytest.fixture(scope="module")
def case():
    original = evaluation_case()
    evaluation = evaluate_case(original)
    return original, evaluation, result_members(evaluation, original[1])


def test_complete_seven_member_content_roundtrip(case):
    _, evaluation, members = case
    result = decode_editorial_members(members, contexts=evaluation.contexts)
    assert len(result.members) == 7
    assert result.business == evaluation.expected_business
    assert result.admission.feasibility == evaluation.feasibility
    assert result.admission.input_binding_sha256 != result.admission.feasibility.input_binding_sha256


@pytest.mark.parametrize("change", ["missing", "extra", "reorder", "duplicate", "list", "admission_first"])
def test_exact_3n_plus_one_order_is_required(case, change):
    _, evaluation, members = case
    variants = {"missing": members[:-1], "extra": members + members[-1:], "reorder": members[3:6] + members[:3] + members[-1:],
                "duplicate": members[:3] * 2 + members[-1:], "list": list(members), "admission_first": members[-1:] + members[:-1]}
    with pytest.raises(ValueError):
        decode_editorial_members(variants[change], contexts=evaluation.contexts)


@pytest.mark.parametrize("field", ["logical_id", "artifact_type", "revision", "scope", "content_hash"])
def test_admission_member_identity_cannot_drift(case, field):
    _, evaluation, members = case
    member = members[-1]
    values = {"logical_id": "foreign", "artifact_type": "portfolio_admission", "revision": 2,
              "scope": replace(member.scope, key="foreign"), "content_hash": "sha256:" + "f" * 64}
    with pytest.raises(ValueError):
        decode_editorial_members((*members[:-1], replace(member, **{field: values[field]})), contexts=evaluation.contexts)


@pytest.mark.parametrize("field", ["input_binding_sha256", "projection_sha256", "subject"])
def test_rehashed_admission_must_bind_actual_context_projection_subject(case, field):
    _, evaluation, members = case
    mapping = json.loads(members[-1].payload_json)
    if field == "input_binding_sha256":
        mapping[field] = "sha256:" + "f" * 64
    elif field == "projection_sha256":
        mapping["feasibility"][field] = "sha256:" + "f" * 64
    else:
        mapping["business_members"][0]["content_hash"] = "sha256:" + "f" * 64
    with pytest.raises(ValueError):
        decode_editorial_members((*members[:-1], rewrite(members[-1], mapping)), contexts=evaluation.contexts)


@pytest.mark.parametrize("position", ["root", "nested"])
def test_duplicate_json_is_rejected_even_with_recomputed_member_hash(case, position):
    _, evaluation, members = case
    raw = members[-1].payload_json
    if position == "root":
        raw = raw[:-1] + ',"schema_version":"stage3-semantic-feasibility-admission-v1"}'
    else:
        raw = raw.replace('"rule_id":"SS-IN-001"', '"rule_id":"SS-IN-001","rule_id":"SS-IN-001"', 1)
    altered = replace(members[-1], payload_json=raw, content_hash=canonical_payload_hash(raw))
    with pytest.raises(ValueError):
        decode_editorial_members((*members[:-1], altered), contexts=evaluation.contexts)


def test_fully_rehashed_search_telemetry_is_content_not_independent_truth(case):
    original, evaluation, members = case
    admission = admission_for(evaluation, original[1])
    search = replace(admission.feasibility.material_search, examined_states=admission.feasibility.material_search.examined_states + 1)
    forged = replace(admission, feasibility=replace(admission.feasibility, material_search=search))
    altered = rewrite(members[-1], forged.to_mapping())
    decoded = decode_editorial_members((*members[:-1], altered), contexts=evaluation.contexts)
    # Structural decoding deliberately cannot authenticate search execution.
    # Actual raw replay recomputes the whole result, not merely "feasible".
    recomputed = evaluate_case(original)
    assert decoded.admission.feasibility != recomputed.feasibility
    assert decoded.admission.canonical_hash != admission_for(recomputed, original[1]).canonical_hash
