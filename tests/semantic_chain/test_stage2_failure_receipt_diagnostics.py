"""Synthetic regressions using real compilers; no live model or DB claims."""

import json
from uuid import UUID

from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes, sha256_bytes
from autocut_kernel.pipeline import compile_story_portfolio_command as command_module
from autocut_kernel.pipeline.compile_story_portfolio_command import CompileStoryPortfolioCommand
from autocut_kernel.pipeline.story_design_diagnostics import story_design_failure_detail
from autocut_kernel.semantic_chain.story_design_validation import StoryProposalValidationError

from tests.semantic_chain.test_compile_story_portfolio_command import command_case


def test_wrong_person_type_survives_material_wrapper_and_durable_denial():
    store, _, _, raw = command_case()
    graph_member = next(m for m in store.predecessor.record.artifacts if m.artifact_type == "narrative_graph")
    graph = json.loads(graph_member.payload_json)
    person = next(node for node in graph["nodes"] if node["node_type"] == "entity"
                  and node["attributes"]["entity_kind"] == "person")
    payload = json.loads(raw)
    graph_owner = payload["proposals"][0]["required_fact_refs"][0]["member_ref"]
    payload["proposals"][0]["key_character_refs"] = [{
        "member_ref": graph_owner, "object_type": "character", "object_id": person["node_id"],
    }]
    raw = canonical_json_bytes(payload)
    store, provider, request, _ = command_case(raw=raw)
    command = CompileStoryPortfolioCommand(store, provider)
    result = command.execute(request)
    assert result.outcome.state == "denied"
    assert result.outcome.failure_code == "STAGE2_DRAFT_OR_COMPILATION_REJECTED"
    detail = json.loads(result.outcome.failure_detail_json)["attempts"][-1]["failure_detail"]
    diagnostic = detail["diagnostic"]
    assert diagnostic["phase"] == "compilation"
    assert diagnostic["raw_response_sha256"] == sha256_bytes(raw)
    assert diagnostic["attempt_id"] == str(result.attempt.attempt_id)
    assert diagnostic["cause_types"] == ["MaterialSupportError", "StoryProposalValidationError"]
    validation = diagnostic["validation"]
    assert validation["error_code"] == "GRAPH_REFERENCE_TYPE_MISMATCH"
    assert validation["json_path"] == "$.proposals[0].key_character_refs[0]"
    assert validation["rule_id"] == "SD-REF-001"
    assert validation["expected_object_type"] == "character"
    assert validation["actual_object_type"] == "entity"
    assert diagnostic["retryability"] == "requires_diagnosis"
    assert store.record is None and not store.successes
    assert store.read_immutable_blob(request.job, result.attempt.raw_response) == raw
    assert command.execute(request).outcome == result.outcome
    assert len(provider.dispatches) == len(store.attempts) == 1


def test_independent_evaluation_failure_records_its_phase(monkeypatch):
    store, provider, request, raw = command_case()

    def rejected(*args, **kwargs):
        raise StoryProposalValidationError("SD-MAT-001", "private text", 1,
                                          json_path="$.proposals[1].required_fact_refs",
                                          error_code="REQUIRED_FACT_CLOSURE_MISMATCH")

    monkeypatch.setattr(command_module, "_evaluate", rejected)
    result = CompileStoryPortfolioCommand(store, provider).execute(request)
    detail = json.loads(result.outcome.failure_detail_json)["attempts"][-1]["failure_detail"]
    assert detail["diagnostic"]["phase"] == "independent_evaluation"
    assert detail["diagnostic"]["raw_response_sha256"] == sha256_bytes(raw)
    assert detail["diagnostic"]["validation"]["proposal_index"] == 1
    assert "private text" not in result.outcome.failure_detail_json
    assert store.record is None and len(provider.dispatches) == 1


def test_generic_exception_messages_and_raw_are_not_echoed():
    error = ValueError("Authorization: Bearer secret signed-url")
    error.__cause__ = KeyError("private source path")
    raw = b"private raw model content"
    detail = story_design_failure_detail(error, phase="compilation", raw=raw, attempt_id=UUID(int=1))
    encoded = json.dumps(detail)
    assert "secret" not in encoded and "private" not in encoded
    assert detail["diagnostic"]["validation"] is None
    assert detail["diagnostic"]["cause_types"] == ["ValueError", "KeyError"]


def test_cyclic_and_deep_causes_are_bounded():
    error = ValueError("cycle")
    error.__cause__ = error
    detail = story_design_failure_detail(error, phase="compilation", raw=b"{}", attempt_id=UUID(int=1))
    assert detail["diagnostic"]["cause_types"] == ["ValueError"]
    for _ in range(12):
        wrapped = ValueError("outer")
        wrapped.__cause__ = error
        error = wrapped
    detail = story_design_failure_detail(error, phase="compilation", raw=b"{}", attempt_id=UUID(int=1))
    assert len(detail["diagnostic"]["cause_types"]) == 8
