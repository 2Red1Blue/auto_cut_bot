"""Synthetic source/accepted-reader fixtures; no Git, DB or model acceptance."""

from __future__ import annotations

import base64
import copy
import json
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes, canonical_json_hash
from autocut_kernel.registry.authority_profiles import AuthorityProfileSourceError
from autocut_kernel.registry.calibration_binding import bind_profile_calibration
from autocut_kernel.registry.installed_local_run import LocalRunResourceError
from autocut_kernel.registry.installed_runtime import InstalledLocalRunProfileResolver
from autocut_kernel.registry.timed_speech import (
    AUTHORITY_BOOTSTRAP_JOB,
    BOOTSTRAP_TIMED_SPEECH_PROFILE_REGISTRY_COMMAND,
    VerifiedTimedSpeechAuthorityContext,
)
from autocut_kernel.registry.timed_speech_contract import timed_speech_registry_contract_sha256
from autocut_kernel.semantic_chain.story_design_command_policy import Stage2CommandPolicy
from autocut_kernel.store.errors import IdempotencyConflictError
from autocut_kernel.store.models import CommandOutcome, CommandSuccess, artifact_set_hash
from jsonschema import Draft202012Validator

from tests.authority.test_authority_profile_sources import (
    REPO_ROOT,
    _decoded_dependencies,
    _hash,
    _profiles,
    _raw,
    decode_local_run_profile_source,
    synthetic_stage2_command_policy,
)
from tests.authority.test_installed_local_run import (
    _decode,
    _encoded,
    _rehash_chain,
    _resource_mapping,
)
from tests.authority.test_local_run_calibration import (
    FakeAcceptedAnchorReader,
    _fixture_anchor,
    _fixture_record,
    _project,
)
from tests.pipeline.installed_profile_fixture import synthetic_installed_resource
from tests.store.test_command_execution_kind_lifecycle import _store


def _validator():
    schema = json.loads((REPO_ROOT / "governance/schemas/local-run-profile.schema.json").read_bytes())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _decode_source(source):
    narrative, shadow, _ = _profiles()
    narrative, shadow = _decoded_dependencies(narrative, shadow)
    return decode_local_run_profile_source(_raw(source), narrative=narrative, shadow=shadow)


def _rehash(source):
    source["stage2_command_policy_sha256"] = canonical_json_hash(source["stage2_command_policy"])


def _change_resource(wire, change):
    wire = copy.deepcopy(wire)
    local = json.loads(base64.b64decode(wire["current"]["profile_raw_base64"]))
    change(local)
    wire["current"]["profile_raw_base64"] = _encoded(_raw(local))
    _rehash_chain(wire["current"], "local_run_v1")
    return wire


def test_current_source_has_exact_frozen_stage2_policy_and_unchanged_v2_dependencies():
    narrative, shadow, source = _profiles()
    assert narrative["schema_version"] == "autocut-stage1-narrative-profile-v2"
    assert shadow["schema_version"] == "autocut-shadow-calibration-profile-v2"
    assert source["schema_version"] == "autocut-local-run-profile-v4"
    assert source["profile_state"] == "local_run_v1"
    _validator().validate(source)
    decoded = _decode_source(source)
    assert type(decoded.stage2_command_policy) is Stage2CommandPolicy
    assert decoded.stage2_command_policy == synthetic_stage2_command_policy()
    assert decoded.stage2_command_policy_sha256 == canonical_json_hash(source["stage2_command_policy"])
    assert decoded.to_mapping() == source
    with pytest.raises(FrozenInstanceError):
        decoded.stage2_command_policy = None
    fresh = decoded.to_mapping()
    fresh["stage2_command_policy"]["generation"]["prompt_template"] = "changed"
    fresh["stage2_command_policy"]["retry_policy"]["backoff_seconds"].clear()
    assert decoded.to_mapping() == source


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_old_local_source_never_receives_an_implicit_stage2_policy(version):
    _, _, source = _profiles()
    source["schema_version"] = "autocut-local-run-profile-" + version
    source.pop("stage2_command_policy")
    source.pop("stage2_command_policy_sha256")
    with pytest.raises(AuthorityProfileSourceError):
        _decode_source(source)
    assert not _validator().is_valid(source)


