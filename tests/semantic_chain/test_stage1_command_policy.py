"""Input-free policy values and synthetic request compatibility, not authority."""

import hashlib
import json
from dataclasses import FrozenInstanceError, fields, replace
from unittest.mock import Mock

import pytest
from autocut_kernel.pipeline import build_narrative_graph_request as module
from autocut_kernel.pipeline.build_narrative_graph_request import (
    BuildNarrativeGraphRequest,
    Stage1CommandPolicy,
    Stage1GenerationPolicy,
)
from autocut_kernel.semantic_chain.coverage_analysis import Stage1CoveragePolicy
from autocut_kernel.semantic_chain.dependency_projection import DependencyProjectionPolicy
from autocut_kernel.vlm.retry_policy import GenerationRetryPolicy

from tests.semantic_chain.test_stage1_draft import POLICY
from tests.semantic_chain.test_stage1_generation_request import _request


def _policy():
    # Construct policy alone: no Job, Source, VLM ref or committed-input fixture.
    return Stage1CommandPolicy(
        1,
        Stage1GenerationPolicy(
            "doubao-ark-text-responses-stream", "synthetic-model", "synthetic-prompt-v1",
            "构造跨窗口草稿。", "doubao-ark-text-responses-stream-v1", 1024, "0.5",
        ),
        POLICY, Stage1CoveragePolicy("0.5", "strict_global"),
        DependencyProjectionPolicy("semantic-dependencies-v1"),
        GenerationRetryPolicy("generation-retry-v1", 3, (2, 8)),
    )


