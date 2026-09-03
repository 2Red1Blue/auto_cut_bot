"""Synthetic local-run v4 content; no real calibration, provider or DB run."""

from __future__ import annotations

import base64
import json
import os
import subprocess
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes, canonical_json_hash
from autocut_kernel.registry.authority_profiles import AuthorityProfileSourceError
from autocut_kernel.registry.calibration_binding import bind_profile_calibration
from autocut_kernel.registry.installed_local_run import LocalRunResourceError
from autocut_kernel.registry.timed_speech_contract import timed_speech_registry_contract_sha256
from autocut_kernel.semantic_chain.editorial_command_policy import Stage3CommandPolicy

from tests.authority.test_authority_profile_sources import (
    REPO_ROOT,
    _decoded_dependencies,
    _hash,
    _profiles,
    _raw,
    decode_local_run_profile_source,
    synthetic_stage3_command_policy,
)
from tests.authority.test_installed_local_run import _decode, _resource_mapping
from tests.authority.test_local_run_calibration import (
    FakeAcceptedAnchorReader,
    _fixture_anchor,
    _fixture_record,
    _project,
)
from tests.authority.test_stage2_policy_source import _change_resource, _decode_source, _validator
from tests.pipeline.installed_profile_fixture import synthetic_installed_resource


def _rehash(source):
    source["stage3_command_policy_sha256"] = canonical_json_hash(source["stage3_command_policy"])


def test_v4_roundtrip_preserves_full_stage3_and_exact_v2_dependencies():
    narrative, shadow, source = _profiles()
    assert narrative["schema_version"] == "autocut-stage1-narrative-profile-v2"
    assert shadow["schema_version"] == "autocut-shadow-calibration-profile-v2"
    assert source["schema_version"] == "autocut-local-run-profile-v4"
    _validator().validate(source)
    decoded = _decode_source(source)
    assert type(decoded.stage3_command_policy) is Stage3CommandPolicy
    assert decoded.stage3_command_policy == synthetic_stage3_command_policy()
    assert decoded.stage3_command_policy_sha256 == canonical_json_hash(source["stage3_command_policy"])
    assert decoded.to_mapping() == source
    with pytest.raises(FrozenInstanceError):
        decoded.stage3_command_policy = None
    fresh = decoded.to_mapping()
    fresh["stage3_command_policy"]["retry_policy"]["backoff_seconds"].clear()
    assert decoded.to_mapping() == source


@pytest.mark.parametrize("version", ["v1", "v2", "v3"])
def test_old_sources_get_no_implicit_stage3_policy(version):
    _, _, source = _profiles()
    source["schema_version"] = "autocut-local-run-profile-" + version
    source.pop("stage3_command_policy")
    source.pop("stage3_command_policy_sha256")
    assert not _validator().is_valid(source)
    with pytest.raises(AuthorityProfileSourceError):
        _decode_source(source)


@pytest.mark.parametrize("field", ["stage3_command_policy", "stage3_command_policy_sha256"])
@pytest.mark.parametrize("change", ["missing", "null", "array"])
def test_policy_and_hash_are_explicit_required_fields(field, change):
    _, _, source = _profiles()
    if change == "missing":
        del source[field]
    else:
        source[field] = None if change == "null" else []
    assert not _validator().is_valid(source)
    with pytest.raises(AuthorityProfileSourceError):
        _decode_source(source)


@pytest.mark.parametrize("digest", [_hash("foreign-stage3"), "sha256:" + "0" * 64, "sha256:" + "A" * 64])
def test_policy_hash_is_recomputed_not_trusted(digest):
    _, _, source = _profiles()
    source["stage3_command_policy_sha256"] = digest
    with pytest.raises(AuthorityProfileSourceError):
        _decode_source(source)


@pytest.mark.parametrize("path", [(), ("generation",), ("draft_policy",), ("context_policy",), ("feasibility_policy",), ("retry_policy",)])
@pytest.mark.parametrize("change", ["missing", "extra"])
def test_all_nested_objects_are_closed_after_full_policy_rehash(path, change):
    _, _, source = _profiles()
    value = source["stage3_command_policy"]
    for part in path:
        value = value[part]
    if change == "missing":
        del value[next(iter(value))]
    else:
        value["accepted"] = True
    _rehash(source)
    assert not _validator().is_valid(source)
    with pytest.raises(AuthorityProfileSourceError):
        _decode_source(source)