@pytest.mark.parametrize("field", ["stage2_command_policy", "stage2_command_policy_sha256"])
@pytest.mark.parametrize("mutation", ["missing", "null", "wrong_type"])
def test_policy_and_explicit_hash_are_required(field, mutation):
    _, _, source = _profiles()
    if mutation == "missing":
        source.pop(field)
    else:
        source[field] = None if mutation == "null" else []
    assert not _validator().is_valid(source)
    with pytest.raises(AuthorityProfileSourceError):
        _decode_source(source)


@pytest.mark.parametrize("digest", [_hash("wrong-stage2"), "sha256:" + "0" * 64, "sha256:" + "A" * 64])
def test_policy_hash_cannot_be_substituted(digest):
    _, _, source = _profiles()
    source["stage2_command_policy_sha256"] = digest
    with pytest.raises(AuthorityProfileSourceError):
        _decode_source(source)


_OBJECTS = [
    (), ("generation",), ("draft_policy",), ("candidate_policy",), ("job_policy",),
    ("job_policy", "proposal_count"), ("job_policy", "target_duration_seconds"),
    ("job_policy", "source_constraints"), ("story_policy",),
    ("story_policy", "editing_profiles", 0), ("story_policy", "required_physical_requirements", 0),
    ("retry_policy",),
]


@pytest.mark.parametrize("path", _OBJECTS)
@pytest.mark.parametrize("mutation", ["missing", "extra"])
def test_all_nested_policy_objects_are_closed_even_when_rehashed(path, mutation):
    _, _, source = _profiles()
    item = source["stage2_command_policy"]
    for key in path:
        item = item[key]
    if mutation == "missing":
        item.pop(next(iter(item)))
    else:
        item["accepted"] = True
    _rehash(source)
    assert not _validator().is_valid(source)
    with pytest.raises(AuthorityProfileSourceError):
        _decode_source(source)


@pytest.mark.parametrize(("path", "value"), [
    (("artifact_revision",), True), (("artifact_revision",), 0), (("artifact_revision",), 2**53),
    (("max_prompt_bytes",), False), (("max_prompt_bytes",), 16777217),
    (("generation", "provider_id"), "foreign"), (("generation", "model_id"), "foreign"),
    (("generation", "adapter_strategy_version"), "foreign"),
    (("generation", "prompt_template"), "   "), (("generation", "prompt_version"), ""),
    (("generation", "max_output_tokens"), 32769), (("generation", "temperature"), "1.0"),
    (("generation", "temperature"), "NaN"), (("generation", "temperature"), "2.1"),
    (("draft_policy", "max_response_bytes"), 16777217), (("draft_policy", "max_json_depth"), 65),
    (("draft_policy", "max_proposals"), 257), (("draft_policy", "max_total_references"), 8193),
    (("candidate_policy", "minimum_confidence"), "0.80"),
    (("candidate_policy", "required_measurement_kinds"), ["foreign"]),
    (("job_policy", "source_reuse_policy"), "sometimes"),
    (("job_policy", "source_constraints", "authorization_purpose"), "semantic_analysis"),
    (("job_policy", "completion_policy"), "partial"), (("job_policy", "max_search_states"), 0),
    (("story_policy", "selection_strategy"), "top_k"), (("story_policy", "editing_profiles"), []),
    (("story_policy", "required_physical_requirements", 0, "mode"), "complete"),
    (("retry_policy", "max_attempts"), 4), (("retry_policy", "backoff_seconds"), [2]),
    (("retry_policy", "backoff_seconds"), [True, 8]),
])
def test_rehashed_invalid_values_fail_schema_and_typed_decoder(path, value):
    _, _, source = _profiles()
    item = source["stage2_command_policy"]
    for key in path[:-1]:
        item = item[key]
    item[path[-1]] = value
    if value != 2**53:
        _rehash(source)
    assert not _validator().is_valid(source)
    with pytest.raises(AuthorityProfileSourceError):
        _decode_source(source)


