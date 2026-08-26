"""Synthetic source grammar, never accepted calibration or deployed authority."""

from __future__ import annotations

import copy
import json
from dataclasses import FrozenInstanceError

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_hash
from autocut_kernel.registry.authority_profiles import (
    AuthorityProfileSourceError,
    decode_stage1_narrative_profile_source,
)
from autocut_kernel.registry.installed_local_run import compute_local_profile_registry_sha256
from jsonschema import Draft202012Validator

from tests.authority.test_authority_profile_sources import (
    REPO_ROOT,
    _decoded_dependencies,
    _hash,
    _narrative_mapping,
    _profiles,
    _raw,
    decode_local_run_profile_source,
    decode_shadow_calibration_profile_source,
    synthetic_stage1_command_policy,
)
from tests.pipeline.installed_profile_fixture import synthetic_installed_resource


def _validator():
    raw = (REPO_ROOT / "governance/schemas/stage1-narrative-profile.schema.json").read_bytes()
    schema = json.loads(raw)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _rehash(source):
    source["policies"]["stage1_command_policy_sha256"] = canonical_json_hash(source["stage1_command_policy"])


def test_source_retains_exact_typed_input_free_policy_and_fresh_mapping():
    mapping = _narrative_mapping()
    source = decode_stage1_narrative_profile_source(_raw(mapping))
    policy = synthetic_stage1_command_policy()
    assert source.command_policy == policy
    assert source.reference.stage1_command_policy_sha256 == policy.canonical_hash
    assert source.to_mapping() == mapping
    assert set(mapping["stage1_command_policy"]) == {
        "artifact_revision", "generation", "draft_policy", "coverage_policy", "dependency_policy", "retry_policy",
    }
    with pytest.raises(FrozenInstanceError):
        source.command_policy.artifact_revision = 2
    projected = source.to_mapping()
    projected["stage1_command_policy"]["generation"]["prompt_template"] = "changed"
    projected["stage1_command_policy"]["retry_policy"]["backoff_seconds"].clear()
    assert source.to_mapping() == mapping
    assert not hasattr(source, "accepted")


@pytest.mark.parametrize("kind", ["narrative", "shadow", "local_run"])
def test_no_v1_source_fallback(kind):
    narrative, shadow, local_run = _profiles()
    narrative_source, shadow_source = _decoded_dependencies(narrative, shadow)
    source = {"narrative": narrative, "shadow": shadow, "local_run": local_run}[kind]
    source["schema_version"] = source["schema_version"].replace("-v2", "-v1")
    with pytest.raises(AuthorityProfileSourceError):
        if kind == "narrative":
            decode_stage1_narrative_profile_source(_raw(source))
        elif kind == "shadow":
            decode_shadow_calibration_profile_source(_raw(source), narrative=narrative_source)
        else:
            decode_local_run_profile_source(_raw(source), narrative=narrative_source, shadow=shadow_source)


_POLICY_OBJECT_PATHS = (
    (), ("generation",), ("draft_policy",), ("coverage_policy",), ("dependency_policy",),
    ("dependency_policy", "canonical_owner_by_object_type"),
    ("dependency_policy", "edge_projections"), ("retry_policy",),
)


@pytest.mark.parametrize("path", _POLICY_OBJECT_PATHS)
@pytest.mark.parametrize("mutation", ["missing", "extra"])
def test_policy_and_every_nested_object_have_closed_schema(path, mutation):
    source = _narrative_mapping()
    target = source["stage1_command_policy"]
    for key in path:
        target = target[key]
    if mutation == "missing":
        target.pop(next(iter(target)))
    else:
        target["caller_accepted"] = True
    _rehash(source)
    assert not _validator().is_valid(source)
    with pytest.raises(AuthorityProfileSourceError):
        decode_stage1_narrative_profile_source(_raw(source))


@pytest.mark.parametrize("mutation", ["missing_policy", "missing_hash", "wrong_hash", "old_slots"])
def test_full_policy_and_matching_hash_are_required_without_old_slot_defaults(mutation):
    source = _narrative_mapping()
    if mutation == "missing_policy":
        source.pop("stage1_command_policy")
    elif mutation == "missing_hash":
        source["policies"].pop("stage1_command_policy_sha256")
    elif mutation == "wrong_hash":
        source["policies"]["stage1_command_policy_sha256"] = _hash("other-policy")
    else:
        source["policies"].pop("stage1_command_policy_sha256")
        source["policies"].update({f"{name}_policy_sha256": _hash(name) for name in ("coverage", "dependency", "conflict")})
    with pytest.raises(AuthorityProfileSourceError):
        decode_stage1_narrative_profile_source(_raw(source))


