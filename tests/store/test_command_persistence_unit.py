"""Unit coverage for closed semantic persistence request objects."""

import hashlib
import json
from uuid import uuid4

import pytest
from autocut_kernel.store import (
    ArtifactMember,
    ArtifactScope,
    BlobRef,
    CommandClaim,
    CommandOutcome,
    CommandRejection,
    CommandSuccess,
    GenerationAttempt,
    Job,
    MediaEvidenceReference,
    PersistedMediaEvidence,
    PersistedMediaOutputs,
    PersistedRecipe,
    PersistenceConflictError,
    RecipeReference,
    RuntimeStoreError,
    StaleHeadError,
    StoreValidationError,
)
from autocut_kernel.store.postgres import PostgresRuntimeStore
from psycopg import ProgrammingError


def digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def test_command_claim_requires_canonical_digest_and_identity() -> None:
    with pytest.raises(StoreValidationError, match="request_hash"):
        CommandClaim(Job("fixture-job", "test"), "run-1", "preflight", "not-a-hash", execution_kind="deterministic")


def test_command_outcome_defaults_to_a_replay_claim() -> None:
    outcome = CommandOutcome(command_slot_id=uuid4(), state="running")

    assert outcome.is_fresh_claim is False


def test_blob_ref_exposes_only_kernel_identity_and_rejects_invalid_length() -> None:
    reference = BlobRef(uuid4(), digest("raw"), 3, "application/json")

    assert set(reference.__dataclass_fields__) == {
        "object_id",
        "content_hash",
        "byte_length",
        "media_type",
    }
    with pytest.raises(StoreValidationError, match="byte_length"):
        BlobRef(uuid4(), digest("raw"), -1, "application/json")


def test_generation_attempt_closes_state_dependent_blob_and_receipt_shape() -> None:
    attempt_id, job_id, slot_id = uuid4(), uuid4(), uuid4()
    blob = BlobRef(uuid4(), digest("raw"), 3, "application/json")
    request_blob = BlobRef(uuid4(), digest("request-payload"), 7, "application/json")
    responded = GenerationAttempt(
        attempt_id,
        job_id,
        slot_id,
        digest("request"),
        "provider-test",
        "provider-idempotency-1",
        request_blob,
        "responded",
        2,
        "provider-request-1",
        blob,
    )
    assert responded.raw_response == blob
    assert responded.request_payload == request_blob

    with pytest.raises(StoreValidationError, match="raw-response BlobRef"):
        GenerationAttempt(
            attempt_id,
            job_id,
            slot_id,
            digest("request"),
            "provider-test",
            "provider-idempotency-1",
            request_blob,
            "responded",
            2,
        )
    with pytest.raises(StoreValidationError, match="receipt and artifact set"):
        GenerationAttempt(
            attempt_id,
            job_id,
            slot_id,
            digest("request"),
            "provider-test",
            "provider-idempotency-1",
            request_blob,
            "committed",
            3,
            raw_response=blob,
        )


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


def test_persisted_recipe_revalidates_canonical_json_against_its_identity() -> None:
    payload = '{"recipe":{"span":{"end_pts":4,"start_pts":1}}}'
    reference = RecipeReference(
        scope=ArtifactScope("pipeline", "job", "fixture-job"),
        logical_id="recipe",
        revision=1,
        content_hash=digest(
            json.dumps(
                json.loads(payload), ensure_ascii=False, separators=(",", ":"), sort_keys=True
            )
        ),
    )

    job_id = uuid4()
    result = PersistedRecipe(reference, payload, job_id, uuid4(), uuid4(), uuid4())

    assert result.reference == reference
    assert result.job_id == job_id


def test_persisted_recipe_refuses_payload_hash_mismatch() -> None:
    reference = RecipeReference(
        scope=ArtifactScope("pipeline", "job", "fixture-job"),
        logical_id="recipe",
        revision=1,
        content_hash=digest("different payload"),
    )

    with pytest.raises(StoreValidationError, match="content_hash"):
        PersistedRecipe(reference, "{}", uuid4(), uuid4(), uuid4(), uuid4())


