"""Pure artifact closure for committed shadow-local measurement attempts.

This module deliberately knows neither PostgreSQL nor calibration acceptance.
It turns one already-persisted, fully staged attempt and its exact raw bytes
into the two immutable Store artifacts used by the local-measurement route.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast
from uuid import UUID

from ..media.shadow_local_measurement_set import (
    ShadowLocalMeasurementManifest,
    ShadowLocalMeasurementResults,
    ShadowLocalMeasurementSetError,
    ShadowLocalMeasurementValidationReport,
)
from .errors import StoreValidationError
from .models import (
    SHADOW_LOCAL_CALIBRATION_MEASUREMENT_COMMAND_NAME,
    ArtifactMember,
    ArtifactScope,
    BlobRef,
    CommandClaim,
    ShadowLocalMeasurementAttempt,
    ShadowLocalMeasurementMember,
    ShadowLocalMeasurementMemberPlan,
    ShadowLocalMeasurementPlan,
    canonical_payload_hash,
)

SHADOW_LOCAL_MEASUREMENT_MANIFEST_ARTIFACT_SCHEMA = "shadow-local-measurement-artifact-manifest-v1"
SHADOW_LOCAL_MEASUREMENT_RESULTS_ARTIFACT_SCHEMA = "shadow-local-measurement-artifact-results-v1"
SHADOW_LOCAL_MEASUREMENT_MANIFEST_ARTIFACT_TYPE = "shadow_local_measurement_manifest"
SHADOW_LOCAL_MEASUREMENT_RESULTS_ARTIFACT_TYPE = "shadow_local_measurement_results"

RawResponseKey = tuple[int, str]


def _store_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _strict_object(value: str, name: str) -> dict[str, object]:
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError) as error:
        raise StoreValidationError(f"{name} must be strict JSON") from error
    if type(decoded) is not dict:  # noqa: E721
        raise StoreValidationError(f"{name} must be an object")
    return cast(dict[str, object], decoded)


def _sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _blob_mapping(blob: BlobRef) -> dict[str, object]:
    return {
        "object_id": str(blob.object_id),
        "content_hash": blob.content_hash,
        "byte_length": blob.byte_length,
        "media_type": blob.media_type,
    }


def _member_plan(member: ShadowLocalMeasurementMember) -> ShadowLocalMeasurementMemberPlan:
    return ShadowLocalMeasurementMemberPlan(
        member.member_ordinal,
        member.case_sha256,
        member.request_sha256,
        member.canonical_case_json,
        member.canonical_request_json,
        member.source_job_id,
        member.source_blob,
        member.source_blob_reference_sha256,
        member.binding_sha256,
        member.service_profile_sha256,
        member.max_response_bytes,
    )


def _plan_for_attempt(attempt: ShadowLocalMeasurementAttempt) -> ShadowLocalMeasurementPlan:
    claim = CommandClaim(
        attempt.job,
        f"shadow-local-measurement:{attempt.plan_hash.removeprefix('sha256:')}",
        SHADOW_LOCAL_CALIBRATION_MEASUREMENT_COMMAND_NAME,
        attempt.plan_hash,
        execution_kind="deterministic",
    )
    plan = ShadowLocalMeasurementPlan(
        claim, attempt.canonical_plan_json, tuple(_member_plan(member) for member in attempt.members)
    )
    if tuple(plan.members) != tuple(_member_plan(member) for member in attempt.members):
        raise StoreValidationError("shadow-local attempt members drift from its immutable plan")
    return plan


def _raw_responses(
    attempt: ShadowLocalMeasurementAttempt, raw_responses: Mapping[RawResponseKey, bytes]
) -> dict[RawResponseKey, bytes]:
    expected = {(member.member_ordinal, member.case_sha256): member for member in attempt.members}
    checked: dict[RawResponseKey, bytes] = {}
    for key, value in raw_responses.items():
        if type(key) is not tuple or len(key) != 2 or type(key[0]) is not int or type(key[1]) is not str:
            raise StoreValidationError("shadow-local raw response key is not exact ordinal/case identity")
        member = expected.get(key)
        if member is None or type(value) is not bytes or key in checked:  # noqa: E721
            raise StoreValidationError("shadow-local raw response identity is missing or substituted")
        if member.raw_blob is None or not 0 < len(value) <= member.max_response_bytes:
            raise StoreValidationError("shadow-local raw response violates its staged byte bound")
        if (
            _sha256(value) != member.raw_blob.content_hash
            or len(value) != member.raw_blob.byte_length
        ):
            raise StoreValidationError("shadow-local raw response does not match its staged BlobRef")
        checked[key] = value
    if set(checked) != set(expected):
        raise StoreValidationError("shadow-local raw responses do not cover the complete staged attempt")
    return checked


def _total_response_limit(plan: ShadowLocalMeasurementPlan) -> int:
    payload = _strict_object(plan.canonical_plan_json, "shadow-local plan")
    inputs = payload.get("shadow_local_inputs")
    if type(inputs) is not dict:  # noqa: E721
        raise StoreValidationError("shadow-local plan does not contain response limits")
    limits = cast(dict[str, object], inputs).get("limits")
    if type(limits) is not dict:  # noqa: E721
        raise StoreValidationError("shadow-local plan does not contain response limits")
    value = cast(dict[str, object], limits).get("max_total_response_bytes")
    if type(value) is not int or value <= 0:  # noqa: E721
        raise StoreValidationError("shadow-local plan max_total_response_bytes is invalid")
    return value


def _measurement_manifest(
    attempt: ShadowLocalMeasurementAttempt, plan: ShadowLocalMeasurementPlan
) -> ShadowLocalMeasurementManifest:
    try:
        rebuilt = ShadowLocalMeasurementManifest.from_mapping(
            {
                "schema_version": "shadow-local-measurement-manifest-v1",
                "members": [
                    {
                        "ordinal": member.member_ordinal,
                        "case": _strict_object(member.canonical_case_json, "shadow-local member case"),
                        "request": _strict_object(member.canonical_request_json, "shadow-local member request"),
                    }
                    for member in attempt.members
                ],
            }
        )
        plan_payload = _strict_object(plan.canonical_plan_json, "shadow-local plan")
        inputs = cast(dict[str, object], plan_payload["shadow_local_inputs"])
        persisted = ShadowLocalMeasurementManifest.from_mapping(inputs["manifest"])
        if _store_json(persisted.to_mapping()) != _store_json(rebuilt.to_mapping()):
            raise StoreValidationError("shadow-local persisted manifest drifts from its immutable members")
        return rebuilt
    except ShadowLocalMeasurementSetError as error:
        raise StoreValidationError("shadow-local staged members cannot rebuild the pure manifest") from error


@dataclass(frozen=True, slots=True)
class CompiledShadowLocalMeasurementArtifacts:
    """The only unaccepted, two-member artifact pair for a local attempt."""

    manifest_artifact: ArtifactMember
    results_artifact: ArtifactMember
    manifest: ShadowLocalMeasurementManifest
    results: ShadowLocalMeasurementResults
    report: ShadowLocalMeasurementValidationReport

    @property
    def artifacts(self) -> tuple[ArtifactMember, ArtifactMember]:
        return self.manifest_artifact, self.results_artifact


@dataclass(frozen=True, slots=True)
class CommittedShadowLocalMeasurement:
    """Exact committed local evidence; intentionally carries no acceptance state."""

    attempt_id: UUID
    job_id: UUID
    command_slot_id: UUID
    receipt_id: UUID
    artifact_set_id: UUID
    request_hash: str
    manifest: ShadowLocalMeasurementManifest
    results: ShadowLocalMeasurementResults
    report: ShadowLocalMeasurementValidationReport


def validate_shadow_local_measurement_artifact_metadata(
    attempt: ShadowLocalMeasurementAttempt,
    manifest_payload_json: str,
    results_payload_json: str,
) -> None:
    """Close journal/artifact metadata before a Store reader opens raw bytes."""

    if any(
        member.state != "staged" or member.raw_blob is None or member.evidence_json is None
        for member in attempt.members
    ):
        raise StoreValidationError("shadow-local artifact metadata requires complete staged members")
    plan = _plan_for_attempt(attempt)
    manifest = _measurement_manifest(attempt, plan)
    expected_manifest = {
        "schema_version": SHADOW_LOCAL_MEASUREMENT_MANIFEST_ARTIFACT_SCHEMA,
        "measurement_request_sha256": attempt.plan_hash,
        "plan": _strict_object(attempt.canonical_plan_json, "shadow-local plan"),
        "manifest": manifest.to_mapping(),
    }
    actual_manifest = _strict_object(manifest_payload_json, "shadow-local manifest artifact")
    if _store_json(actual_manifest) != _store_json(expected_manifest):
        raise StoreValidationError("shadow-local manifest artifact does not close over the committed journal")
    expected_results = {
        "schema_version": "shadow-local-measurement-results-v1",
        "manifest_sha256": manifest.canonical_hash,
        "members": [
            {
                "ordinal": member.member_ordinal,
                "case_sha256": member.case_sha256,
                "request_sha256": member.request_sha256,
                "evidence": _strict_object(member.evidence_json or "", "shadow-local staged evidence"),
            }
            for member in attempt.members
        ],
    }
    expected_result_artifact = {
        "schema_version": SHADOW_LOCAL_MEASUREMENT_RESULTS_ARTIFACT_SCHEMA,
        "measurement_manifest_artifact_sha256": canonical_payload_hash(_store_json(expected_manifest)),
        "measurement_manifest_sha256": manifest.canonical_hash,
        "results": expected_results,
        "raw_responses": [
            {
                "ordinal": member.member_ordinal,
                "case_sha256": member.case_sha256,
                "request_sha256": member.request_sha256,
                "blob": _blob_mapping(cast(BlobRef, member.raw_blob)),
            }
            for member in attempt.members
        ],
    }
    actual_results = _strict_object(results_payload_json, "shadow-local results artifact")
    if _store_json(actual_results) != _store_json(expected_result_artifact):
        raise StoreValidationError("shadow-local results artifact does not close over staged raw metadata")


def compile_shadow_local_measurement_artifacts(
    attempt: ShadowLocalMeasurementAttempt,
    raw_responses: Mapping[RawResponseKey, bytes],
) -> CompiledShadowLocalMeasurementArtifacts:
    """Rebuild the exact artifact pair from persisted plan/staging/raw bytes only."""

    if type(attempt) is not ShadowLocalMeasurementAttempt:  # noqa: E721
        raise StoreValidationError("shadow-local artifact compiler requires an exact attempt")
    if attempt.state not in ("ready", "committed") or any(
        member.state != "staged" or member.raw_blob is None or member.evidence_json is None
        for member in attempt.members
    ):
        raise StoreValidationError("shadow-local artifact compiler requires every member staged")
    plan = _plan_for_attempt(attempt)
    responses = _raw_responses(attempt, raw_responses)
    if sum(len(raw) for raw in responses.values()) > _total_response_limit(plan):
        raise StoreValidationError("shadow-local staged responses exceed the frozen total byte budget")
    manifest = _measurement_manifest(attempt, plan)
    try:
        results = ShadowLocalMeasurementResults.from_mapping(
            {
                "schema_version": "shadow-local-measurement-results-v1",
                "manifest_sha256": manifest.canonical_hash,
                "members": [
                    {
                        "ordinal": member.member_ordinal,
                        "case_sha256": member.case_sha256,
                        "request_sha256": member.request_sha256,
                        "evidence": _strict_object(member.evidence_json or "", "shadow-local staged evidence"),
                    }
                    for member in attempt.members
                ],
            },
            manifest=manifest,
            raw_responses=responses,
        )
        report = ShadowLocalMeasurementValidationReport(results)
    except ShadowLocalMeasurementSetError as error:
        raise StoreValidationError("shadow-local staged evidence fails independent raw replay") from error

    summary = attempt.plan_hash.removeprefix("sha256:")
    scope = ArtifactScope("autocut_calibration", "shadow_local_run", summary)
    manifest_payload = {
        "schema_version": SHADOW_LOCAL_MEASUREMENT_MANIFEST_ARTIFACT_SCHEMA,
        "measurement_request_sha256": attempt.plan_hash,
        "plan": _strict_object(attempt.canonical_plan_json, "shadow-local plan"),
        "manifest": manifest.to_mapping(),
    }
    manifest_json = _store_json(manifest_payload)
    manifest_artifact = ArtifactMember(
        SHADOW_LOCAL_MEASUREMENT_MANIFEST_ARTIFACT_TYPE,
        f"shadow-local-measurement:{summary}:manifest",
        1,
        scope,
        canonical_payload_hash(manifest_json),
        manifest_json,
    )
    result_payload = {
        "schema_version": SHADOW_LOCAL_MEASUREMENT_RESULTS_ARTIFACT_SCHEMA,
        "measurement_manifest_artifact_sha256": manifest_artifact.content_hash,
        "measurement_manifest_sha256": manifest.canonical_hash,
        "results": results.to_mapping(),
        "raw_responses": [
            {
                "ordinal": member.member_ordinal,
                "case_sha256": member.case_sha256,
                "request_sha256": member.request_sha256,
                "blob": _blob_mapping(cast(BlobRef, member.raw_blob)),
            }
            for member in attempt.members
        ],
    }
    results_json = _store_json(result_payload)
    results_artifact = ArtifactMember(
        SHADOW_LOCAL_MEASUREMENT_RESULTS_ARTIFACT_TYPE,
        f"shadow-local-measurement:{summary}:results",
        1,
        scope,
        canonical_payload_hash(results_json),
        results_json,
    )
    return CompiledShadowLocalMeasurementArtifacts(
        manifest_artifact, results_artifact, manifest, results, report
    )
