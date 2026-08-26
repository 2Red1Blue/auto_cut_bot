"""The local recovery command cannot bypass its dedicated journal owner."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar
from uuid import UUID, uuid4

import pytest
from autocut_kernel.store import (
    SHADOW_LOCAL_CALIBRATION_MEASUREMENT_COMMAND_NAME,
    SHADOW_LOCAL_CALIBRATION_MEASUREMENT_PROTOCOL,
    ArtifactMember,
    ArtifactScope,
    CommandClaim,
    CommandRejection,
    CommandStateError,
    CommandSuccess,
    Job,
    PostgresRuntimeStore,
)
from autocut_kernel.store.models import artifact_set_hash, canonical_payload_hash

_T = TypeVar("_T")
_REQUEST_HASH = "sha256:" + "1" * 64
_JOB = Job(f"shadow-local:{_REQUEST_HASH.removeprefix('sha256:')}", "shadow")
_CLAIM = CommandClaim(
    _JOB,
    f"shadow-local-measurement:{_REQUEST_HASH.removeprefix('sha256:')}",
    SHADOW_LOCAL_CALIBRATION_MEASUREMENT_COMMAND_NAME,
    _REQUEST_HASH,
    execution_kind="deterministic",
)


class _ReservedBoundaryStore(PostgresRuntimeStore):
    """Runs only the generic pre-write reservation branches without PostgreSQL."""

    def __init__(self) -> None:
        super().__init__(lambda: (_ for _ in ()).throw(AssertionError("must not connect")))
        self.job_id = uuid4()

    def _transaction(self, operation: Callable[[object], _T]) -> _T:
        return operation(object())

    def _locked_job_then_slot(
        self, cursor: object, command_slot_id: UUID
    ) -> tuple[UUID, str, str, str]:
        del cursor
        return self.job_id, "running", SHADOW_LOCAL_CALIBRATION_MEASUREMENT_COMMAND_NAME, _REQUEST_HASH


def test_local_command_and_protocol_are_publicly_exported() -> None:
    assert SHADOW_LOCAL_CALIBRATION_MEASUREMENT_COMMAND_NAME == "MeasureShadowLocalCalibrationCommand@1"
    assert SHADOW_LOCAL_CALIBRATION_MEASUREMENT_PROTOCOL == "shadow-local-calibration-measurement-v1"


def test_generic_claim_is_rejected_before_any_connection() -> None:
    with pytest.raises(CommandStateError, match="explicit local shadow owner"):
        _ReservedBoundaryStore().claim_command(_CLAIM)

    mismatched = CommandClaim(
        _JOB,
        _CLAIM.idempotency_key,
        "unrelated-command",
        _REQUEST_HASH,
        execution_kind="deterministic",
    )
    with pytest.raises(CommandStateError, match="idempotency keys require"):
        _ReservedBoundaryStore().claim_command(mismatched)


def test_generic_success_and_rejection_are_rejected_before_any_write() -> None:
    store = _ReservedBoundaryStore()
    slot_id = uuid4()
    payload = '{"schema_version":"shadow-local-test-v1"}'
    artifact = ArtifactMember(
        "shadow_local_measurement_manifest",
        "shadow-local-measurement:test:manifest",
        1,
        ArtifactScope("autocut_calibration", "shadow_local_run", "test"),
        canonical_payload_hash(payload),
        payload,
    )
    success = CommandSuccess(slot_id, artifact_set_hash((artifact,)), (artifact,))
    with pytest.raises(CommandStateError, match="local shadow owner"):
        store.commit_command_success(success)
    with pytest.raises(CommandStateError, match="cannot use generic rejection"):
        store.commit_command_rejection(
            CommandRejection(slot_id, "INVALID", '{"reason":"test"}')
        )