@pytest.mark.parametrize("path,value", [
    (("artifact_revision",), True), (("artifact_revision",), 0), (("artifact_revision",), 2**53),
    (("max_prompt_bytes",), False), (("max_prompt_bytes",), 16777217),
    (("generation", "provider_id"), "foreign"), (("generation", "model_id"), "foreign"),
    (("generation", "adapter_strategy_version"), "foreign"), (("generation", "prompt_template"), "   "),
    (("generation", "prompt_version"), ""), (("generation", "max_output_tokens"), 32769),
    (("generation", "temperature"), "1.0"), (("generation", "temperature"), "NaN"),
    (("blueprint_strategy_version",), "partitioned"),
    (("draft_policy", "budget_unit"), "tokens"), (("draft_policy", "max_response_bytes"), 16777217),
    (("draft_policy", "max_json_depth"), 65), (("draft_policy", "max_stories"), 129),
    (("draft_policy", "max_beats_per_story"), 129), (("draft_policy", "max_total_beats"), 1025),
    (("draft_policy", "max_requirements_per_beat"), 65), (("draft_policy", "max_total_requirements"), 4097),
    (("draft_policy", "max_alternatives_per_requirement"), 129), (("draft_policy", "max_total_alternatives"), 8193),
    (("draft_policy", "max_references_per_field"), 1025), (("draft_policy", "max_total_references"), 65537),
    (("draft_policy", "max_ordering_constraints_per_story"), 1025), (("draft_policy", "max_total_ordering_constraints"), 8193),
    (("draft_policy", "max_text_characters"), 65537), (("draft_policy", "max_total_text_characters"), 4194305),
    (("context_policy", "strategy"), "partitioned"), (("context_policy", "budget_unit"), "tokens"),
    (("context_policy", "max_story_context_bytes"), 67108865), (("context_policy", "max_batch_context_bytes"), 67108865),
    (("context_policy", "max_source_members"), 8193), (("feasibility_policy", "strategy_version"), "foreign"),
    (("feasibility_policy", "max_search_states"), 1000001), (("feasibility_policy", "max_search_states"), True),
    (("retry_policy", "max_attempts"), 4), (("retry_policy", "backoff_seconds"), [2]),
    (("retry_policy", "backoff_seconds"), [True, 8]),
])
def test_schema_and_shared_typed_owner_reject_invalid_rehashed_policy(path, value):
    _, _, source = _profiles()
    target = source["stage3_command_policy"]
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    if value != 2**53:
        _rehash(source)
    assert not _validator().is_valid(source)
    with pytest.raises(AuthorityProfileSourceError):
        _decode_source(source)


def test_schema_valid_cross_field_text_budget_is_still_rejected():
    _, _, source = _profiles()
    source["stage3_command_policy"]["draft_policy"]["max_total_text_characters"] = 1
    _rehash(source)
    _validator().validate(source)
    with pytest.raises(AuthorityProfileSourceError):
        _decode_source(source)


@pytest.mark.parametrize("number", [b"1.0", b"NaN", b"Infinity", b"true", b'1,"artifact_revision":1'])
def test_nested_raw_policy_json_rejects_float_nonfinite_boolean_duplicate(number):
    narrative, shadow, source = _profiles()
    narrative, shadow = _decoded_dependencies(narrative, shadow)
    raw = _raw(source).replace(b'"stage3_command_policy":{"artifact_revision":1',
                              b'"stage3_command_policy":{"artifact_revision":' + number)
    with pytest.raises(AuthorityProfileSourceError):
        decode_local_run_profile_source(raw, narrative=narrative, shadow=shadow)


@pytest.mark.parametrize("part", ["artifact_revision", "generation", "max_prompt_bytes", "draft_policy", "context_policy", "feasibility_policy", "retry_policy"])
def test_every_variable_stage3_policy_component_changes_only_current_registry(part):
    original = _resource_mapping()
    before = _decode(original)

    def change(source):
        policy = source["stage3_command_policy"]
        if part in ("artifact_revision", "max_prompt_bytes"):
            policy[part] += 1
        elif part == "generation":
            policy[part]["prompt_template"] += " 中文补充。"
        elif part == "draft_policy":
            policy[part]["max_total_beats"] += 1
        elif part == "context_policy":
            policy[part]["max_source_members"] += 1
        elif part == "feasibility_policy":
            policy[part]["max_search_states"] += 1
        else:
            policy[part]["backoff_seconds"] = [3, 9]
        _rehash(source)

    changed = _change_resource(original, change)
    after = _decode(changed)
    assert after.current_registry_sha256 != before.current_registry_sha256
    assert after.local_run.source_sha256 != before.local_run.source_sha256
    assert after.local_run.stage3_command_policy_sha256 != before.local_run.stage3_command_policy_sha256
    assert after.local_run.stage2_command_policy == before.local_run.stage2_command_policy
    assert after.narrative == before.narrative and after.shadow == before.shadow
    assert changed["predecessor"] == original["predecessor"]
    assert after.local_run.calibration == before.local_run.calibration
    assert after.local_run.timed_speech_registry_entry == before.local_run.timed_speech_registry_entry
    assert _decode(changed) == after
    raw_profile = json.loads(base64.b64decode(changed["current"]["profile_raw_base64"]))
    assert after.local_run.stage3_command_policy.to_mapping() == raw_profile["stage3_command_policy"]


