"""Serial verification-DB tests for the dedicated CalibrationRecord Store seam."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from autocut_kernel.media.calibration_record import (
    CALIBRATION_VALIDATOR_COMMAND,
    CalibrationRecordArtifactSet,
    CalibrationRecordRole,
    build_calibration_record_candidate,
    validator_internal_assemble_accepted_artifact_set,
)
from autocut_kernel.pipeline import MeasureShadowCalibrationCommand
from autocut_kernel.registry.authority_profiles import LocalRunCalibration
from autocut_kernel.store import (
    ArtifactMember,
    ArtifactScope,
    BlobIntegrityError,
    BlobUnavailableError,
    CalibrationValidationBinding,
    CommandOutcome,
    CommandRejection,
    CommandStateError,
    CommandSuccess,
    CommittedArtifactMemberReference,
    IdempotencyConflictError,
    Job,
    MediaEvidenceUnavailableError,
    PostgresRuntimeStore,
    StoreValidationError,
)
from autocut_kernel.store.models import canonical_payload_hash

from tests.media.test_calibration_record_persistence import (
    _child,
    _identity,
    _proof,
    _runtime_measurement,
)
from tests.pipeline.test_measure_shadow_calibration_command import _member_port, _request

psycopg = pytest.importorskip("psycopg")
VERIFY_POSTGRES_DSN = "postgresql://ac_user:ac_password_2026@127.0.0.1:5433/ac_autocut_verify"
MIGRATIONS = Path("packages/autocut-kernel/migrations")


def _hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
def store() -> PostgresRuntimeStore:
    try:
        connection = psycopg.connect(VERIFY_POSTGRES_DSN, autocommit=True)
    except psycopg.OperationalError:
        pytest.skip("disposable authority PostgreSQL is unavailable")
    with connection, connection.cursor() as cursor:
        if connection.info.dbname != "ac_autocut_verify":
            pytest.fail("calibration Store tests may reset only ac_autocut_verify")
        cursor.execute("DROP SCHEMA IF EXISTS storage CASCADE")
        cursor.execute("DROP SCHEMA IF EXISTS runtime CASCADE")
        for migration in sorted(MIGRATIONS.glob("*.sql")):
            cursor.execute(migration.read_text(encoding="utf-8"))
    return PostgresRuntimeStore(lambda: psycopg.connect(VERIFY_POSTGRES_DSN))


def _references(outcome: CommandOutcome) -> tuple[CommittedArtifactMemberReference, ...]:
    assert outcome.receipt_id is not None and outcome.artifact_set_id is not None
    with psycopg.connect(VERIFY_POSTGRES_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT member.ordinal, artifact.namespace, artifact.scope_kind, artifact.scope_key,
                   artifact.artifact_type, artifact.logical_id, artifact.revision, artifact.content_hash
              FROM runtime.artifact_set_members AS member
              JOIN runtime.artifacts AS artifact ON artifact.artifact_id = member.artifact_id
             WHERE member.artifact_set_id = %s ORDER BY member.ordinal
            """,
            (outcome.artifact_set_id,),
        )
        return tuple(
            CommittedArtifactMemberReference(
                outcome.receipt_id, outcome.artifact_set_id, row[0],
                ArtifactScope(row[1], row[2], row[3]), *row[4:],
            )
            for row in cursor.fetchall()
        )


def _binding(store: PostgresRuntimeStore) -> CalibrationValidationBinding:
    request = _request()
    outcome = MeasureShadowCalibrationCommand(store, _member_port(request)).execute(request)
    assert outcome.state == "succeeded"
    manifest, results = _references(outcome)
    return CalibrationValidationBinding(
        "1", request.shadow_inputs.profile_source_sha256,
        request.shadow_inputs.registry_snapshot_sha256, manifest, results, "validation-attempt:1",
    )


