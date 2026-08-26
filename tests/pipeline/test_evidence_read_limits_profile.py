"""Independent JSON budgets on synthetic profiles; no DB or evidence authority."""

import json
from dataclasses import FrozenInstanceError, replace
from hashlib import sha256

import pytest

from auto_cut_bot.pipeline.runtime.errors import PipelineRunValidationError
from auto_cut_bot.pipeline.runtime.models import (
    EvidenceReadLimits,
    PipelineCommand,
    PipelineExecutionProfile,
    PipelineRunRequest,
    PipelineStageContext,
)
from auto_cut_bot.pipeline.runtime.postgres import PostgresPipelineRunStore, _execution_profile
from tests.pipeline.runtime_profile_fixture import execution_profile


@pytest.mark.parametrize("field", ("max_blob_bytes", "max_total_blob_bytes"))
@pytest.mark.parametrize("invalid", (True, False, 1.0, 0, -1, "1", None, 2**53))
def test_limits_reject_invalid_leaf_types_and_bounds_direct_and_wire(field, invalid):
    mapping = {"max_blob_bytes": 1, "max_total_blob_bytes": 10}
    mapping[field] = invalid
    with pytest.raises(PipelineRunValidationError):
        EvidenceReadLimits(**mapping)
    with pytest.raises(PipelineRunValidationError):
        EvidenceReadLimits.from_mapping(mapping)
    profile = execution_profile().to_mapping()
    profile["evidence_read_limits"] = mapping
    with pytest.raises(PipelineRunValidationError):
        PipelineExecutionProfile.from_mapping(profile)


@pytest.mark.parametrize("invalid", (
    None, [], "{}", {}, {"max_blob_bytes": 1}, {"max_total_blob_bytes": 2},
    {"max_blob_bytes": 1, "max_total_blob_bytes": 2, "default": 1},
    {"max_blob_bytes": 3, "max_total_blob_bytes": 2},
))
def test_limits_require_exact_closed_object(invalid):
    with pytest.raises(PipelineRunValidationError):
        EvidenceReadLimits.from_mapping(invalid)


def test_limits_are_immutable_and_hash_exact_canonical_bytes():
    limits = EvidenceReadLimits(1, 9_007_199_254_740_991)
    raw = b'{"max_blob_bytes":1,"max_total_blob_bytes":9007199254740991}'
    assert limits.canonical_hash == "sha256:" + sha256(raw).hexdigest()
    assert EvidenceReadLimits.from_mapping(limits.to_mapping()) == limits
    copied = limits.to_mapping()
    copied["max_blob_bytes"] = 2
    assert limits.max_blob_bytes == 1
    with pytest.raises(FrozenInstanceError):
        limits.max_blob_bytes = 2


@pytest.mark.parametrize("limits", (EvidenceReadLimits(100_001, 500_000), EvidenceReadLimits(100_000, 500_001)))
def test_each_limit_changes_execution_hash_without_changing_source_limits(limits):
    original = execution_profile()
    changed = execution_profile(evidence_limits=limits)
    assert changed.canonical_hash != original.canonical_hash
    assert changed.to_materialization_limits() == original.to_materialization_limits()
    assert changed.to_doubao_policy() == original.to_doubao_policy()
    assert changed.to_evidence_read_limits() == limits
    assert PipelineExecutionProfile.from_mapping(changed.to_mapping()) == changed
    with pytest.raises(PipelineRunValidationError, match="hash does not bind"):
        _execution_profile(changed.to_mapping(), original.canonical_hash)


def test_v9_requires_explicit_limits_no_default_or_redundant_hash():
    original = execution_profile()
    arguments = {
        "retry_policy": original.to_generation_retry_policy(),
        "materialization_limits": original.to_materialization_limits(),
        "stage1_policy": original.build_stage1_command_policy(),
        "stage2_policy": original.build_stage2_command_policy(),
        "stage3_policy": original.build_stage3_command_policy(),
    }
    with pytest.raises(TypeError, match="evidence_read_limits"):
        PipelineExecutionProfile.from_policies(
            original.to_doubao_policy(), original.to_media_preflight_policy(), **arguments,
        )
    with pytest.raises(PipelineRunValidationError, match="exact EvidenceReadLimits"):
        PipelineExecutionProfile.from_policies(
            original.to_doubao_policy(), original.to_media_preflight_policy(),
            evidence_read_limits={"max_blob_bytes": 1, "max_total_blob_bytes": 2}, **arguments,
        )
    mapping = original.to_mapping()
    del mapping["evidence_read_limits"]
    with pytest.raises(PipelineRunValidationError, match="missing fields"):
        PipelineExecutionProfile.from_mapping(mapping)
    mapping = original.to_mapping()
    mapping["evidence_read_limits_sha256"] = original.to_evidence_read_limits().canonical_hash
    with pytest.raises(PipelineRunValidationError, match="unsupported fields"):
        PipelineExecutionProfile.from_mapping(mapping)


@pytest.mark.parametrize("raw", (
    None, "{}", "null", "[]", ' {"max_blob_bytes":1,"max_total_blob_bytes":2}',
    '{"max_blob_bytes":1,"max_blob_bytes":1,"max_total_blob_bytes":2}',
    '{"max_blob_bytes":1.0,"max_total_blob_bytes":2}',
    '{"max_blob_bytes":NaN,"max_total_blob_bytes":2}',
))
def test_embedded_json_must_be_closed_strict_and_canonical(raw):
    with pytest.raises(PipelineRunValidationError):
        replace(execution_profile(), evidence_read_limits_json=raw)


def test_v8_retains_exact_wire_hash_and_all_policies_but_cannot_execute_or_claim():
    mapping = execution_profile().to_mapping()
    mapping["schema_version"] = "pipeline-execution-profile-v8"
    del mapping["evidence_read_limits"]
    historical = PipelineExecutionProfile.from_mapping(mapping)
    assert historical.to_mapping() == mapping
    expected_raw = json.dumps(mapping, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    assert historical.canonical_hash == "sha256:" + sha256(expected_raw).hexdigest()
    assert historical.evidence_read_limits_json is None
    assert not historical.has_media_preflight_policy
    assert _execution_profile(mapping, historical.canonical_hash) == historical
    assert historical.build_stage3_command_policy() == execution_profile().build_stage3_command_policy()
    for build in (historical.to_evidence_read_limits, historical.to_doubao_policy):
        with pytest.raises(PipelineRunValidationError):
            build()
    with pytest.raises(PipelineRunValidationError, match="persisted mappings"):
        replace(execution_profile(), schema_version="pipeline-execution-profile-v8")
    for stage in ("vlm", "stage1_narrative", "stage2_portfolio", "stage3_blueprint", "media_preflight"):
        with pytest.raises(PipelineRunValidationError, match="profile v9"):
            PipelineStageContext(
                "pipeline_run_" + "1" * 32,
                PipelineRunRequest("test", source_reference="synthetic-source"),
                PipelineCommand("history", stage, "pending"), historical,
            )

    def forbidden_connection():
        raise AssertionError("historical claim must reject before DB I/O")

    store = PostgresPipelineRunStore(forbidden_connection)
    request = PipelineRunRequest("test", source_reference="synthetic-source")
    with pytest.raises(PipelineRunValidationError, match="profile v9"):
        store._claim_run_sync("pipeline_run_" + "1" * 32, "historical", request, request.request_hash, historical)
    mapping["evidence_read_limits"] = {"max_blob_bytes": 1, "max_total_blob_bytes": 2}
    with pytest.raises(PipelineRunValidationError, match="unsupported fields"):
        PipelineExecutionProfile.from_mapping(mapping)