@pytest.mark.parametrize("mutation", ["story_hash", "range", "selected_count", "text_budget", "measurement_order"])
def test_schema_valid_cross_field_policy_drift_still_fails_decoder(mutation):
    _, _, source = _profiles()
    policy = source["stage2_command_policy"]
    if mutation == "story_hash":
        policy["job_policy"]["story_design_policy_sha256"] = _hash("foreign-story")
    elif mutation == "range":
        policy["job_policy"]["proposal_count"] = {"min": 10, "max": 1}
    elif mutation == "selected_count":
        policy["job_policy"]["selected_story_count"] = 17
    elif mutation == "text_budget":
        policy["draft_policy"]["max_total_text_characters"] = 10
    else:
        policy["candidate_policy"]["required_measurement_kinds"] = ["reveal_strength", "hook_strength"]
    _rehash(source)
    _validator().validate(source)
    with pytest.raises(AuthorityProfileSourceError):
        _decode_source(source)


@pytest.mark.parametrize("number", [b"1.0", b"NaN", b"Infinity", b"true", b"1,\"artifact_revision\":1"])
def test_raw_nested_json_is_strict(number):
    narrative, shadow, source = _profiles()
    narrative, shadow = _decoded_dependencies(narrative, shadow)
    raw = _raw(source).replace(b'"artifact_revision":1', b'"artifact_revision":' + number)
    with pytest.raises(AuthorityProfileSourceError):
        decode_local_run_profile_source(raw, narrative=narrative, shadow=shadow)


@pytest.mark.parametrize("part", ["artifact_revision", "generation", "max_prompt_bytes", "draft_policy", "candidate_policy", "job_policy", "story_policy", "retry_policy"])
def test_each_stage2_policy_component_changes_only_current_source_registry_identity(part):
    original = _resource_mapping()
    before = _decode(original)

    def mutate(source):
        policy = source["stage2_command_policy"]
        if part in ("artifact_revision", "max_prompt_bytes"):
            policy[part] += 1
        elif part == "generation":
            policy[part]["prompt_template"] += " 中文补充。"
        elif part == "draft_policy":
            policy[part]["max_proposals"] += 1
        elif part == "candidate_policy":
            policy[part]["minimum_confidence"] = "0.9"
        elif part == "job_policy":
            policy[part]["max_search_states"] += 1
        elif part == "story_policy":
            policy[part]["teaser_strategies"] += ["suspense"]
            policy["job_policy"]["story_design_policy_sha256"] = canonical_json_hash(policy[part])
        else:
            policy[part]["backoff_seconds"] = [3, 9]
        _rehash(source)

    changed = _change_resource(original, mutate)
    after = _decode(changed)
    assert after.local_run.stage2_command_policy_sha256 != before.local_run.stage2_command_policy_sha256
    assert after.current_registry_sha256 != before.current_registry_sha256
    assert after.local_run.source_sha256 != before.local_run.source_sha256
    assert changed["predecessor"] == original["predecessor"]
    assert after.narrative == before.narrative and after.shadow == before.shadow
    assert after.local_run.calibration == before.local_run.calibration
    assert after.local_run.timed_speech_registry_entry == before.local_run.timed_speech_registry_entry
    assert _decode(changed) == after


def test_installed_transport_rehash_does_not_hide_a_stale_policy_hash():
    changed = _change_resource(_resource_mapping(), lambda source: source["stage2_command_policy"].update(max_prompt_bytes=100))
    with pytest.raises(LocalRunResourceError):
        _decode(changed)


def test_stage2_schema_addition_does_not_change_timed_speech_closure():
    schema = json.loads((REPO_ROOT / "governance/schemas/local-run-profile.schema.json").read_bytes())
    current = timed_speech_registry_contract_sha256(canonical_json_bytes(schema))
    schema["$id"] = "schema://governance/local-run-profile/2.0.0"
    schema["properties"]["schema_version"]["const"] = "autocut-local-run-profile-v2"
    for field in ("stage2_command_policy", "stage2_command_policy_sha256"):
        schema["required"].remove(field)
        del schema["properties"][field]
    schema["$defs"] = {key: value for key, value in schema["$defs"].items() if not key.startswith("stage2_")}
    assert timed_speech_registry_contract_sha256(canonical_json_bytes(schema)) == current