def _record(binding: CalibrationValidationBinding) -> CalibrationRecordArtifactSet:
    """Real closed media DTOs; the independent raw validator is tested in Pipeline."""
    identity = replace(
        _identity(), profile_source_sha256=binding.profile_source_sha256,
        registry_snapshot_sha256=binding.registry_snapshot_sha256,
        runtime_measurement_identity_sha256=(
            None
            if binding.runtime_measurement_identity is None
            else binding.runtime_measurement_identity.canonical_sha256
        ),
    )
    candidate = build_calibration_record_candidate(
        profile_version=binding.profile_version, identity=identity,
        measurement_manifest_sha256=binding.manifest_reference.content_hash,
        measurement_results_sha256=binding.results_reference.content_hash,
        asr=_child(CalibrationRecordRole.ASR, identity=identity),
        vad=_child(CalibrationRecordRole.VAD, identity=identity),
        runtime_capability_id=(
            None
            if binding.runtime_measurement_identity is None
            else binding.runtime_measurement_identity.runtime_capability_id
        ),
    )
    return validator_internal_assemble_accepted_artifact_set(_proof(candidate))


def _success(slot: UUID, record: CalibrationRecordArtifactSet) -> CommandSuccess:
    artifacts = tuple(
        ArtifactMember(
            member.artifact_type, member.logical_id, member.revision,
            ArtifactScope(member.scope.namespace, member.scope.kind, member.scope.key),
            member.content_hash, member.payload_json,
        )
        for member in record.members
    )
    return _artifact_success(slot, artifacts)


def _artifact_success(slot: UUID, artifacts: tuple[ArtifactMember, ...]) -> CommandSuccess:
    value = [{
        "artifact_type": member.artifact_type, "content_hash": member.content_hash,
        "logical_id": member.logical_id, "payload_json": json.loads(member.payload_json),
        "revision": member.revision,
        "scope": {"namespace": member.scope.namespace, "kind": member.scope.kind, "key": member.scope.key},
    } for member in artifacts]
    return CommandSuccess(slot, canonical_payload_hash(json.dumps(value)), artifacts)


