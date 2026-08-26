"""Deployment-only composition of measurement and independent calibration validation.

The caller supplies verified deployment authority and independently established
anchors. This function does not load authority, launch services, publish profiles
or enable a Runtime. Durable Commands retain retry and acceptance ownership.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from autocut_kernel.pipeline.measure_shadow_calibration_command import (
    MeasureShadowCalibrationCommand,
    ShadowCalibrationMeasurementStore,
)
from autocut_kernel.pipeline.validate_calibration_record_command import (
    CalibrationValidationLimits,
    CalibrationValidationStore,
    ValidateCalibrationRecordCommand,
)
from autocut_kernel.registry.authority_profiles import (
    ShadowCalibrationProfileSource,
    Stage1NarrativeProfileSource,
)
from autocut_kernel.store import (
    CalibrationValidationBinding,
    CommandOutcome,
    Job,
    PersistedShadowCalibrationMeasurement,
    ShadowMeasurementRetryAuthorization,
)
from autocut_kernel.store.models import MaterializationLimits

from .http_transport import FileHttpTransport
from .shadow_calibration_http import (
    ShadowCalibrationHttpMeasurementPort,
    ShadowCalibrationMaterializationStore,
)
from .shadow_calibration_inputs import (
    CalibrationSourceStore,
    CommittedCalibrationSourceHandle,
    resolve_shadow_calibration_inputs,
)


class ShadowCalibrationExecutionStore(
    CalibrationSourceStore,
    ShadowCalibrationMaterializationStore,
    ShadowCalibrationMeasurementStore,
    CalibrationValidationStore,
    Protocol,
):
    def read_shadow_calibration_measurement_outcome(
        self,
        job: Job,
        outcome: CommandOutcome,
        *,
        expected_request_sha256: str,
        expected_profile_source_sha256: str,
        expected_registry_snapshot_sha256: str,
    ) -> PersistedShadowCalibrationMeasurement: ...


@dataclass(frozen=True, slots=True)
class ShadowCalibrationExecutionResult:
    measurement: CommandOutcome
    validation: CommandOutcome | None


def execute_shadow_calibration(
    *,
    store: ShadowCalibrationExecutionStore,
    profile: ShadowCalibrationProfileSource,
    narrative: Stage1NarrativeProfileSource,
    expected_profile_contract_sha256: str,
    registry_snapshot_sha256: str,
    source_handles: tuple[CommittedCalibrationSourceHandle, ...],
    materialization_limits: MaterializationLimits,
    max_response_bytes: int,
    endpoint_url: str,
    shared_token: str,
    timeout_seconds: int,
    validation_limits: CalibrationValidationLimits,
    validation_attempt_idempotency_key: str,
    retry_authorization: ShadowMeasurementRetryAuthorization | None = None,
    transport: FileHttpTransport | None = None,
) -> ShadowCalibrationExecutionResult:
    if (
        type(validation_attempt_idempotency_key) is not str
        or not validation_attempt_idempotency_key.strip()
    ):
        raise ValueError("validator attempt idempotency key must be explicit nonempty text")
    validator = ValidateCalibrationRecordCommand(
        store, profile, registry_snapshot_sha256, narrative,
        expected_profile_contract_sha256, validation_limits,
    )
    resolved = resolve_shadow_calibration_inputs(
        store=store, profile=profile, narrative=narrative,
        expected_profile_contract_sha256=expected_profile_contract_sha256,
        registry_snapshot_sha256=registry_snapshot_sha256, source_handles=source_handles,
        limits=materialization_limits, max_response_bytes=max_response_bytes,
    )
    port = ShadowCalibrationHttpMeasurementPort(
        expected_request=resolved.request, source_bindings=resolved.source_bindings,
        store=store, limits=materialization_limits, endpoint_url=endpoint_url,
        shared_token=shared_token, timeout_seconds=timeout_seconds,
        max_response_bytes=max_response_bytes, transport=transport,
    )
    measured = MeasureShadowCalibrationCommand(store, port).execute(
        resolved.request, retry_authorization=retry_authorization,
    )
    if measured.state != "succeeded":
        return ShadowCalibrationExecutionResult(measured, None)
    persisted = store.read_shadow_calibration_measurement_outcome(
        resolved.request.job, measured,
        expected_request_sha256=resolved.request.request_hash,
        expected_profile_source_sha256=profile.source_sha256,
        expected_registry_snapshot_sha256=registry_snapshot_sha256,
    )
    binding = CalibrationValidationBinding(
        profile.profile_version, profile.source_sha256, registry_snapshot_sha256,
        persisted.manifest.reference, persisted.results.reference,
        validation_attempt_idempotency_key,
    )
    return ShadowCalibrationExecutionResult(measured, validator.execute(binding))