@pytest.mark.parametrize(("path", "value"), [
    (("artifact_revision",), True), (("artifact_revision",), 0),
    (("artifact_revision",), 1.5), (("artifact_revision",), 2**53),
    (("generation", "provider_id"), "doubao-ark-responses-stream"),
    (("generation", "model_id"), "different-model"),
    (("generation", "adapter_strategy_version"), "other-v1"),
    (("generation", "prompt_template"), "   "),
    (("generation", "max_output_tokens"), 32769),
    (("generation", "temperature"), "1.0"),
    (("generation", "temperature"), "NaN"),
    (("generation", "temperature"), "2.1"),
    (("draft_policy", "max_response_bytes"), 0),
    (("draft_policy", "max_prompt_bytes"), False),
    (("coverage_policy", "minimum_confidence"), "0.80"),
    (("coverage_policy", "minimum_confidence"), "1.1"),
    (("coverage_policy", "coverage_mode"), "local_only"),
    (("dependency_policy", "canonical_owner_by_object_type", "event"), "narrative_graph"),
    (("dependency_policy", "edge_projections", "requires"), "from_to"),
    (("dependency_policy", "attribute_projections"), []),
    (("dependency_policy", "external_root_projections"), []),
    (("retry_policy", "strategy_version"), "other-v1"),
    (("retry_policy", "max_attempts"), 4),
    (("retry_policy", "backoff_seconds"), [2]),
    (("retry_policy", "backoff_seconds"), [-1, 8]),
    (("retry_policy", "backoff_seconds"), [True, 8]),
])
def test_rehashed_invalid_policy_still_fails_value_decoder_and_schema(path, value):
    source = _narrative_mapping()
    target = source["stage1_command_policy"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    if value != 2**53 and value != 1.5:
        _rehash(source)
    assert not _validator().is_valid(source)
    with pytest.raises(AuthorityProfileSourceError):
        decode_stage1_narrative_profile_source(_raw(source))


@pytest.mark.parametrize("raw_number", [b"1.0", b"NaN", b"Infinity"])
def test_json_numeric_subset_and_duplicate_keys_apply_inside_full_policy(raw_number):
    raw = _raw(_narrative_mapping()).replace(b'"artifact_revision":1', b'"artifact_revision":' + raw_number)
    with pytest.raises(AuthorityProfileSourceError):
        decode_stage1_narrative_profile_source(raw)
    duplicate = _raw(_narrative_mapping()).replace(b'"artifact_revision":1', b'"artifact_revision":1,"artifact_revision":1')
    with pytest.raises(AuthorityProfileSourceError):
        decode_stage1_narrative_profile_source(duplicate)


@pytest.mark.parametrize("part", ["artifact_revision", "generation", "draft_policy", "coverage_policy", "retry_policy"])
def test_policy_change_requires_new_source_registry_and_nested_reference_identity(part):
    original, shadow, _run = _profiles()
    changed = copy.deepcopy(original)
    policy = changed["stage1_command_policy"]
    if part == "artifact_revision":
        policy[part] = 2
    elif part == "generation":
        policy[part]["prompt_template"] += " Changed synthetic prompt."
    elif part == "draft_policy":
        policy[part]["max_input_objects"] += 1
    elif part == "coverage_policy":
        policy[part]["minimum_confidence"] = "0.9"
    else:
        policy[part]["backoff_seconds"] = [3, 9]
    _rehash(changed)
    _validator().validate(changed)
    before = decode_stage1_narrative_profile_source(_raw(original))
    after = decode_stage1_narrative_profile_source(_raw(changed))
    assert before.source_sha256 != after.source_sha256
    assert before.canonical_sha256 != after.canonical_sha256
    assert before.reference.stage1_command_policy_sha256 != after.reference.stage1_command_policy_sha256
    schema = (REPO_ROOT / "governance/schemas/shadow-calibration-profile.schema.json").read_bytes()
    hashes = [compute_local_profile_registry_sha256(
        profile_kind="shadow_calibration_v1", narrative_raw=_raw(source), profile_raw=_raw(shadow), schema_raw=schema,
    ) for source in (original, changed)]
    assert hashes[0] != hashes[1]
    with pytest.raises(AuthorityProfileSourceError):
        decode_shadow_calibration_profile_source(_raw(shadow), narrative=after)


def test_synthetic_installed_fixture_exposes_real_typed_policy_not_process_default():
    policy = synthetic_stage1_command_policy()
    resource = synthetic_installed_resource(command_policy=policy)
    assert resource.narrative.command_policy == policy
    assert resource.local_run.stage1_narrative_profile.stage1_command_policy_sha256 == policy.canonical_hash
    assert resource.shadow.stage1_narrative_profile.stage1_command_policy_sha256 == policy.canonical_hash