def _authority_counts(binding: CalibrationValidationBinding) -> tuple[int, int, int, str]:
    with psycopg.connect(VERIFY_POSTGRES_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT (SELECT count(*) FROM runtime.artifact_sets WHERE job_id = job.job_id),
                   (SELECT count(*) FROM runtime.command_receipts AS receipt
                     JOIN runtime.command_slots AS slot USING (command_slot_id) WHERE slot.job_id = job.job_id),
                   (SELECT count(*) FROM runtime.calibration_record_anchors WHERE scope_key = %s), job.state
              FROM runtime.jobs AS job WHERE job.job_key = %s
            """,
            (binding.profile_key, binding.job.job_key),
        )
        return cursor.fetchone()


def test_binding_hash_closes_all_refs_but_not_retry_attempt_key(store: PostgresRuntimeStore) -> None:
    binding = _binding(store)
    assert binding.job == Job("autocut_calibration_validator:shadow_calibration@1", "authority")
    assert binding.claim.command_name == CALIBRATION_VALIDATOR_COMMAND
    retry = replace(binding, attempt_idempotency_key="validation-attempt:2")
    assert retry.request_hash == binding.request_hash
    for changed in (
        replace(binding, profile_source_sha256=_hash("other-profile")),
        replace(binding, registry_snapshot_sha256=_hash("other-registry")),
        replace(binding, manifest_reference=replace(binding.manifest_reference, content_hash=_hash("other-manifest"))),
    ):
        assert changed.request_hash != binding.request_hash
    with pytest.raises(StoreValidationError):
        replace(binding, results_reference=replace(binding.results_reference, artifact_set_id=uuid4()))


@pytest.mark.parametrize("returned_row", (None, (None,), (b"tampered",)))
def test_claimed_blob_unavailable_is_distinct_from_invalid_reference(
    store: PostgresRuntimeStore, monkeypatch: pytest.MonkeyPatch, returned_row: object
) -> None:
    job = Job("blob-classification", "shadow")
    content = b"exact raw bytes"
    blob = store.put_immutable_blob(
        job, content=content, media_type="application/json", content_hash=_hash(content.decode())
    )
    assert store.read_immutable_blob(job, blob) == content
    with pytest.raises(BlobIntegrityError):
        store.read_immutable_blob(job, replace(blob, object_id=uuid4()))
    with pytest.raises(BlobIntegrityError):
        store.read_immutable_blob(job, replace(blob, byte_length=blob.byte_length + 1))
    with pytest.raises(BlobIntegrityError):
        store.read_immutable_blob(Job("missing-owner", "shadow"), blob)

    original_transaction = store._transaction

    class ReadFaultCursor:
        def __init__(self, cursor):
            self.cursor = cursor
            self.bytes_query = False

        def execute(self, query, params=None):
            self.bytes_query = "SELECT content_bytes FROM storage.blob_objects" in query
            return self.cursor.execute(query, params)

        def fetchone(self):
            return returned_row if self.bytes_query else self.cursor.fetchone()

    def fault_transaction(operation):
        return original_transaction(lambda cursor: operation(ReadFaultCursor(cursor)))

    monkeypatch.setattr(store, "_transaction", fault_transaction)
    expected = BlobIntegrityError if returned_row == (b"tampered",) else BlobUnavailableError
    with pytest.raises(expected):
        store.read_immutable_blob(job, blob)


def test_exact_measurement_reader_returns_owner_and_full_v3_pair(store: PostgresRuntimeStore) -> None:
    binding = _binding(store)
    measurement = store.read_committed_shadow_calibration_measurement(binding)
    assert measurement.manifest.reference == binding.manifest_reference
    assert measurement.results.reference == binding.results_reference
    assert measurement.job == Job(measurement.request_hash.removeprefix("sha256:"), "shadow")
    payload = json.loads(measurement.manifest.payload_json)
    assert payload["schema_version"] == "shadow-calibration-measurement-manifest-v3"
    assert payload["native_invocations"][0]["raw_context"]
    with pytest.raises(StoreValidationError, match="binding"):
        store.read_committed_shadow_calibration_measurement(
            replace(binding, profile_source_sha256=_hash("wrong-profile"))
        )


def test_measurement_outcome_reader_returns_durable_pair_and_same_receipt_replay(
    store: PostgresRuntimeStore,
) -> None:
    request = _request()
    command = MeasureShadowCalibrationCommand(store, _member_port(request))
    outcome = command.execute(request)
    expected = {
        "expected_request_sha256": request.request_hash,
        "expected_profile_source_sha256": request.shadow_inputs.profile_source_sha256,
        "expected_registry_snapshot_sha256": request.shadow_inputs.registry_snapshot_sha256,
    }
    measurement = store.read_shadow_calibration_measurement_outcome(request.job, outcome, **expected)
    refs = _references(outcome)
    assert (measurement.manifest.reference, measurement.results.reference) == refs
    assert measurement.job == request.job
    assert measurement.request_hash == request.request_hash
    assert measurement.command_slot_id == outcome.command_slot_id
    binding = CalibrationValidationBinding(
        "1", expected["expected_profile_source_sha256"], expected["expected_registry_snapshot_sha256"],
        refs[0], refs[1], "reader-comparison",
    )
    assert store.read_committed_shadow_calibration_measurement(binding) == measurement
    replay = command.execute(request)
    assert replay.receipt_id == outcome.receipt_id
    assert store.read_shadow_calibration_measurement_outcome(request.job, replay, **expected) == measurement
    with psycopg.connect(VERIFY_POSTGRES_DSN) as connection, connection.cursor() as cursor:
        cursor.execute("DELETE FROM runtime.logical_heads WHERE job_id = %s", (outcome.job_id,))
    assert store.read_shadow_calibration_measurement_outcome(
        request.job, replace(outcome, job_id=None), **expected
    ) == measurement


@pytest.mark.parametrize("drift", (
    "job", "job-profile", "request", "receipt", "set", "slot", "job-id", "profile", "registry",
))
def test_measurement_outcome_reader_rejects_identity_substitution(
    store: PostgresRuntimeStore, drift: str,
) -> None:
    request = _request()
    outcome = MeasureShadowCalibrationCommand(store, _member_port(request)).execute(request)
    job = request.job
    expected = {
        "expected_request_sha256": request.request_hash,
        "expected_profile_source_sha256": request.shadow_inputs.profile_source_sha256,
        "expected_registry_snapshot_sha256": request.shadow_inputs.registry_snapshot_sha256,
    }
    if drift == "job":
        job = Job("other-job", "shadow")
    elif drift == "job-profile":
        job = Job(job.job_key, "authority")
    elif drift == "request":
        expected["expected_request_sha256"] = _hash("other-request")
        job = Job(expected["expected_request_sha256"].removeprefix("sha256:"), "shadow")
    elif drift in {"profile", "registry"}:
        key = "expected_profile_source_sha256" if drift == "profile" else "expected_registry_snapshot_sha256"
        expected[key] = _hash("other-identity")
    else:
        field = {"receipt": "receipt_id", "set": "artifact_set_id", "slot": "command_slot_id", "job-id": "job_id"}[drift]
        outcome = replace(outcome, **{field: uuid4()})
    with pytest.raises((StoreValidationError, MediaEvidenceUnavailableError)):
        store.read_shadow_calibration_measurement_outcome(job, outcome, **expected)


@pytest.mark.parametrize("changes", (
    {"state": "running"}, {"state": "failed"}, {"state": "denied"}, {"state": True},
    {"receipt_id": None}, {"artifact_set_id": None}, {"command_slot_id": None},
    {"receipt_id": "not-a-uuid"}, {"artifact_set_id": True}, {"command_slot_id": 1.0},
    {"job_id": "not-a-uuid"}, {"is_fresh_claim": True}, {"is_fresh_claim": 0},
    {"failure_code": "FAILED"}, {"failure_detail_json": "{}"},
))
def test_measurement_outcome_reader_rejects_non_exact_success_before_database(changes) -> None:
    def no_database():
        pytest.fail("malformed outcome must be rejected before opening a connection")

    request = _request()
    outcome = replace(CommandOutcome(uuid4(), "succeeded", receipt_id=uuid4(), artifact_set_id=uuid4()), **changes)
    with pytest.raises(StoreValidationError):
        PostgresRuntimeStore(no_database).read_shadow_calibration_measurement_outcome(
            request.job, outcome, expected_request_sha256=request.request_hash,
            expected_profile_source_sha256=request.shadow_inputs.profile_source_sha256,
            expected_registry_snapshot_sha256=request.shadow_inputs.registry_snapshot_sha256,
        )


@pytest.mark.parametrize("field", (
    "expected_request_sha256", "expected_profile_source_sha256", "expected_registry_snapshot_sha256",
))
@pytest.mark.parametrize("value", ("sha256:" + "0" * 64, "invalid", True, 1.0))
def test_measurement_outcome_reader_rejects_invalid_expected_hash_before_database(field, value) -> None:
    def no_database():
        pytest.fail("malformed identity must be rejected before opening a connection")

    request = _request()
    expected = {
        "expected_request_sha256": request.request_hash,
        "expected_profile_source_sha256": request.shadow_inputs.profile_source_sha256,
        "expected_registry_snapshot_sha256": request.shadow_inputs.registry_snapshot_sha256,
        field: value,
    }
    outcome = CommandOutcome(uuid4(), "succeeded", receipt_id=uuid4(), artifact_set_id=uuid4())
    with pytest.raises(StoreValidationError):
        PostgresRuntimeStore(no_database).read_shadow_calibration_measurement_outcome(request.job, outcome, **expected)


def test_success_is_atomic_terminal_and_replay_checks_anchor(store: PostgresRuntimeStore) -> None:
    binding = _binding(store)
    record = _record(binding)
    claim = store.claim_command(binding.claim)
    success = _success(claim.command_slot_id, record)
    outcome = store.commit_calibration_record_validation_success(success, binding, record)
    assert _authority_counts(binding) == (1, 1, 1, "succeeded")
    assert store.claim_command(binding.claim).receipt_id == outcome.receipt_id
    assert store.commit_calibration_record_validation_success(success, binding, record) == outcome
    references = _references(outcome)
    calibration = LocalRunCalibration(
        references[0], references[3], record.members[1].content_hash, record.members[2].content_hash,
        record.aggregate.asr_accepted_bound_tick, record.aggregate.vad_accepted_bound_tick,
    )
    # A LocalRun consumer has only these accepted references and its predecessor
    # profile hashes: it need not reconstruct either measurement UUID or retry key.
    anchor = store.read_calibration_record_anchor(
        calibration.record_ref, calibration.validation_receipt_ref,
        expected_profile_source_sha256=record.aggregate.identity.profile_source_sha256,
        expected_registry_snapshot_sha256=record.aggregate.identity.registry_snapshot_sha256,
    )
    assert anchor.record == record
    assert anchor.record.asr.content_hash == calibration.asr_producer_record_sha256
    assert anchor.record.vad.content_hash == calibration.vad_producer_record_sha256
    assert anchor.record.aggregate.asr_accepted_bound_tick == calibration.asr_timing_error_bound_tick
    assert anchor.record.aggregate.vad_accepted_bound_tick == calibration.vad_timing_error_bound_tick
    assert anchor.aggregate.reference == references[0]
    assert anchor.validation.reference == references[3]
    assert anchor.record_sha256 == record.members[0].content_hash
    with pytest.raises(CommandStateError, match="protected validator writer"):
        store.commit_command_success(success)
    with pytest.raises(IdempotencyConflictError):
        store.commit_calibration_record_validation_success(
            success, replace(binding, attempt_idempotency_key="other-attempt"), record
        )
    with pytest.raises(IdempotencyConflictError):
        store.claim_command(replace(binding.claim, request_hash=_hash("different-request")))


def test_v2_runtime_capability_is_written_and_read_only_by_exact_live_identity(
    store: PostgresRuntimeStore,
) -> None:
    identity = _runtime_measurement()
    binding = replace(_binding(store), runtime_measurement_identity=identity)
    record = _record(binding)
    claim = store.claim_command(binding.claim)
    outcome = store.commit_calibration_record_validation_success(
        _success(claim.command_slot_id, record), binding, record
    )
    capability = store.read_runtime_calibration_capability(
        profile_source_sha256=binding.profile_source_sha256,
        registry_snapshot_sha256=binding.registry_snapshot_sha256,
        measurement_identity=identity,
    )
    assert capability.measurement_identity == identity
    assert capability.anchor.record == record
    assert capability.anchor.aggregate.reference.receipt_id == outcome.receipt_id
    with pytest.raises(MediaEvidenceUnavailableError):
        store.read_runtime_calibration_capability(
            profile_source_sha256=binding.profile_source_sha256,
            registry_snapshot_sha256=binding.registry_snapshot_sha256,
            measurement_identity=_runtime_measurement(capability_id="mac_cpu"),
        )


def test_generic_success_cannot_write_fresh_validator_result(store: PostgresRuntimeStore) -> None:
    binding = _binding(store)
    claim = store.claim_command(binding.claim)
    with pytest.raises(CommandStateError, match="protected validator writer"):
        store.commit_command_success(_success(claim.command_slot_id, _record(binding)))
    assert _authority_counts(binding) == (0, 0, 0, "running")


@pytest.mark.parametrize("drift", ("profile", "registry", "request", "job", "command", "attempt"))
def test_dedicated_writer_rejects_binding_and_reserved_slot_drift(
    store: PostgresRuntimeStore, drift: str
) -> None:
    binding = _binding(store)
    record = _record(binding)
    claim_input = binding.claim
    submitted_binding = binding
    if drift == "profile":
        submitted_binding = replace(binding, profile_source_sha256=_hash("other"))
    elif drift == "registry":
        submitted_binding = replace(binding, registry_snapshot_sha256=_hash("other"))
    elif drift == "request":
        claim_input = replace(claim_input, request_hash=_hash("other"))
    elif drift == "job":
        claim_input = replace(claim_input, job=Job("not-the-validator", "authority"))
    elif drift == "command":
        claim_input = replace(claim_input, command_name="AnotherCommand")
    else:
        submitted_binding = replace(binding, attempt_idempotency_key="other-attempt")
    claim = store.claim_command(claim_input)
    with pytest.raises((StoreValidationError, IdempotencyConflictError, CommandStateError)):
        store.commit_calibration_record_validation_success(
            _success(claim.command_slot_id, record), submitted_binding, record
        )
    assert store.claim_command(claim_input).state == "running"


def test_rollback_after_member_write_preserves_running_claim(
    store: PostgresRuntimeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding = _binding(store)
    record = _record(binding)
    claim = store.claim_command(binding.claim)
    success = _success(claim.command_slot_id, record)
    original = store._write_success

    def crash_after_write(cursor, submitted, job_id):
        original(cursor, submitted, job_id)
        raise RuntimeError("injected after all members and receipt")

    with monkeypatch.context() as patch:
        patch.setattr(store, "_write_success", crash_after_write)
        with pytest.raises(RuntimeError, match="injected"):
            store.commit_calibration_record_validation_success(success, binding, record)
    assert _authority_counts(binding) == (0, 0, 0, "running")
    assert store.claim_command(binding.claim).state == "running"
    store.commit_calibration_record_validation_success(success, binding, record)
    assert _authority_counts(binding) == (1, 1, 1, "succeeded")


@pytest.mark.parametrize("state", ("denied", "failed"))
def test_rejection_is_receipt_only_and_new_attempt_can_validate(
    store: PostgresRuntimeStore, state: str
) -> None:
    binding = _binding(store)
    claim = store.claim_command(binding.claim)
    code = "CALIBRATION_RECORD_INVALID" if state == "denied" else "CALIBRATION_RECORD_VALIDATION_INDETERMINATE"
    rejection = CommandRejection(claim.command_slot_id, code, '{}', state)
    outcome = store.commit_command_rejection(rejection)
    assert store.claim_command(binding.claim).receipt_id == outcome.receipt_id
    assert _authority_counts(binding) == (0, 1, 0, "running")
    record = _record(binding)
    with pytest.raises(CommandStateError):
        store.commit_calibration_record_validation_success(_success(claim.command_slot_id, record), binding, record)
    retry = replace(binding, attempt_idempotency_key="validation-attempt:2")
    fresh = store.claim_command(retry.claim)
    store.commit_calibration_record_validation_success(_success(fresh.command_slot_id, record), retry, record)
    assert _authority_counts(binding) == (1, 2, 1, "succeeded")


def test_other_open_attempt_blocks_success_without_partial_result(store: PostgresRuntimeStore) -> None:
    binding = _binding(store)
    record = _record(binding)
    claim = store.claim_command(binding.claim)
    other = store.claim_command(replace(binding, attempt_idempotency_key="other").claim)
    success = _success(claim.command_slot_id, record)
    with pytest.raises(CommandStateError, match="another command slot"):
        store.commit_calibration_record_validation_success(success, binding, record)
    assert _authority_counts(binding) == (0, 0, 0, "running")
    store.commit_command_rejection(CommandRejection(
        other.command_slot_id, "CALIBRATION_RECORD_VALIDATION_INDETERMINATE", '{}', "failed"
    ))
    store.commit_calibration_record_validation_success(success, binding, record)


def test_anchor_reader_ignores_heads_and_rejects_reference_substitution(store: PostgresRuntimeStore) -> None:
    binding = _binding(store)
    record = _record(binding)
    claim = store.claim_command(binding.claim)
    outcome = store.commit_calibration_record_validation_success(_success(claim.command_slot_id, record), binding, record)
    refs = _references(outcome)
    with psycopg.connect(VERIFY_POSTGRES_DSN) as connection, connection.cursor() as cursor:
        cursor.execute("DELETE FROM runtime.logical_heads WHERE job_id = %s", (outcome.job_id,))
    expected = {
        "expected_profile_source_sha256": binding.profile_source_sha256,
        "expected_registry_snapshot_sha256": binding.registry_snapshot_sha256,
    }
    assert store.read_calibration_record_anchor(refs[0], refs[3], **expected).record == record
    with pytest.raises(StoreValidationError, match="expected exact accepted members"):
        store.read_calibration_record_anchor(replace(refs[0], content_hash=_hash("substituted")), refs[3], **expected)
    for statement in (
        "UPDATE runtime.calibration_record_anchors SET record_sha256 = record_sha256",
        "DELETE FROM runtime.calibration_record_anchors",
    ):
        with pytest.raises(psycopg.Error, match="anchors are immutable"):
            with psycopg.connect(VERIFY_POSTGRES_DSN) as connection, connection.cursor() as cursor:
                cursor.execute(statement)


@pytest.mark.parametrize("drift", (
    "profile", "registry", "receipt", "set", "both-refs", "ordinal", "type",
    "logical-id", "revision", "scope", "scope-kind", "namespace", "validation-hash",
))
def test_consumer_anchor_reader_rejects_identity_or_exact_reference_drift(
    store: PostgresRuntimeStore, drift: str
) -> None:
    binding = _binding(store)
    record = _record(binding)
    claim = store.claim_command(binding.claim)
    outcome = store.commit_calibration_record_validation_success(_success(claim.command_slot_id, record), binding, record)
    refs = _references(outcome)
    aggregate, validation = refs[0], refs[3]
    expected = {
        "expected_profile_source_sha256": binding.profile_source_sha256,
        "expected_registry_snapshot_sha256": binding.registry_snapshot_sha256,
    }
    if drift in {"profile", "registry"}:
        key = "expected_profile_source_sha256" if drift == "profile" else "expected_registry_snapshot_sha256"
        expected[key] = _hash("wrong-identity")
    elif drift == "receipt":
        validation = replace(validation, receipt_id=uuid4())
    elif drift == "set":
        validation = replace(validation, artifact_set_id=uuid4())
    elif drift == "both-refs":
        receipt_id, set_id = uuid4(), uuid4()
        aggregate = replace(aggregate, receipt_id=receipt_id, artifact_set_id=set_id)
        validation = replace(validation, receipt_id=receipt_id, artifact_set_id=set_id)
    elif drift == "ordinal":
        validation = replace(validation, member_ordinal=2)
    elif drift == "type":
        validation = replace(validation, artifact_type="calibration_record_member")
    elif drift == "logical-id":
        validation = replace(validation, logical_id="wrong-validation-id")
    elif drift == "revision":
        validation = replace(validation, revision=2)
    elif drift == "scope":
        validation = replace(validation, scope=replace(validation.scope, key="shadow_calibration@2"))
    elif drift == "scope-kind":
        aggregate = replace(aggregate, scope=replace(aggregate.scope, kind="different"))
    elif drift == "namespace":
        aggregate = replace(aggregate, scope=replace(aggregate.scope, namespace="different"))
    else:
        validation = replace(validation, content_hash=_hash("wrong-validation"))
    with pytest.raises(StoreValidationError):
        store.read_calibration_record_anchor(aggregate, validation, **expected)


@pytest.mark.parametrize("drift", (
    "command", "profile", "v2", "results-link", "type", "logical-id", "revision", "scope", "ordinal",
    "manifest-extra", "results-extra", "coverage", "member-link", "duplicate-corpus", "raw-link",
))
def test_reader_rejects_store_readable_forged_predecessor(
    store: PostgresRuntimeStore, drift: str
) -> None:
    original = _binding(store)
    measurement = store.read_committed_shadow_calibration_measurement(original)
    request_hash = _hash("forged-request")
    job = Job(request_hash.removeprefix("sha256:"), "test" if drift == "profile" else "shadow")
    manifest = json.loads(measurement.manifest.payload_json)
    manifest["measurement_request_sha256"] = request_hash
    if drift == "v2":
        manifest["schema_version"] = "shadow-calibration-measurement-manifest-v2"
    elif drift == "manifest-extra":
        manifest["untrusted_extra"] = True
    elif drift == "duplicate-corpus":
        manifest["native_invocations"].append(manifest["native_invocations"][0])
    manifest_json = json.dumps(manifest)
    manifest_hash = canonical_payload_hash(manifest_json)
    results = json.loads(measurement.results.payload_json)
    results["measurement_manifest_sha256"] = _hash("wrong-link") if drift == "results-link" else manifest_hash
    if drift == "results-extra":
        results["untrusted_extra"] = True
    elif drift == "coverage":
        results["members"] = []
    elif drift == "member-link":
        results["members"][0]["expected_anchor_reference_sha256"] = _hash("other-anchor")
    elif drift == "duplicate-corpus":
        results["members"].append(results["members"][0])
    elif drift == "raw-link":
        results["members"][0]["native_response_sha256"] = _hash("other-raw")
    results_json = json.dumps(results)
    scope = ArtifactScope("autocut_calibration", "shadow_run", job.job_key)
    artifacts = (
        ArtifactMember("calibration_measurement_manifest", "measurement-manifest", 1, scope, manifest_hash, manifest_json),
        ArtifactMember("calibration_measurement_results", "measurement-results", 1, scope, canonical_payload_hash(results_json), results_json),
    )
    if drift == "ordinal":
        artifacts = (artifacts[1], artifacts[0])
    elif drift in {"type", "logical-id", "revision", "scope"}:
        field, value = {
            "type": ("artifact_type", "other_type"), "logical-id": ("logical_id", "other-id"),
            "revision": ("revision", 2), "scope": ("scope", replace(scope, key="other-scope")),
        }[drift]
        artifacts = (artifacts[0], replace(artifacts[1], **{field: value}))
    # Raw SQL creates malformed provenance intentionally; generic member reads
    # remain possible, while the specialized predecessor seam must reject it.
    job_id, slot_id, set_id, receipt_id = uuid4(), uuid4(), uuid4(), uuid4()
    with psycopg.connect(VERIFY_POSTGRES_DSN) as connection, connection.cursor() as cursor:
        cursor.execute("INSERT INTO runtime.jobs(job_id,job_key,profile,state) VALUES (%s,%s,%s,'running')", (job_id,job.job_key,job.profile))
        cursor.execute(
            "INSERT INTO runtime.command_slots(execution_kind, command_slot_id,job_id,idempotency_key,command_name,request_hash,state) VALUES ('deterministic', %s,%s,'forged',%s,%s,'running')",
            (slot_id,job_id,"OtherCommand" if drift == "command" else "MeasureShadowCalibrationCommand@2.1.3",request_hash),
        )
        cursor.execute("INSERT INTO runtime.artifact_sets(artifact_set_id,command_slot_id,job_id,set_hash,member_count) VALUES (%s,%s,%s,%s,2)", (set_id,slot_id,job_id,_artifact_success(slot_id,artifacts).set_hash))
        for ordinal, artifact in enumerate(artifacts):
            artifact_id = uuid4()
            cursor.execute(
                "INSERT INTO runtime.artifacts(artifact_id,artifact_set_id,job_id,artifact_type,logical_id,revision,namespace,scope_kind,scope_key,content_hash,payload_json) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)",
                (artifact_id,set_id,job_id,artifact.artifact_type,artifact.logical_id,artifact.revision,artifact.scope.namespace,artifact.scope.kind,artifact.scope.key,artifact.content_hash,artifact.payload_json),
            )
            cursor.execute("INSERT INTO runtime.artifact_set_members VALUES (%s,%s,%s)", (set_id,ordinal,artifact_id))
        cursor.execute("INSERT INTO runtime.command_receipts(receipt_id,command_slot_id,outcome,result_artifact_set_id) VALUES (%s,%s,'succeeded',%s)", (receipt_id,slot_id,set_id))
        cursor.execute("UPDATE runtime.command_slots SET state='succeeded',completed_at=transaction_timestamp() WHERE command_slot_id=%s", (slot_id,))
    outcome = CommandOutcome(slot_id, "succeeded", receipt_id=receipt_id, artifact_set_id=set_id, job_id=job_id)
    refs = _references(outcome)
    assert store.read_committed_artifact_member(refs[0]).reference == refs[0]
    with pytest.raises(StoreValidationError):
        binding = replace(original, manifest_reference=refs[0], results_reference=refs[1])
        store.read_committed_shadow_calibration_measurement(binding)
    with pytest.raises(StoreValidationError):
        store.read_shadow_calibration_measurement_outcome(
            Job(request_hash.removeprefix("sha256:"), "shadow"), outcome,
            expected_request_sha256=request_hash,
            expected_profile_source_sha256=original.profile_source_sha256,
            expected_registry_snapshot_sha256=original.registry_snapshot_sha256,
        )