def test_persisted_media_evidence_retains_exact_source_evidence_json() -> None:
    payload = '{"source":{"byte_size":42,"sha256":"sha256:' + "a" * 64 + '"}}'
    reference = MediaEvidenceReference(
        scope=ArtifactScope("pipeline", "job", "fixture-job"),
        logical_id="media_evidence",
        revision=1,
        content_hash=digest(
            json.dumps(
                json.loads(payload), ensure_ascii=False, separators=(",", ":"), sort_keys=True
            )
        ),
    )

    result = PersistedMediaEvidence(reference, payload, uuid4(), uuid4(), uuid4(), uuid4())

    assert json.loads(result.payload_json)["source"] == {
        "byte_size": 42,
        "sha256": "sha256:" + "a" * 64,
    }


def test_persisted_media_evidence_refuses_payload_hash_mismatch() -> None:
    reference = MediaEvidenceReference(
        scope=ArtifactScope("pipeline", "job", "fixture-job"),
        logical_id="media_evidence",
        revision=1,
        content_hash=digest("different payload"),
    )

    with pytest.raises(StoreValidationError, match="media evidence content_hash"):
        PersistedMediaEvidence(reference, "{}", uuid4(), uuid4(), uuid4(), uuid4())


def test_persisted_media_outputs_require_shared_scope_and_uuid_provenance() -> None:
    scope = ArtifactScope("pipeline", "job", "fixture-job")
    evidence = MediaEvidenceReference(scope, "media_evidence", 1, digest('{"evidence":true}'))
    recipe = RecipeReference(scope, "recipe", 1, digest('{"recipe":true}'))
    outputs = PersistedMediaOutputs(evidence, recipe, uuid4(), uuid4(), uuid4(), uuid4())
    assert outputs.media_evidence == evidence
    with pytest.raises(StoreValidationError, match="share one artifact scope"):
        PersistedMediaOutputs(
            evidence,
            RecipeReference(ArtifactScope("pipeline", "job", "other"), "recipe", 1, digest('{"recipe":true}')),
            uuid4(),
            uuid4(),
            uuid4(),
            uuid4(),
        )


def test_terminal_rejection_requires_structured_failure_detail() -> None:
    with pytest.raises(StoreValidationError, match="failure_detail_json"):
        CommandRejection(uuid4(), "PRECHECK_DENY", "")


def test_terminal_rejection_requires_valid_json_failure_detail() -> None:
    with pytest.raises(StoreValidationError, match="failure_detail_json must contain JSON"):
        CommandRejection(uuid4(), "PRECHECK_DENY", "not json")


@pytest.mark.parametrize("outcome", ("success", "running", "", None))
def test_rejection_rejects_non_terminal_outcomes(outcome: object) -> None:
    with pytest.raises(StoreValidationError, match="outcome must be 'denied' or 'failed'"):
        CommandRejection(uuid4(), "PRECHECK_DENY", "{}", outcome=outcome)  # type: ignore[arg-type]


@pytest.mark.parametrize("slot_id", ("not-a-uuid", 1, None))
def test_rejection_requires_a_uuid_slot(slot_id: object) -> None:
    with pytest.raises(StoreValidationError, match="command_slot_id must be a UUID"):
        CommandRejection(slot_id, "PRECHECK_DENY", "{}")  # type: ignore[arg-type]


@pytest.mark.parametrize("detail", ('{"value": NaN}', '{"value": Infinity}', "[]", '"text"', "null"))
def test_rejection_requires_finite_json_object(detail: str) -> None:
    with pytest.raises(StoreValidationError):
        CommandRejection(uuid4(), "PRECHECK_DENY", detail)


def test_rejection_supports_failed_as_well_as_denied_outcome() -> None:
    rej = CommandRejection(uuid4(), "RUNTIME_CRASH", '{"reason":"unexpected"}', outcome="failed")
    assert rej.outcome == "failed"
    assert rej.failure_code == "RUNTIME_CRASH"


class _UniqueViolationError(Exception):
    sqlstate = "23505"

    def __init__(self, constraint_name: str) -> None:
        self.diag = type("Diagnostic", (), {"constraint_name": constraint_name})()


class _UniqueViolationCursor:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def execute(self, query: str, params: tuple[object, ...] = ()) -> None:
        raise self._error

    def fetchone(self) -> None:
        return None

    def close(self) -> None:
        pass


class _UniqueViolationConnection:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def cursor(self) -> _UniqueViolationCursor:
        return _UniqueViolationCursor(self._error)

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


def _store_with_unique_violation(constraint_name: str) -> PostgresRuntimeStore:
    error = _UniqueViolationError(constraint_name)
    return PostgresRuntimeStore(lambda: _UniqueViolationConnection(error))