def test_input_free_policy_round_trip_uses_independent_canonical_hash_oracle():
    policy = _policy()
    wire = policy.to_mapping()
    assert tuple(wire) == tuple(field.name for field in fields(Stage1CommandPolicy)) == (
        "artifact_revision", "generation", "draft_policy", "coverage_policy", "dependency_policy", "retry_policy",
    )
    assert Stage1CommandPolicy.from_mapping(json.loads(json.dumps(wire))) == policy
    raw = json.dumps(wire, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    assert policy.canonical_hash == "sha256:" + hashlib.sha256(raw).hexdigest()
    assert not hasattr(policy, "inputs") and not hasattr(policy, "job")
    assert not hasattr(policy, "accepted") and not hasattr(policy, "authorize")


def test_policy_validation_never_constructs_placeholder_input_objects(monkeypatch):
    policy = _policy()
    forbidden = Mock(side_effect=AssertionError("policy construction reached an input/reference owner"))
    for name in ("Job", "CommittedSemanticInputsRequest", "CommittedArtifactMemberReference", "BuildNarrativeGraphRequest"):
        monkeypatch.setattr(module, name, forbidden)
    assert Stage1CommandPolicy.from_mapping(policy.to_mapping()) == policy
    assert _policy() == policy
    forbidden.assert_not_called()


def test_frozen_policy_and_mapping_do_not_share_mutable_children():
    policy = _policy()
    with pytest.raises(FrozenInstanceError):
        policy.artifact_revision = 2
    with pytest.raises(FrozenInstanceError):
        policy.generation.model_id = "changed"
    wire = policy.to_mapping()
    wire["generation"]["prompt_template"] = "changed"
    wire["retry_policy"]["backoff_seconds"].clear()
    wire["dependency_policy"]["attribute_projections"].clear()
    assert policy == _policy()
    decoded_wire = _policy().to_mapping()
    decoded = Stage1CommandPolicy.from_mapping(decoded_wire)
    decoded_wire["retry_policy"]["backoff_seconds"].clear()
    assert decoded.retry_policy.backoff_seconds == (2, 8)


@pytest.mark.parametrize("name", [field.name for field in fields(Stage1CommandPolicy)])
@pytest.mark.parametrize("kind", ["missing", "unknown"])
def test_policy_root_is_exactly_closed(name, kind):
    wire = _policy().to_mapping()
    value = wire.pop(name)
    if kind == "unknown":
        wire["unknown_" + name] = value
    with pytest.raises(ValueError):
        Stage1CommandPolicy.from_mapping(wire)


@pytest.mark.parametrize("name", ["generation", "draft_policy", "coverage_policy", "dependency_policy", "retry_policy"])
@pytest.mark.parametrize("kind", ["missing", "unknown"])
def test_each_nested_policy_is_closed_by_one_owner(name, kind):
    wire = _policy().to_mapping()
    if kind == "unknown":
        wire[name]["unknown"] = True
    else:
        wire[name].pop(next(iter(wire[name])))
    with pytest.raises(ValueError):
        Stage1CommandPolicy.from_mapping(wire)


@pytest.mark.parametrize("value", [False, True, 1.0, 0, -1, 2**53, "1", None])
def test_revision_requires_actual_positive_safe_integer(value):
    with pytest.raises(ValueError):
        replace(_policy(), artifact_revision=value)
    wire = _policy().to_mapping()
    wire["artifact_revision"] = value
    with pytest.raises(ValueError):
        Stage1CommandPolicy.from_mapping(wire)


@pytest.mark.parametrize("name", ["generation", "draft_policy", "coverage_policy", "dependency_policy", "retry_policy"])
@pytest.mark.parametrize("value", [None, {}, [], True])
def test_direct_construction_requires_exact_typed_policy_components(name, value):
    with pytest.raises(ValueError):
        replace(_policy(), **{name: value})


@pytest.mark.parametrize("path,value", [
    (("generation", "max_output_tokens"), True),
    (("generation", "max_output_tokens"), 1.0),
    (("generation", "temperature"), 0.5),
    (("generation", "provider_id"), "foreign"),
    (("generation", "prompt_template"), "\ud800"),
    (("draft_policy", "max_prompt_bytes"), True),
    (("draft_policy", "max_prompt_bytes"), 1.5),
    (("coverage_policy", "minimum_confidence"), 0.5),
    (("coverage_policy", "coverage_mode"), "dependency_scoped"),
    (("retry_policy", "max_attempts"), True),
    (("retry_policy", "backoff_seconds"), (2, 8)),
    (("retry_policy", "backoff_seconds"), [True, 8]),
    (("retry_policy", "backoff_seconds"), [2.0, 8]),
])
def test_wire_primitives_are_not_coerced(path, value):
    wire = _policy().to_mapping()
    wire[path[0]][path[1]] = value
    with pytest.raises(ValueError):
        Stage1CommandPolicy.from_mapping(wire)


@pytest.mark.parametrize("change", ["revision", "generation", "draft", "coverage", "retry"])
def test_every_variable_policy_component_is_hash_bound(change):
    policy = _policy()
    changes = {
        "revision": {"artifact_revision": 2},
        "generation": {"generation": replace(policy.generation, model_id="changed-model")},
        "draft": {"draft_policy": replace(policy.draft_policy, max_prompt_bytes=policy.draft_policy.max_prompt_bytes + 1)},
        "coverage": {"coverage_policy": Stage1CoveragePolicy("0.6", "strict_global")},
        "retry": {"retry_policy": GenerationRetryPolicy("generation-retry-v1", 3, (3, 8))},
    }
    assert replace(policy, **changes[change]).canonical_hash != policy.canonical_hash


def test_build_request_preserves_exact_input_objects_and_legacy_fields():
    _inputs, request = _request()
    policy = request.command_policy
    rebuilt = policy.build_request(request.inputs, request.idempotency_key)
    assert type(rebuilt) is BuildNarrativeGraphRequest and rebuilt == request
    assert rebuilt.inputs is request.inputs
    assert rebuilt.command_policy == policy
    assert rebuilt.to_mapping() == request.to_mapping()
    assert BuildNarrativeGraphRequest.from_mapping(rebuilt.to_mapping()) == request


@pytest.mark.parametrize("inputs,idempotency_key", [(None, "key"), ({}, "key"), (False, "key")])
def test_build_request_does_not_replace_absent_inputs(inputs, idempotency_key):
    with pytest.raises(ValueError):
        _policy().build_request(inputs, idempotency_key)


@pytest.mark.parametrize("value", ["", " ", None, True, "\ud800"])
def test_build_request_preserves_idempotency_key_validation(value):
    _inputs, request = _request()
    with pytest.raises(ValueError):
        request.command_policy.build_request(request.inputs, value)


def test_legacy_request_decoder_delegates_policy_without_an_alternate_parser(monkeypatch):
    _inputs, request = _request()
    wire = request.to_mapping()
    decoder = Mock(side_effect=ValueError("shared policy decoder called"))
    monkeypatch.setattr(Stage1CommandPolicy, "from_mapping", decoder)
    with pytest.raises(ValueError, match="shared policy decoder called"):
        BuildNarrativeGraphRequest.from_mapping(wire)
    decoder.assert_called_once_with(request.command_policy.to_mapping())
