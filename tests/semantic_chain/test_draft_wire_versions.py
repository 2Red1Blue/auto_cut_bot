"""Versioned Ark wire changes preserve semantic input and historical replay."""

import json
from dataclasses import replace

import pytest
from autocut_kernel.pipeline.build_editorial_blueprint_request import prepare_stage3_request
from autocut_kernel.pipeline.build_narrative_graph_request import prepare_stage1_request
from autocut_kernel.pipeline.compile_story_portfolio_request import prepare_stage2_request
from autocut_kernel.semantic_chain.draft_provider import (
    DRAFT_DIRECT_SCHEMA_ADAPTER_STRATEGY_VERSION as V2,
)
from autocut_kernel.semantic_chain.draft_provider import (
    DRAFT_LEGACY_ADAPTER_STRATEGY_VERSION as V1,
)
from autocut_kernel.semantic_chain.draft_provider import (
    DraftProviderError,
    build_draft_text_format,
    decode_draft_text_format,
)

from tests.semantic_chain.test_build_editorial_blueprint_request import stage3_case
from tests.semantic_chain.test_compile_story_portfolio_request import request_case
from tests.semantic_chain.test_draft_provider import body, encoded
from tests.semantic_chain.test_stage1_generation_request import _request


@pytest.mark.parametrize("stage", [1, 2, 3])
def test_all_compilers_version_wire_without_changing_input_or_schema(stage):
    if stage == 1:
        inputs, request = _request()
        prepare = prepare_stage1_request
    elif stage == 2:
        request, inputs, _, _ = request_case()
        prepare = prepare_stage2_request
    else:
        inputs, request = stage3_case()
        prepare = prepare_stage3_request
    old = prepare(request, inputs)
    new_request = replace(request, generation=replace(request.generation, adapter_strategy_version=V2))
    new = prepare(new_request, inputs)
    old_body, new_body = json.loads(old.provider_payload), json.loads(new.provider_payload)
    descriptor = old_body["text"]["format"]["json_schema"]
    assert new_body["text"] == {"format": {"type": "json_schema", **descriptor}}
    new_body["text"] = old_body["text"]
    assert encoded(new_body) == old.provider_payload
    assert old.input_binding_sha256 == new.input_binding_sha256
    assert old.request_hash != new.request_hash
    assert old.provider_idempotency_key_for(1) != new.provider_idempotency_key_for(1)
    restored = type(request).from_mapping(request.to_mapping())
    assert prepare(restored, inputs).request_payload == old.request_payload


@pytest.mark.parametrize("version", [V1, V2])
def test_shared_builder_validates_closed_shape_and_version(version):
    legacy = body()["text"]
    descriptor = legacy["format"]["json_schema"]
    text = build_draft_text_format(version, descriptor["name"], descriptor["schema"])
    assert decode_draft_text_format(text, adapter_strategy_version=version) == descriptor
    if version == V1:
        assert encoded(text) == encoded(legacy)
    with pytest.raises(DraftProviderError):
        decode_draft_text_format(text, adapter_strategy_version=V2 if version == V1 else V1)
    text["format"]["unexpected"] = True
    with pytest.raises(DraftProviderError):
        decode_draft_text_format(text)


@pytest.mark.parametrize("field,value", [("strict", False), ("name", "bad name"), ("schema", {})])
def test_direct_shape_retains_field_validation(field, value):
    descriptor = body()["text"]["format"]["json_schema"]
    descriptor[field] = value
    with pytest.raises(DraftProviderError):
        decode_draft_text_format({"format": {"type": "json_schema", **descriptor}})


def test_mixed_shape_and_unknown_version_rejected():
    text = body()["text"]
    text["format"].update(text["format"]["json_schema"])
    with pytest.raises(DraftProviderError):
        decode_draft_text_format(text)
    with pytest.raises(DraftProviderError):
        build_draft_text_format("unknown", "draft", {"type": "object"})