def test_installed_synthetic_fixture_carries_explicit_typed_policy():
    policy = replace(synthetic_stage2_command_policy(), max_prompt_bytes=1_000_000)
    resource = synthetic_installed_resource(stage2_command_policy=policy)
    assert resource.local_run.stage2_command_policy == policy
    assert resource.local_run.stage2_command_policy_sha256 == policy.canonical_hash


def test_new_version_reuses_identical_fake_accepted_calibration_without_measurement():
    wire = _resource_mapping()
    resource = _decode(wire)
    # Test-only shape supplies facts to the existing synthetic record fixture;
    # this does not claim that any Git chain or real Store accepted the record.
    context = SimpleNamespace(profiles=SimpleNamespace(shadow=resource.shadow),
                              compilation=SimpleNamespace(registry_sha256=resource.predecessor_registry_sha256))
    anchor = _fixture_anchor(_fixture_record(context))
    wire = _change_resource(wire, lambda source: _project(source, anchor))
    before = _decode(wire)

    def successor(source):
        source["profile_version"] = "2"
        source["timed_speech_registry_entry"]["profile_version"] = "2"
        source["stage2_command_policy"]["max_prompt_bytes"] += 1
        _rehash(source)

    after = _decode(_change_resource(wire, successor))
    reader = FakeAcceptedAnchorReader(anchor)
    for value in (before, after):
        assert bind_profile_calibration(local_run=value.local_run, shadow=value.shadow,
                                        predecessor_registry_sha256=value.predecessor_registry_sha256,
                                        store=reader) is anchor
    assert reader.calls[0] == reader.calls[1]
    assert after.local_run.calibration == before.local_run.calibration
    assert after.current_registry_sha256 != before.current_registry_sha256


@pytest.mark.parametrize("policy_field", ["stage2_command_policy", "stage3_command_policy"])
def test_real_store_anchor_guard_requires_new_profile_key_with_scripted_io(monkeypatch, policy_field):
    resource = _decode(_resource_mapping())
    changed = _change_resource(_resource_mapping(), lambda source: (
        source[policy_field].update(max_prompt_bytes=12345),
        source.update({policy_field + "_sha256": canonical_json_hash(source[policy_field])})))
    job_id, slot_id = UUID(int=1), UUID(int=2)
    for new_version in (False, True):
        if new_version:
            changed = _change_resource(changed, lambda source: (
                source.update(profile_version="2"),
                source["timed_speech_registry_entry"].update(profile_version="2")))
        successor = _decode(changed)
        snapshot = InstalledLocalRunProfileResolver(successor).snapshot
        request = VerifiedTimedSpeechAuthorityContext(snapshot, successor.local_run.timed_speech_registry_entry).bootstrap_request()
        rows = [(job_id,), ("running",),
                (job_id, "running", BOOTSTRAP_TIMED_SPEECH_PROFILE_REGISTRY_COMMAND, request.request_hash),
                ("deterministic",), (AUTHORITY_BOOTSTRAP_JOB.job_key, AUTHORITY_BOOTSTRAP_JOB.profile),
                None if new_version else (resource.current_registry_sha256, resource.local_run.timed_speech_registry_entry.canonical_hash)]
        store, cursor, connection = _store(rows)
        outcome = CommandOutcome(slot_id, "succeeded", receipt_id=UUID(int=3), artifact_set_id=UUID(int=4))
        writer = Mock(return_value=outcome)
        monkeypatch.setattr(store, "_write_success", writer)
        artifacts = (request.artifact(),)
        success = CommandSuccess(slot_id, artifact_set_hash(artifacts), artifacts)
        if not new_version:
            with pytest.raises(IdempotencyConflictError, match="already anchored"):
                store.commit_timed_speech_profile_bootstrap(success, snapshot)
            writer.assert_not_called()
            connection.rollback.assert_called_once()
        else:
            assert store.commit_timed_speech_profile_bootstrap(success, snapshot) == outcome
            writer.assert_called_once()
            connection.commit.assert_called_once()
            inserts = [params for sql, params in cursor.calls if "INSERT INTO runtime.timed_speech_profile_anchors" in sql]
            assert inserts[0][:2] == ("local_run@2", successor.current_registry_sha256)
        assert not cursor.rows
