"""Closed Store-contract coverage for the local shadow recovery sibling."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from uuid import uuid4

import pytest
from autocut_kernel.store.models import (
    SHADOW_LOCAL_CALIBRATION_MEASUREMENT_COMMAND_NAME,
    SHADOW_LOCAL_CALIBRATION_MEASUREMENT_PROTOCOL,
    BlobRef,
    CommandClaim,
    CommandOutcome,
    Job,
    ShadowLocalMeasurementAttempt,
    ShadowLocalMeasurementMember,
    ShadowLocalMeasurementMemberPlan,
    ShadowLocalMeasurementNotStartedProof,
    ShadowLocalMeasurementPlan,
    ShadowLocalMeasurementRetryAuthorization,
    ShadowLocalMeasurementTerminalDenialResult,
    StoreValidationError,
    canonical_payload_hash,
)


def _hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _media(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _store(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _member(ordinal: int = 0) -> ShadowLocalMeasurementMemberPlan:
    case = _media({"case": f"窗口-{ordinal}", "schema_version": "case-v1"})
    request = _media({"request": ordinal, "schema_version": "request-v1"})
    return ShadowLocalMeasurementMemberPlan(
        member_ordinal=ordinal,
        case_sha256=_hash(case),
        request_sha256=_hash(request),
        canonical_case_json=case,
        canonical_request_json=request,
        source_job_id=uuid4(),
        source_blob=BlobRef(uuid4(), _hash(f"source-{ordinal}"), ordinal + 1, "video/mp4"),
        source_blob_reference_sha256=_hash(f"source-ref-{ordinal}"),
        binding_sha256=_hash(f"binding-{ordinal}"),
        service_profile_sha256=_hash(f"profile-{ordinal}"),
        max_response_bytes=1024,
    )


def _plan() -> ShadowLocalMeasurementPlan:
    members = (_member(0), _member(1))
    payload = {
        "command": SHADOW_LOCAL_CALIBRATION_MEASUREMENT_COMMAND_NAME,
        "measurement_protocol": SHADOW_LOCAL_CALIBRATION_MEASUREMENT_PROTOCOL,
        "shadow_local_inputs": {
            "service_profile": {"schema_version": "shadow-local-service-profile-v1"},
            "manifest": {"schema_version": "shadow-local-measurement-manifest-v1"},
            "source_bindings": [member.source_binding_mapping() for member in members],
            "limits": {"max_total_response_bytes": 2048},
            "max_attempt_count": 2,
        },
        "corpus_members": [member.to_plan_mapping() for member in members],
    }
    canonical = _store(payload)
    request_hash = canonical_payload_hash(canonical)
    claim = CommandClaim(
        Job(f"shadow-local:{request_hash.removeprefix('sha256:')}", "shadow"),
        f"shadow-local-measurement:{request_hash.removeprefix('sha256:')}",
        SHADOW_LOCAL_CALIBRATION_MEASUREMENT_COMMAND_NAME,
        request_hash,
        execution_kind="deterministic",
    )
    return ShadowLocalMeasurementPlan(claim, canonical, members)


def _busy(member: ShadowLocalMeasurementMemberPlan) -> ShadowLocalMeasurementNotStartedProof:
    proof = _media(
        {
            "schema_version": "local-speech-window-busy-v1",
            "invocation_state": "not_started",
            "reason": "admission_busy",
            "request_sha256": member.request_sha256,
            "binding_sha256": member.binding_sha256,
            "service_profile_sha256": member.service_profile_sha256,
        }
    )
    raw = proof.encode("utf-8")
    return ShadowLocalMeasurementNotStartedProof(
        raw_bytes=raw,
        content_hash="sha256:" + hashlib.sha256(raw).hexdigest(),
        media_type="application/json",
        proof_json=proof,
    )


def test_plan_closes_exact_store_and_media_hash_domains() -> None:
    plan = _plan()

    assert plan.claim.request_hash == canonical_payload_hash(plan.canonical_plan_json)
    assert "\\u7a97" in plan.members[0].canonical_case_json
    assert "窗口" in plan.canonical_plan_json


def test_plan_rejects_unknown_or_drifting_member_fields() -> None:
    plan = _plan()
    wire = json.loads(plan.canonical_plan_json)
    wire["unexpected"] = True
    canonical = _store(wire)
    request_hash = canonical_payload_hash(canonical)
    changed_claim = CommandClaim(
        Job(f"shadow-local:{request_hash.removeprefix('sha256:')}", "shadow"),
        f"shadow-local-measurement:{request_hash.removeprefix('sha256:')}",
        SHADOW_LOCAL_CALIBRATION_MEASUREMENT_COMMAND_NAME,
        request_hash,
        execution_kind="deterministic",
    )
    with pytest.raises(StoreValidationError, match="shape"):
        ShadowLocalMeasurementPlan(
            changed_claim,
            canonical,
            plan.members,
        )

    drifted = replace(plan.members[0], max_response_bytes=2048)
    with pytest.raises(StoreValidationError, match="drifts"):
        ShadowLocalMeasurementPlan(
            plan.claim,
            plan.canonical_plan_json,
            (drifted, plan.members[1]),
        )

    bool_wire = json.loads(plan.canonical_plan_json)
    bool_wire["corpus_members"][0]["max_response_bytes"] = False
    bool_canonical = _store(bool_wire)
    bool_hash = canonical_payload_hash(bool_canonical)
    bool_claim = CommandClaim(
        Job(f"shadow-local:{bool_hash.removeprefix('sha256:')}", "shadow"),
        f"shadow-local-measurement:{bool_hash.removeprefix('sha256:')}",
        SHADOW_LOCAL_CALIBRATION_MEASUREMENT_COMMAND_NAME,
        bool_hash,
        execution_kind="deterministic",
    )
    with pytest.raises(StoreValidationError, match="drifts"):
        ShadowLocalMeasurementPlan(bool_claim, bool_canonical, plan.members)


def test_member_rejects_cross_domain_hash_and_boolean_limit() -> None:
    member = _member()
    with pytest.raises(StoreValidationError, match="media hashes"):
        replace(member, case_sha256=_hash("store-domain-not-media"))
    with pytest.raises(StoreValidationError, match="max_response_bytes"):
        replace(member, max_response_bytes=True)


def test_local_member_states_and_attempt_require_exact_typed_values() -> None:
    plan = _plan()
    member_plan = plan.members[0]
    member = ShadowLocalMeasurementMember(
        attempt_id=uuid4(),
        case_sha256=member_plan.case_sha256,
        request_sha256=member_plan.request_sha256,
        member_ordinal=member_plan.member_ordinal,
        canonical_case_json=member_plan.canonical_case_json,
        canonical_request_json=member_plan.canonical_request_json,
        source_job_id=member_plan.source_job_id,
        source_blob=member_plan.source_blob,
        source_blob_reference_sha256=member_plan.source_blob_reference_sha256,
        binding_sha256=member_plan.binding_sha256,
        service_profile_sha256=member_plan.service_profile_sha256,
        max_response_bytes=member_plan.max_response_bytes,
        state="not_started",
        version=0,
        busy_proof_blob=BlobRef(
            uuid4(),
            _busy(member_plan).content_hash,
            len(_busy(member_plan).raw_bytes),
            _busy(member_plan).media_type,
        ),
        busy_proof_json=_busy(member_plan).proof_json,
    )
    assert member.state == "not_started"
    with pytest.raises(StoreValidationError, match="state"):
        replace(member, state="retrying")
    with pytest.raises(StoreValidationError, match="BUSY proof"):
        replace(member, busy_proof_json=_media({"invalid": "proof"}))

    outcome = CommandOutcome(uuid4(), "running", job_id=uuid4())
    attempt = ShadowLocalMeasurementAttempt(
        attempt_id=member.attempt_id,
        command_slot_id=outcome.command_slot_id,
        job=plan.claim.job,
        plan_hash=plan.claim.request_hash,
        canonical_plan_json=plan.canonical_plan_json,
        attempt_ordinal=1,
        previous_attempt_id=None,
        state="prepared",
        version=0,
        members=(member,),
        outcome=outcome,
    )
    assert attempt.members == (member,)
    denied_outcome = CommandOutcome(outcome.command_slot_id, "denied", job_id=outcome.job_id)
    denied_attempt = replace(attempt, state="denied", outcome=denied_outcome)
    assert ShadowLocalMeasurementTerminalDenialResult(denied_attempt, denied_outcome).outcome.state == "denied"
    with pytest.raises(StoreValidationError, match="exact members"):
        replace(attempt, members=(object(),))


def test_retry_authorization_binds_predecessor_and_member_identity() -> None:
    plan = _plan()
    authorization = ShadowLocalMeasurementRetryAuthorization(
        decision_reference_sha256=_hash("retry"),
        predecessor_plan_hash=plan.claim.request_hash,
        predecessor_attempt_id=uuid4(),
        predecessor_version=3,
        member_case_sha256=plan.members[1].case_sha256,
        next_attempt_ordinal=2,
        reason_code="REQUEST_NOT_STARTED",
    )
    assert authorization.next_attempt_ordinal == 2
    with pytest.raises(StoreValidationError, match="reason"):
        replace(authorization, reason_code="unknown")
    with pytest.raises(StoreValidationError, match="ordinal"):
        replace(authorization, next_attempt_ordinal=1)
