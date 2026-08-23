"""Unit coverage for closed semantic persistence request objects."""

import hashlib
import json
from uuid import uuid4

import pytest
from autocut_kernel.store import (
    ArtifactMember,
    ArtifactScope,
    CommandClaim,
    CommandRejection,
    CommandSuccess,
    Job,
    StaleHeadError,
    StoreValidationError,
)
from autocut_kernel.store.postgres import PostgresRuntimeStore


def digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def test_command_claim_requires_canonical_digest_and_identity() -> None:
    with pytest.raises(StoreValidationError, match="request_hash"):
        CommandClaim(Job("fixture-job", "test"), "run-1", "preflight", "not-a-hash")


def test_success_requires_a_non_empty_set_with_bound_member_hash() -> None:
    member = ArtifactMember(
        artifact_type="media_evidence",
        logical_id="preflight",
        revision=1,
        scope=ArtifactScope("pipeline", "job", "fixture-job"),
        content_hash=digest("evidence"),
        payload_json='{"ready":true}',
    )
    with pytest.raises(StoreValidationError, match="set_hash must bind"):
        CommandSuccess(command_slot_id=uuid4(), set_hash=digest("wrong"), artifacts=(member,))


def test_success_accepts_exact_canonical_member_set_hash() -> None:
    member = ArtifactMember(
        artifact_type="media_evidence",
        logical_id="preflight",
        revision=1,
        scope=ArtifactScope("pipeline", "job", "fixture-job"),
        content_hash=digest("evidence"),
        payload_json=json.dumps({"ready": True}),
    )
    canonical = [
        {
            "artifact_type": member.artifact_type,
            "content_hash": member.content_hash,
            "logical_id": member.logical_id,
            "payload_json": {"ready": True},
            "revision": 1,
            "scope": {"key": "fixture-job", "kind": "job", "namespace": "pipeline"},
        }
    ]
    set_hash = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                canonical, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            ).encode()
        ).hexdigest()
    )
    success = CommandSuccess(command_slot_id=uuid4(), set_hash=set_hash, artifacts=(member,))
    assert success.expected_set_hash == set_hash


def test_terminal_rejection_requires_structured_failure_detail() -> None:
    with pytest.raises(StoreValidationError, match="failure_detail_json"):
        CommandRejection(uuid4(), "PRECHECK_DENY", "")


def test_first_head_unique_violation_maps_to_stable_error() -> None:
    class UniqueViolationError(Exception):
        sqlstate = "23505"

        def __str__(self) -> str:
            return "duplicate key runtime_artifacts_scope_revision_key"

    assert PostgresRuntimeStore._is_first_head_race(UniqueViolationError())
    assert isinstance(StaleHeadError("race"), Exception)
