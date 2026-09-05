"""Synthetic saved-context diagnostics; no real provider or Store replay evidence."""

import json
from copy import deepcopy

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes, sha256_bytes
from autocut_kernel.semantic_chain.member_refs import SemanticMemberIdentity

from scripts import diagnose_stage2_debug as diagnostic_cli
from tests.semantic_chain.test_material_support import material_case


@pytest.fixture
def offline_case():
    case = material_case()
    stage1, projection = case["stage1"], case["projection"]
    context = {
        "schema_version": "stage2-proposal-context-v1",
        "input_binding_sha256": case["draft"].input_binding_sha256,
        # Synthetic identities stand in for the saved request's committed refs.
        # Neither this fixture nor the diagnostic CLI verifies their authority.
        "stage1_members": [SemanticMemberIdentity.from_artifact_member(member).to_mapping()
                           for member in stage1.members],
        "source_grant": case["inputs"].source_grant.to_mapping(),
        "candidate_catalog": {
            "member_ref": SemanticMemberIdentity.from_artifact_member(projection.member).to_mapping(),
            "payload": projection.catalog.to_mapping(),
        },
        "policies": {name: case[name].to_mapping()
                     for name in ("candidate_policy", "job_policy", "story_policy")},
    }
    for kind, payload in (
        ("narrative_graph", stage1.coverage.narrative_graph),
        ("episode_digest_set", stage1.coverage.episode_digests),
        ("event_card_set", stage1.coverage.event_cards),
    ):
        context[kind] = {"member_ref": stage1.coverage.identity(kind).to_mapping(),
                         "payload": payload.to_mapping()}
    return context, case["draft"].to_mapping()


def _diagnose(context, response):
    return diagnostic_cli.diagnose(canonical_json_bytes(context), canonical_json_bytes(response))


def test_valid_offline_case_only_claims_proposal_rule_validation(offline_case):
    context, response = offline_case
    before = deepcopy(offline_case)
    report, code = _diagnose(context, response)
    assert code == 0
    assert report["status"] == "proposal_rules_passed"
    assert report["scope"] == "offline_structure_and_proposal_rules_only"
    assert report["authority_verified"] is False
    assert report["full_policy_and_resource_verification"] is False
    assert report["provider_calls"] == 0
    assert report["proposal_count"] == 2
    assert report["context_sha256"] == sha256_bytes(canonical_json_bytes(context))
    assert report["raw_sha256"] == sha256_bytes(canonical_json_bytes(response))
    assert offline_case == before


def test_offline_wrong_person_type_keeps_exact_validator_diagnostic(offline_case):
    context, response = offline_case
    person = next(node for node in context["narrative_graph"]["payload"]["nodes"]
                  if node["node_type"] == "entity" and node["attributes"]["entity_kind"] == "person")
    response["proposals"][0]["key_character_refs"] = [{
        "member_ref": context["narrative_graph"]["member_ref"],
        "object_type": "character", "object_id": person["node_id"],
    }]
    report, code = _diagnose(context, response)
    assert code == 1
    assert report["status"] == "proposal_rules_rejected"
    assert report["diagnostic"]["error_code"] == "GRAPH_REFERENCE_TYPE_MISMATCH"
    assert report["diagnostic"]["actual_object_type"] == "entity"
    assert report["diagnostic"]["json_path"] == "$.proposals[0].key_character_refs[0]"


@pytest.mark.parametrize("raw", [b'{"private-secret":', b'{"private-secret":0,"private-secret":1}',
                                 b'{"number":0.25}', b'"\xff"', b'[' * 65 + b']' * 65])
@pytest.mark.parametrize("target", ["context", "response"])
def test_invalid_json_is_bounded_and_redacted(offline_case, raw, target):
    context, response = (canonical_json_bytes(value) for value in offline_case)
    report, code = diagnostic_cli.diagnose(
        raw if target == "context" else context, raw if target == "response" else response,
    )
    assert code == 2
    assert report["status"] == "invalid_diagnostic_input"
    assert report["phase"] == f"{target}_json"
    assert report["error_code"] == "OFFLINE_DIAGNOSTIC_INPUT_REJECTED"
    assert "private-secret" not in json.dumps(report)


@pytest.mark.parametrize("mutation", ["graph_owner", "empty_candidates", "mixed_owners",
                                      "wrong_owner_type", "wrong_scope", "missing_grant"])
def test_missing_or_ambiguous_context_owner_is_unsupported(offline_case, mutation):
    context, response = offline_case
    candidates = context["candidate_catalog"]["payload"]["candidates"]
    if mutation == "graph_owner":
        del context["narrative_graph"]["member_ref"]
    elif mutation == "empty_candidates":
        candidates.clear()
    elif mutation == "mixed_owners":
        foreign = deepcopy(candidates[0])
        foreign["source_ref"]["member_ref"]["logical_id"] = "different-owner"
        candidates.append(foreign)
    elif mutation == "wrong_owner_type":
        for candidate in candidates:
            candidate["source_ref"]["member_ref"]["artifact_type"] = "narrative_graph"
    elif mutation == "wrong_scope":
        for candidate in candidates:
            candidate["source_ref"]["member_ref"]["scope"]["key"] = "foreign-scope"
    else:
        del context["source_grant"]["sources"]
    report, code = _diagnose(context, response)
    assert code == 2
    assert report["status"] == "unsupported_diagnostic_input"
    assert report["phase"] == "context_inputs"


def test_source_owner_is_derived_from_context_and_never_response(offline_case):
    context, response = offline_case
    for proposal in response["proposals"]:
        for requirement in proposal["material_requirements"]:
            for ref in requirement["source_constraints"]["allowed_source_refs"]:
                ref["member_ref"]["logical_id"] = "private-secret-response-owner"
    report, code = _diagnose(context, response)
    assert code == 1
    assert report["diagnostic"]["error_code"] == "SOURCE_REFERENCE_FOREIGN_OWNER"
    assert "private-secret-response-owner" not in json.dumps(report)


def test_context_binding_mismatch_cannot_report_pass(offline_case):
    context, response = offline_case
    response["input_binding_sha256"] = "sha256:" + "f" * 64
    report, code = _diagnose(context, response)
    assert code == 2
    assert report["status"] == "unsupported_diagnostic_input"


def test_cli_reads_files_and_prints_json_only(offline_case, tmp_path, capsys):
    context_path, raw_path = tmp_path / "context.json", tmp_path / "raw.json"
    context_path.write_bytes(canonical_json_bytes(offline_case[0]))
    raw_path.write_bytes(canonical_json_bytes(offline_case[1]))
    assert diagnostic_cli.main(["--context", str(context_path), "--response", str(raw_path)]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out)["status"] == "proposal_rules_passed"


def test_cli_rejects_oversized_file_without_echoing_file_path(tmp_path, capsys, monkeypatch):
    path = tmp_path / "private-secret.json"
    path.write_bytes(b" " * 9)
    monkeypatch.setattr(diagnostic_cli, "MAX_FILE_BYTES", 8)
    assert diagnostic_cli.main(["--context", str(path), "--response", str(path)]) == 2
    captured = capsys.readouterr()
    assert "private-secret" not in captured.out
    assert json.loads(captured.out)["phase"] == "file_read"
