"""Real command composition with synthetic native bytes and an in-memory Store.

These are integration fixtures, not actual model calibration. PostgreSQL result
provenance is independently exercised by the Store integration suite.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from autocut_kernel.media import SHADOW_CALIBRATION_RAW_RESPONSE_SCHEMA
from autocut_kernel.pipeline.validate_calibration_record_command import CalibrationValidationLimits
from autocut_kernel.store import (
    CalibrationValidationBinding,
    CommandOutcome,
    CommittedArtifactMemberReference,
    Job,
    PersistedCommittedArtifactMember,
    PersistedShadowCalibrationMeasurement,
    PostgresRuntimeStore,
    RuntimeStoreError,
    ShadowMeasurementRetryAuthorization,
)

from auto_cut_bot.pipeline.media_preflight.shadow_calibration_execution import (
    execute_shadow_calibration,
)
from auto_cut_bot.pipeline.media_preflight.shadow_calibration_inputs import (
    resolve_shadow_calibration_inputs,
)
from tests.pipeline.test_measure_shadow_calibration_command import _Store
from tests.pipeline.test_shadow_calibration_http import ENDPOINT, Transport
from tests.pipeline.test_shadow_calibration_http import Store as FileStore
from tests.pipeline.test_shadow_calibration_inputs import options  # noqa: F401
from tests.pipeline.test_shadow_calibration_service_profile import (
    inputs as service_inputs,  # noqa: F401
)
from tests.pipeline.test_validate_calibration_record_command import FakeStore


class ExecutionStore(_Store):
    def __init__(self, source_store: Any, directory: Path) -> None:
        super().__init__()
        self.source_store = source_store
        self.files = FileStore(directory)
        self.raw_blobs: dict[UUID, bytes] = {}
        self.validations: list[CalibrationValidationBinding] = []
        self.reader_calls: list[dict[str, Any]] = []
        self.validation_store: FakeStore | None = None
        self.reader_failure: Exception | None = None
        self.tamper_raw = False

    def read_whole_series_source_manifest(self, *args: Any) -> Any:
        return self.source_store.read_whole_series_source_manifest(*args)

    def materialize_immutable_blob(self, *args: Any) -> Any:
        return self.files.materialize_immutable_blob(*args)

    def stage_shadow_measurement_member_response(self, *args: Any, **kwargs: Any) -> Any:
        result = super().stage_shadow_measurement_member_response(*args, **kwargs)
        self.raw_blobs[self.blobs[-1].object_id] = kwargs["staged"].raw_bytes
        return result

    def finalize_shadow_measurement_success(self, attempt_id: UUID, *, expected_version: int) -> CommandOutcome:
        outcome = replace(super().finalize_shadow_measurement_success(attempt_id, expected_version=expected_version),
                          receipt_id=uuid4(), artifact_set_id=uuid4())
        self.attempts[attempt_id] = replace(self.attempts[attempt_id], outcome=outcome)
        return outcome

    def read_shadow_calibration_measurement_outcome(self, job: Job, outcome: CommandOutcome, **expected: Any) -> PersistedShadowCalibrationMeasurement:
        self.reader_calls.append({"job": job, "outcome": outcome, **expected})
        if self.reader_failure:
            raise self.reader_failure
        attempt = next(item for item in self.attempts.values() if item.command_slot_id == outcome.command_slot_id)
        assert job == attempt.job and outcome == attempt.outcome
        assert expected["expected_request_sha256"] == attempt.plan_hash
        assert outcome.receipt_id is not None and outcome.artifact_set_id is not None
        artifacts = PostgresRuntimeStore(lambda: pytest.fail("no database in this fixture"))._shadow_measurement_artifacts(attempt)
        members = tuple(PersistedCommittedArtifactMember(
            CommittedArtifactMemberReference(outcome.receipt_id, outcome.artifact_set_id, ordinal,
                                             artifact.scope, artifact.artifact_type, artifact.logical_id,
                                             artifact.revision, artifact.content_hash),
            artifact.payload_json, outcome.command_slot_id,
        ) for ordinal, artifact in enumerate(artifacts))
        measured = PersistedShadowCalibrationMeasurement(job, attempt.plan_hash, outcome.command_slot_id, *members)
        if self.validation_store is None:
            self.validation_store = FakeStore(measured, self.raw_blobs)
        return measured

    def claim_command(self, claim: Any) -> CommandOutcome:
        assert self.validation_store is not None
        return self.validation_store.claim_command(claim)

    def read_committed_shadow_calibration_measurement(self, binding: CalibrationValidationBinding) -> PersistedShadowCalibrationMeasurement:
        self.validations.append(binding)
        assert self.validation_store is not None
        return self.validation_store.read_committed_shadow_calibration_measurement(binding)

    def read_immutable_blob(self, *args: Any) -> bytes:
        assert self.validation_store is not None
        return b"corrupt" if self.tamper_raw else self.validation_store.read_immutable_blob(*args)

    def commit_calibration_record_validation_success(self, *args: Any) -> CommandOutcome:
        assert self.validation_store is not None
        return self.validation_store.commit_calibration_record_validation_success(*args)

    def commit_command_rejection(self, *args: Any) -> CommandOutcome:
        assert self.validation_store is not None
        return self.validation_store.commit_command_rejection(*args)


@pytest.fixture
def execution(options: dict[str, Any], tmp_path: Path) -> dict[str, Any]:  # noqa: F811 - imported fixture
    resolved = resolve_shadow_calibration_inputs(**options)
    mapping = resolved.request.corpus_members[0].native_invocation.request_mapping
    raw = json.dumps({
        "schema_version": SHADOW_CALIBRATION_RAW_RESPONSE_SCHEMA,
        "request_identity_sha256": mapping.sha256,
        "source": mapping.source.to_response_mapping(),
        "audio_clock": mapping.audio_clock.to_mapping(),
        "requested_range": {"in_tick": 0, "out_tick": 100},
        "timed_speech_policy_sha256": mapping.timed_speech_policy_sha256,
        "word_gap_policy_sha256": mapping.word_gap_policy_sha256,
        "vad_merge_policy_sha256": mapping.vad_merge_policy_sha256,
        "native_profile_identity_sha256": mapping.native_profile_identity_sha256,
        "producer_identities": [item.to_mapping() for item in mapping.producer_identities],
        "asr_native_output": [{"text": "a", "words": ["a"], "timestamp": [[0, 1]]}],
        "vad_native_output": [{"value": [[0, 2]]}],
    }, sort_keys=True, separators=(",", ":")).encode()
    values = dict(options)
    values["store"] = ExecutionStore(options["store"], tmp_path)
    values["materialization_limits"] = values.pop("limits")
    return {**values, "endpoint_url": ENDPOINT, "shared_token": "fixture-token", "timeout_seconds": 5,
            "transport": Transport(raw), "validation_limits": CalibrationValidationLimits(32768, 65536),
            "validation_attempt_idempotency_key": "validation-attempt:explicit-1"}


def test_composition_uses_actual_persisted_refs_and_replays_without_native(execution: dict[str, Any]) -> None:
    result = execute_shadow_calibration(**execution)
    store, transport = execution["store"], execution["transport"]
    assert result.measurement.state == "succeeded"
    assert result.validation is not None and result.validation.state == "succeeded"
    assert store.validation_store.accepted is not None
    binding = store.validations[0]
    assert binding.manifest_reference == store.validation_store.measured.manifest.reference
    assert binding.results_reference == store.validation_store.measured.results.reference
    assert binding.attempt_idempotency_key == execution["validation_attempt_idempotency_key"]
    assert store.reader_calls[0]["expected_profile_source_sha256"] == execution["profile"].source_sha256
    assert store.reader_calls[0]["expected_registry_snapshot_sha256"] == execution["registry_snapshot_sha256"]
    assert len(transport.calls) == len(store.files.calls) == 1
    assert store.files.leases[0].closes == 1
    assert execute_shadow_calibration(**execution) == result
    assert len(transport.calls) == len(store.files.calls) == len(store.validations) == 1


@pytest.mark.parametrize("status", [429, 500, 503])
def test_native_unavailable_remains_running_without_validator_or_hidden_retry(execution: dict[str, Any], status: int) -> None:
    execution["transport"].status = status
    result = execute_shadow_calibration(**execution)
    assert result.measurement.state == "running" and result.validation is None
    assert execute_shadow_calibration(**execution).validation is None
    assert len(execution["transport"].calls) == 1
    assert not execution["store"].reader_calls and not execution["store"].validations


def test_malformed_native_is_denied_before_validator(execution: dict[str, Any]) -> None:
    execution["transport"].raw = b"{}"
    result = execute_shadow_calibration(**execution)
    assert result.measurement.state == "denied" and result.validation is None
    assert not execution["store"].reader_calls


def test_measurement_success_does_not_mask_independent_validation_denial(execution: dict[str, Any]) -> None:
    execution["store"].tamper_raw = True
    result = execute_shadow_calibration(**execution)
    assert result.measurement.state == "succeeded"
    assert result.validation is not None and result.validation.state == "denied"
    assert execution["store"].validation_store.accepted is None


@pytest.mark.parametrize("key", [None, "", " ", True])
def test_missing_validator_key_fails_before_source_or_native_io(execution: dict[str, Any], key: object) -> None:
    store = execution["store"]
    reads_before = store.source_store.calls
    execution["validation_attempt_idempotency_key"] = key
    with pytest.raises(ValueError, match="idempotency key"):
        execute_shadow_calibration(**execution)
    assert store.source_store.calls == reads_before
    assert not store.attempts and not execution["transport"].calls


def test_store_result_unavailable_is_not_rewritten_as_calibration_acceptance(execution: dict[str, Any]) -> None:
    failure = RuntimeStoreError("measurement set unavailable")
    execution["store"].reader_failure = failure
    with pytest.raises(RuntimeStoreError) as caught:
        execute_shadow_calibration(**execution)
    assert caught.value is failure
    assert not execution["store"].validations


def test_unknown_native_requires_explicit_successor_authorization(execution: dict[str, Any]) -> None:
    store, transport = execution["store"], execution["transport"]
    transport.status = 503
    assert execute_shadow_calibration(**execution).validation is None
    attempt = store.attempts[store.initial_attempt_id]
    store.attempts[attempt.attempt_id] = replace(attempt, members=(replace(
        attempt.members[0], lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    ),))
    assert execute_shadow_calibration(**execution).validation is None
    assert store.attempts[attempt.attempt_id].state == "indeterminate"
    transport.status = 200
    assert execute_shadow_calibration(**execution).validation is None
    assert len(transport.calls) == 1
    execution["retry_authorization"] = ShadowMeasurementRetryAuthorization(
        "sha256:" + "a" * 64, attempt.plan_hash,
    )
    result = execute_shadow_calibration(**execution)
    assert result.validation is not None and result.validation.state == "succeeded"
    assert len(transport.calls) == 2
    successor = next(item for item in store.attempts.values() if item.previous_attempt_id == attempt.attempt_id)
    assert result.measurement.command_slot_id == successor.command_slot_id
    assert store.validations[0].manifest_reference.receipt_id == successor.outcome.receipt_id


def test_invalid_validation_limits_fail_before_dispatch(execution: dict[str, Any]) -> None:
    execution["validation_limits"] = None
    with pytest.raises(ValueError, match="byte limits"):
        execute_shadow_calibration(**execution)
    assert not execution["transport"].calls and not execution["store"].attempts
