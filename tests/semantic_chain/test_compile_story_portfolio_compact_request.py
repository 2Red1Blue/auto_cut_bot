"""Versioned request-boundary checks; synthetic context, no provider calls."""

import json
from dataclasses import replace

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_hash
from autocut_kernel.pipeline.compile_story_portfolio_request import prepare_stage2_request
from autocut_kernel.semantic_chain.story_design_boundary import STAGE2_COMPACT_PROMPT
from autocut_kernel.semantic_chain.story_design_compact import COMPACT_PROMPT_VERSION

from tests.semantic_chain.test_compile_story_portfolio_request import request_case


def test_compact_provider_view_separates_private_owner_map_and_rebuilds():
    request, inputs, _, _ = request_case()
    legacy = prepare_stage2_request(request, inputs)
    compact_request = replace(request, generation=replace(
        request.generation, prompt_version=COMPACT_PROMPT_VERSION,
        prompt_template=STAGE2_COMPACT_PROMPT,
    ))
    prepared = prepare_stage2_request(compact_request, inputs)
    assert prepared == prepare_stage2_request(compact_request, inputs)
    assert prepare_stage2_request(request, inputs) == legacy
    assert prepared.input_binding_sha256 == legacy.input_binding_sha256
    assert prepared.request_hash != legacy.request_hash
    body = json.loads(prepared.provider_payload)
    text = body["input"][0]["content"][0]["text"]
    assert text.startswith(STAGE2_COMPACT_PROMPT + "\n\n")
    view = text.split("\n\n", 1)[1]
    assert '"member_ref"' not in view and "sha256:" not in view
    envelope = json.loads(prepared.request_payload)
    assert envelope["schema_version"] == "stage2-generation-request-v2"
    identity = envelope["model_boundary"]
    assert identity["reference_map_sha256"] == canonical_json_hash(identity["reference_map"])
    assert identity["reference_map"]["input_binding_sha256"] == prepared.input_binding_sha256
    assert "model_boundary" not in json.loads(legacy.request_payload)
    assert identity["implementation_sha256"].startswith("sha256:")


def test_unknown_compact_version_cannot_fall_back_to_v1():
    request, inputs, _, _ = request_case()
    request = replace(request, generation=replace(
        request.generation, prompt_version="stage2-proposal-compact-v999",
    ))
    with pytest.raises(ValueError, match="IMPLEMENTATION_UNAVAILABLE"):
        prepare_stage2_request(request, inputs)


def test_compact_complete_request_honors_explicit_budget():
    request, inputs, _, _ = request_case()
    request = replace(request, generation=replace(
        request.generation, prompt_version=COMPACT_PROMPT_VERSION,
        prompt_template=STAGE2_COMPACT_PROMPT,
    ))
    prepared = prepare_stage2_request(request, inputs)
    with pytest.raises(ValueError, match="byte budget"):
        prepare_stage2_request(replace(request, max_prompt_bytes=len(prepared.provider_payload) - 1), inputs)