def test_outer_resource_rehash_cannot_hide_stale_inner_policy_hash():
    changed = _change_resource(_resource_mapping(), lambda source: source["stage3_command_policy"].update(max_prompt_bytes=100))
    with pytest.raises(LocalRunResourceError):
        _decode(changed)


def test_stage3_schema_is_outside_unchanged_timed_speech_contract_closure():
    schema = json.loads((REPO_ROOT / "governance/schemas/local-run-profile.schema.json").read_bytes())
    current = timed_speech_registry_contract_sha256(canonical_json_bytes(schema))
    schema["$id"] = "schema://governance/local-run-profile/3.0.0"
    schema["properties"]["schema_version"]["const"] = "autocut-local-run-profile-v3"
    for field in ("stage3_command_policy", "stage3_command_policy_sha256"):
        schema["required"].remove(field)
        del schema["properties"][field]
    schema["$defs"] = {key: value for key, value in schema["$defs"].items() if not key.startswith("stage3_")}
    assert timed_speech_registry_contract_sha256(canonical_json_bytes(schema)) == current


def test_installed_fixture_accepts_explicit_stage3_policy_not_process_defaults():
    policy = replace(synthetic_stage3_command_policy(), max_prompt_bytes=1_000_000)
    resource = synthetic_installed_resource(stage3_command_policy=policy)
    assert resource.local_run.stage3_command_policy == policy
    assert resource.local_run.stage3_command_policy_sha256 == policy.canonical_hash


def test_new_local_version_reuses_identical_fake_accepted_calibration_without_measurement():
    wire = _resource_mapping()
    resource = _decode(wire)
    context = SimpleNamespace(profiles=SimpleNamespace(shadow=resource.shadow),
                              compilation=SimpleNamespace(registry_sha256=resource.predecessor_registry_sha256))
    anchor = _fixture_anchor(_fixture_record(context))
    wire = _change_resource(wire, lambda source: _project(source, anchor))
    before = _decode(wire)

    def successor(source):
        source["profile_version"] = "2"
        source["timed_speech_registry_entry"]["profile_version"] = "2"
        source["stage3_command_policy"]["max_prompt_bytes"] += 1
        _rehash(source)

    after = _decode(_change_resource(wire, successor))
    reader = FakeAcceptedAnchorReader(anchor)
    for value in (before, after):
        assert bind_profile_calibration(local_run=value.local_run, shadow=value.shadow,
            predecessor_registry_sha256=value.predecessor_registry_sha256, store=reader) is anchor
    assert reader.calls[0] == reader.calls[1]
    assert after.local_run.calibration == before.local_run.calibration
    assert after.current_registry_sha256 != before.current_registry_sha256


@pytest.mark.parametrize("distribution", ["root", "kernel"])
def test_isolated_wheel_reconstructs_all_three_exact_policies_without_application(tmp_path, distribution):
    from authority.local_run_packaging import prepare_locked_local_run_package

    from tests.architecture.test_local_authority_packaging import (
        _build_and_load,
        _root_build_source,
        _standalone_build_source,
    )
    from tests.authority.test_local_run_resource import _synthetic_accepted_sources

    authority_root = tmp_path / "synthetic-authority"
    authority_root.mkdir()
    sources, anchor = _synthetic_accepted_sources(authority_root)
    builder = _root_build_source if distribution == "root" else _standalone_build_source
    build_root, kernel_package = builder(tmp_path / "staging")
    output = prepare_locked_local_run_package(**sources.options, store=FakeAcceptedAnchorReader(anchor),
                                               destination_kernel_package=kernel_package)
    resource = _decode(json.loads((output / "local-run.json").read_bytes()))
    wheel_root = tmp_path / "wheel"
    _build_and_load(
        source=build_root,
        expected_distribution_prefix=(
            "auto_cut_bot_ai" if distribution == "root" else "autocut_kernel"
        ),
        tmp_path=wheel_root,
    )
    python = wheel_root / "clean-environment" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    script = (
        "import json,sys; from autocut_kernel.registry.installed_local_run import load_installed_local_run_resource; "
        "r=load_installed_local_run_resource(); "
        "assert not any(n.split('.')[0] in {'auto_cut_bot','authority','torch','funasr','psycopg','asyncpg'} for n in sys.modules); "
        "print(json.dumps([r.narrative.command_policy.canonical_hash,r.local_run.stage2_command_policy_sha256,"
        "r.local_run.stage3_command_policy_sha256,r.local_run.to_mapping()['schema_version']]))"
    )
    completed = subprocess.run([str(python), "-c", script], check=True, capture_output=True, text=True,
        cwd=wheel_root / "no-checkout-runtime", env={"PATH": os.environ["PATH"], "PYTHONPATH": ""})
    assert json.loads(completed.stdout) == [resource.narrative.command_policy.canonical_hash,
        resource.local_run.stage2_command_policy_sha256, resource.local_run.stage3_command_policy_sha256,
        "autocut-local-run-profile-v4"]