def test_first_head_unique_violation_maps_to_stale_head_error() -> None:
    store = _store_with_unique_violation("runtime_artifacts_scope_revision_key")

    with pytest.raises(StaleHeadError, match="logical artifact head"):
        store.claim_command(CommandClaim(Job("unique-head", "test"), "cmd", "x", digest("x"), execution_kind="deterministic"))


@pytest.mark.parametrize(
    "constraint_name",
    ("runtime_command_slots_job_id_idempotency_key_key", "runtime_other_unique_key"),
)
def test_other_unique_violations_map_to_persistence_conflict_error(constraint_name: str) -> None:
    store = _store_with_unique_violation(constraint_name)

    with pytest.raises(PersistenceConflictError, match="uniqueness constraint"):
        store.claim_command(CommandClaim(Job("unique-other", "test"), "cmd", "x", digest("x"), execution_kind="deterministic"))


def test_programming_database_errors_are_mapped_to_runtime_store_error() -> None:
    class Cursor:
        def execute(self, query: str, params: tuple[object, ...] = ()) -> None:
            raise ProgrammingError("broken SQL")

        def fetchone(self) -> None:
            return None

        def close(self) -> None:
            pass

    class Connection:
        def cursor(self) -> Cursor:
            return Cursor()

        def commit(self) -> None:
            pass

        def rollback(self) -> None:
            pass

        def close(self) -> None:
            pass

    store = PostgresRuntimeStore(Connection)
    with pytest.raises(RuntimeStoreError, match="database operation failed"):
        store.claim_command(CommandClaim(Job("programming-error", "test"), "cmd", "x", digest("x"), execution_kind="deterministic"))


def test_store_decodes_bytes_text_values_from_database_rows() -> None:
    job_id = uuid4()
    slot_id = uuid4()
    receipt_id = uuid4()
    artifact_set_id = uuid4()
    request_hash = digest("request")
    member = ArtifactMember(
        artifact_type="media_evidence",
        logical_id="preflight",
        revision=2,
        scope=ArtifactScope("pipeline", "job", "bytes-job"),
        content_hash=digest("evidence"),
        payload_json='{"ready":true}',
    )
    canonical = [{
        "artifact_type": member.artifact_type,
        "content_hash": member.content_hash,
        "logical_id": member.logical_id,
        "payload_json": {"ready": True},
        "revision": member.revision,
        "scope": {"key": "bytes-job", "kind": "job", "namespace": "pipeline"},
    }]
    set_hash = digest(json.dumps(canonical, ensure_ascii=False, separators=(",", ":"), sort_keys=True))

    class Cursor:
        def __init__(self) -> None:
            self.rows = iter(
                (
                    None,
                    (job_id, b"test"),
                    (b"running",),
                    (slot_id, b"preflight", request_hash.encode(), b"deterministic"),
                    (b"running", None, None, None, None),
                    (job_id,),
                    (b"running",),
                    (job_id, b"running", b"preflight", request_hash.encode()),
                    (b"deterministic",),
                    (b"1",),
                    (job_id,),
                    (b"running",),
                    (job_id, b"succeeded", b"preflight", request_hash.encode()),
                    (b"deterministic",),
                    (b"succeeded", receipt_id, artifact_set_id, None, None),
                    (set_hash.encode(),),
                )
            )

        def execute(self, query: str, params: tuple[object, ...] = ()) -> None:
            pass

        def fetchone(self) -> tuple[object, ...] | None:
            return next(self.rows)

        def close(self) -> None:
            pass

    class Connection:
        def __init__(self) -> None:
            self.cursor_instance = Cursor()

        def cursor(self) -> Cursor:
            return self.cursor_instance

        def commit(self) -> None:
            pass

        def rollback(self) -> None:
            pass

        def close(self) -> None:
            pass

    connection = Connection()
    store = PostgresRuntimeStore(lambda: connection)
    claim = CommandClaim(Job("bytes-job", "test"), "claim", "preflight", request_hash, execution_kind="deterministic")

    assert store.claim_command(claim).state == "running"
    success = CommandSuccess(slot_id, set_hash, (member,))
    assert store.commit_command_success(success).state == "succeeded"
    replay = store.commit_command_success(success)

    assert replay.state == "succeeded"
    assert replay.artifact_set_id == artifact_set_id
