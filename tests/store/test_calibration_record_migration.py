"""Verification-DB probes for the protected CalibrationRecord migration.

The generic Store API is the threat boundary: this test uses raw SQL only to
prove that the database allows its one exact validator-writer path and rejects
generic/incorrect Store-shaped writes. It is not a claim to constrain a
database superuser. Every destructive reset is explicitly limited to the
disposable ``ac_autocut_verify`` database.
"""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

import pytest

psycopg = pytest.importorskip("psycopg")

VERIFY_POSTGRES_DSN = "postgresql://ac_user:ac_password_2026@127.0.0.1:5433/ac_autocut_verify"
MIGRATIONS = Path("packages/autocut-kernel/migrations")
MIGRATION = MIGRATIONS / "0017_authority_calibration_record.sql"
PROFILE_KEY = "shadow_calibration@1"
COMMAND = "ValidateCalibrationRecord@2.1.3"


def _hash(letter: str) -> str:
    return "sha256:" + sha256(letter.encode("utf-8")).hexdigest()


def _reset_database(*, through_0016: bool = False) -> None:
    try:
        connection = psycopg.connect(VERIFY_POSTGRES_DSN, autocommit=True)
    except psycopg.OperationalError:
        pytest.skip("disposable authority PostgreSQL is unavailable")
    with connection:
        if connection.info.dbname != "ac_autocut_verify":
            pytest.fail("CalibrationRecord migration tests may run only against ac_autocut_verify")
        with connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA IF EXISTS storage CASCADE")
            cursor.execute("DROP SCHEMA IF EXISTS runtime CASCADE")
            for migration in sorted(MIGRATIONS.glob("*.sql")):
                if through_0016 and migration.name >= MIGRATION.name:
                    break
                cursor.execute(migration.read_text(encoding="utf-8"))


@pytest.fixture
def migrated_database() -> None:
    _reset_database()


def _connection():
    return psycopg.connect(VERIFY_POSTGRES_DSN)


def _payloads(*, aggregate_payload: object | None = None, validation_payload: object | None = None) -> tuple[object, object]:
    identity = {
        "acceptance_policy_sha256": _hash("acceptance"),
        "alignment_policy_sha256": _hash("alignment"),
        "bound_algorithm_sha256": _hash("bound-algorithm"),
        "calibration_corpus_set_sha256": _hash("corpus"),
        "native_port_identity_sha256": _hash("native-port"),
        "profile_source_sha256": _hash("profile-source"),
        "registry_snapshot_sha256": _hash("registry-snapshot"),
        "source_clock_id": "source-audio-clock",
        "source_time_base": {"numerator": 1, "denominator": 1_000},
        "timed_speech_policy_sha256": _hash("timed-speech-policy"),
        "vad_merge_policy_sha256": _hash("vad-merge-policy"),
        "word_gap_policy_sha256": _hash("word-gap-policy"),
    }
    aggregate = {
        "schema_version": "calibration-record-v1",
        "record_kind": "shadow_native_timing",
        "member_count": 2,
        "identity": identity,
        "measurement_manifest_sha256": _hash("manifest"),
        "measurement_results_sha256": _hash("results"),
        "asr_member_sha256": _hash("asr-member"),
        "vad_member_sha256": _hash("vad-member"),
        "asr_accepted_bound_tick": 1,
        "vad_accepted_bound_tick": 1,
    }
    validation = {
        "schema_version": "calibration-record-validation-receipt-v1",
        "decision": "accepted",
        "validator_command": COMMAND,
        "validator_principal": "autocut-calibration-validator",
        "bound_algorithm_sha256": _hash("bound-algorithm"),
        "checks": [],
        "validation_input_sha256": _hash("validation-input"),
        "validation_result_sha256": _hash("validation-result"),
        "record_sha256": _hash("record"),
        "asr_member_sha256": _hash("asr-member"),
        "vad_member_sha256": _hash("vad-member"),
        "measurement_manifest_sha256": _hash("manifest"),
        "measurement_results_sha256": _hash("results"),
    }
    return (
        aggregate if aggregate_payload is None else aggregate_payload,
        validation if validation_payload is None else validation_payload,
    )


def _insert_validator_result(
    cursor: object,
    *,
    profile_version: str = "1",
    write_receipt_and_anchor: bool = True,
    terminalize_slot: bool = True,
    terminalize_job: bool = True,
    aggregate_payload: object | None = None,
    validation_payload: object | None = None,
) -> tuple[UUID, UUID, UUID]:
    """Write the sole Store-shaped accepted path, returning job/slot/receipt IDs."""
    job_id, slot_id, set_id, receipt_id = uuid4(), uuid4(), uuid4(), uuid4()
    artifacts = (uuid4(), uuid4(), uuid4(), uuid4())
    profile_key = f"shadow_calibration@{profile_version}"
    job_key = f"autocut_calibration_validator:{profile_key}"
    aggregate, validation = _payloads(
        aggregate_payload=aggregate_payload, validation_payload=validation_payload
    )
    cursor.execute(
        "INSERT INTO runtime.jobs (job_id, job_key, profile, state) VALUES (%s, %s, 'authority', 'running')",
        (job_id, job_key),
    )
    cursor.execute(
        """
        INSERT INTO runtime.command_slots
            (command_slot_id, job_id, idempotency_key, command_name, request_hash, state, completed_at)
        VALUES (%s, %s, 'validator-request', %s, %s, 'running', NULL)
        """,
        (slot_id, job_id, COMMAND, _hash("accepted-request")),
    )
    cursor.execute(
        """
        INSERT INTO runtime.artifact_sets (artifact_set_id, command_slot_id, job_id, set_hash, member_count)
        VALUES (%s, %s, %s, %s, 4)
        """,
        (set_id, slot_id, job_id, _hash("accepted-set")),
    )
    rows = (
        (artifacts[0], "calibration_record", f"calibration-record/aggregate/{profile_key}/1", _hash("record"), aggregate),
        (artifacts[1], "calibration_record_member", f"calibration-record/member/asr/{profile_key}/1", _hash("asr-member"), {"role": "asr"}),
        (artifacts[2], "calibration_record_member", f"calibration-record/member/vad/{profile_key}/1", _hash("vad-member"), {"role": "vad"}),
        (artifacts[3], "calibration_validation_receipt", f"calibration-record/validation/{profile_key}/1", _hash("validation-receipt"), validation),
    )
    for artifact_id, artifact_type, logical_id, content_hash, payload in rows:
        cursor.execute(
            """
            INSERT INTO runtime.artifacts
                (artifact_id, artifact_set_id, artifact_type, logical_id, revision,
                 namespace, scope_kind, scope_key, content_hash, payload_json, job_id)
            VALUES (%s, %s, %s, %s, 1, 'autocut_authority', 'calibration', %s, %s, %s::jsonb, %s)
            """,
            (artifact_id, set_id, artifact_type, logical_id, profile_key, content_hash, json.dumps(payload), job_id),
        )
    for ordinal, artifact_id in enumerate(artifacts):
        cursor.execute(
            "INSERT INTO runtime.artifact_set_members (artifact_set_id, ordinal, artifact_id) VALUES (%s, %s, %s)",
            (set_id, ordinal, artifact_id),
        )
    if write_receipt_and_anchor:
        cursor.execute(
            """
            INSERT INTO runtime.command_receipts (receipt_id, command_slot_id, outcome, result_artifact_set_id)
            VALUES (%s, %s, 'succeeded', %s)
            """,
            (receipt_id, slot_id, set_id),
        )
        cursor.execute(
            """
            INSERT INTO runtime.calibration_record_anchors (
                namespace, scope_kind, scope_key, record_sha256, profile_source_sha256,
                registry_snapshot_sha256, measurement_manifest_sha256, measurement_results_sha256,
                asr_member_sha256, vad_member_sha256, validation_receipt_sha256, receipt_id,
                artifact_set_id, aggregate_member_ordinal, validation_member_ordinal, command_slot_id
            ) VALUES (
                'autocut_authority', 'calibration', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0, 3, %s
            )
            """,
            (
                profile_key,
                _hash("record"),
                _hash("profile-source"),
                _hash("registry-snapshot"),
                _hash("manifest"),
                _hash("results"),
                _hash("asr-member"),
                _hash("vad-member"),
                _hash("validation-receipt"),
                receipt_id,
                set_id,
                slot_id,
            ),
        )
    if terminalize_slot:
        cursor.execute(
            "UPDATE runtime.command_slots SET state = 'succeeded', completed_at = transaction_timestamp() WHERE command_slot_id = %s",
            (slot_id,),
        )
    if terminalize_job:
        cursor.execute("UPDATE runtime.jobs SET state = 'succeeded' WHERE job_id = %s", (job_id,))
    return job_id, slot_id, receipt_id


def _insert_terminal_rejection(
    cursor: object, *, state: str, failure_code: str, profile_version: str, with_set: bool = False
) -> UUID:
    job_id, slot_id, set_id, receipt_id = uuid4(), uuid4(), uuid4(), uuid4()
    profile_key = f"shadow_calibration@{profile_version}"
    job_key = f"autocut_calibration_validator:{profile_key}"
    cursor.execute(
        "INSERT INTO runtime.jobs (job_id, job_key, profile, state) VALUES (%s, %s, 'authority', 'running')",
        (job_id, job_key),
    )
    cursor.execute(
        """
        INSERT INTO runtime.command_slots
            (command_slot_id, job_id, idempotency_key, command_name, request_hash, state, completed_at)
        VALUES (%s, %s, 'validator-rejection', %s, %s, 'running', NULL)
        """,
        (slot_id, job_id, COMMAND, _hash(f"rejection-request:{profile_version}")),
    )
    if with_set:
        artifact_id = uuid4()
        cursor.execute(
            """
            INSERT INTO runtime.artifact_sets (artifact_set_id, command_slot_id, job_id, set_hash, member_count)
            VALUES (%s, %s, %s, %s, 1)
            """,
            (set_id, slot_id, job_id, _hash(f"rejection-set:{profile_version}")),
        )
        cursor.execute(
            """
            INSERT INTO runtime.artifacts
                (artifact_id, artifact_set_id, artifact_type, logical_id, revision,
                 namespace, scope_kind, scope_key, content_hash, payload_json, job_id)
            VALUES (%s, %s, 'ordinary', 'ordinary/1', 1, 'ordinary', 'ordinary', 'ordinary', %s, '{}'::jsonb, %s)
            """,
            (artifact_id, set_id, _hash(f"rejection-artifact:{profile_version}"), job_id),
        )
        cursor.execute(
            "INSERT INTO runtime.artifact_set_members (artifact_set_id, ordinal, artifact_id) VALUES (%s, 0, %s)",
            (set_id, artifact_id),
        )
    cursor.execute(
        """
        INSERT INTO runtime.command_receipts
            (receipt_id, command_slot_id, outcome, failure_code, failure_detail)
        VALUES (%s, %s, %s, %s, '{"stage":"validator"}'::jsonb)
        """,
        (receipt_id, slot_id, state, failure_code),
    )
    cursor.execute(
        "UPDATE runtime.command_slots SET state = %s, completed_at = transaction_timestamp() WHERE command_slot_id = %s",
        (state, slot_id),
    )
    return slot_id


def _attach_ordinary_artifact_set(cursor: object, slot_id: UUID) -> None:
    set_id, artifact_id = uuid4(), uuid4()
    cursor.execute("SELECT job_id FROM runtime.command_slots WHERE command_slot_id = %s", (slot_id,))
    job_id = cursor.fetchone()[0]
    cursor.execute(
        """
        INSERT INTO runtime.artifact_sets (artifact_set_id, command_slot_id, job_id, set_hash, member_count)
        VALUES (%s, %s, %s, %s, 1)
        """,
        (set_id, slot_id, job_id, _hash(f"late-set:{slot_id}")),
    )
    cursor.execute(
        """
        INSERT INTO runtime.artifacts
            (artifact_id, artifact_set_id, artifact_type, logical_id, revision,
             namespace, scope_kind, scope_key, content_hash, payload_json, job_id)
        VALUES (%s, %s, 'ordinary', 'ordinary/1', 1, 'ordinary', 'ordinary', 'ordinary', %s, '{}'::jsonb, %s)
        """,
        (artifact_id, set_id, _hash(f"late-artifact:{slot_id}"), job_id),
    )
    cursor.execute(
        "INSERT INTO runtime.artifact_set_members (artifact_set_id, ordinal, artifact_id) VALUES (%s, 0, %s)",
        (set_id, artifact_id),
    )


def test_calibration_record_migration_closes_exact_terminal_validator_path(migrated_database: None) -> None:
    with _connection() as connection, connection.cursor() as cursor:
        _, slot_id, receipt_id = _insert_validator_result(cursor)
        cursor.execute(
            "SELECT record_sha256, command_slot_id, receipt_id FROM runtime.calibration_record_anchors"
        )
        record_sha256, anchored_slot_id, anchored_receipt_id = cursor.fetchone()
        if isinstance(record_sha256, bytes):
            record_sha256 = record_sha256.decode("ascii")
        assert (record_sha256, anchored_slot_id, anchored_receipt_id) == (_hash("record"), slot_id, receipt_id)


def test_running_validator_slot_cannot_commit_protected_members_without_receipt_or_anchor(
    migrated_database: None,
) -> None:
    with pytest.raises(psycopg.Error, match="calibration record artifact set must contain the exact four ordered members"):
        with _connection() as connection, connection.cursor() as cursor:
            _insert_validator_result(
                cursor,
                write_receipt_and_anchor=False,
                terminalize_slot=False,
                terminalize_job=False,
            )


def test_running_validator_job_key_and_profile_are_frozen(migrated_database: None) -> None:
    job_id = uuid4()
    with _connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO runtime.jobs (job_id, job_key, profile, state)
            VALUES (%s, 'autocut_calibration_validator:shadow_calibration@1', 'authority', 'running')
            """,
            (job_id,),
        )

    with pytest.raises(psycopg.Error, match="calibration validator Job key and profile are immutable"):
        with _connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE runtime.jobs SET job_key = 'autocut_calibration_validator:shadow_calibration@2' WHERE job_id = %s",
                (job_id,),
            )


def test_terminal_pipeline_job_still_requires_finalize_run_outcome(migrated_database: None) -> None:
    job_id = uuid4()
    with pytest.raises(psycopg.Error, match="terminal Job requires exactly one matching FinalizeRunOutcome receipt"):
        with _connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO runtime.jobs (job_id, job_key, profile, state) VALUES (%s, 'ordinary-pipeline', 'test', 'running')",
                (job_id,),
            )
            cursor.execute("UPDATE runtime.jobs SET state = 'succeeded' WHERE job_id = %s", (job_id,))


def test_successful_validator_job_cannot_close_with_an_open_slot(migrated_database: None) -> None:
    with pytest.raises(psycopg.Error, match="calibration validator finalization is blocked by pending or running command slots"):
        with _connection() as connection, connection.cursor() as cursor:
            job_id, _, _ = _insert_validator_result(cursor, terminalize_job=False)
            cursor.execute(
                """
                INSERT INTO runtime.command_slots
                    (command_slot_id, job_id, idempotency_key, command_name, request_hash, state)
                VALUES (%s, %s, 'open', 'ordinary-command', %s, 'running')
                """,
                (uuid4(), job_id, _hash("open-slot")),
            )
            cursor.execute("UPDATE runtime.jobs SET state = 'succeeded' WHERE job_id = %s", (job_id,))


def test_protected_scope_rejects_generic_store_shaped_writer(migrated_database: None) -> None:
    with pytest.raises(psycopg.Error, match="requires the dedicated validator and exact member identity"):
        with _connection() as connection, connection.cursor() as cursor:
            job_id, slot_id, set_id = uuid4(), uuid4(), uuid4()
            cursor.execute(
                "INSERT INTO runtime.jobs (job_id, job_key, profile, state) VALUES (%s, 'generic', 'authority', 'running')",
                (job_id,),
            )
            cursor.execute(
                """
                INSERT INTO runtime.command_slots (command_slot_id, job_id, idempotency_key, command_name, request_hash, state)
                VALUES (%s, %s, 'generic', 'CommitCommandSuccess@2.1.3', %s, 'running')
                """,
                (slot_id, job_id, _hash("q")),
            )
            cursor.execute(
                """
                INSERT INTO runtime.artifact_sets (artifact_set_id, command_slot_id, job_id, set_hash, member_count)
                VALUES (%s, %s, %s, %s, 1)
                """,
                (set_id, slot_id, job_id, _hash("r")),
            )
            cursor.execute(
                """
                INSERT INTO runtime.artifacts
                    (artifact_id, artifact_set_id, artifact_type, logical_id, revision,
                     namespace, scope_kind, scope_key, content_hash, payload_json, job_id)
                VALUES (%s, %s, 'calibration_record', %s, 1,
                        'autocut_authority', 'calibration', %s, %s, '{}'::jsonb, %s)
                """,
                (
                    uuid4(),
                    set_id,
                    f"calibration-record/aggregate/{PROFILE_KEY}/1",
                    PROFILE_KEY,
                    _hash("s"),
                    job_id,
                ),
            )


def test_calibration_record_anchor_is_immutable(migrated_database: None) -> None:
    with _connection() as connection, connection.cursor() as cursor:
        _insert_validator_result(cursor)

    for statement in (
        "UPDATE runtime.calibration_record_anchors SET record_sha256 = record_sha256",
        "DELETE FROM runtime.calibration_record_anchors",
    ):
        with pytest.raises(psycopg.Error, match="calibration record anchors are immutable"):
            with _connection() as connection, connection.cursor() as cursor:
                cursor.execute(statement)


@pytest.mark.parametrize(
    ("aggregate_payload", "validation_payload"),
    (
        ({}, None),
        ({"schema_version": "calibration-record-v1"}, None),
        (None, {}),
        (None, {"schema_version": "calibration-record-validation-receipt-v1"}),
    ),
)
def test_calibration_record_anchor_rejects_empty_or_incomplete_required_payloads(
    migrated_database: None, aggregate_payload: object | None, validation_payload: object | None
) -> None:
    with pytest.raises(psycopg.Error, match="does not close over its exact accepted validator result"):
        with _connection() as connection, connection.cursor() as cursor:
            _insert_validator_result(
                cursor, aggregate_payload=aggregate_payload, validation_payload=validation_payload
            )


def test_calibration_record_anchor_requires_succeeded_validator_job(migrated_database: None) -> None:
    with pytest.raises(psycopg.Error, match="does not close over its exact accepted validator result"):
        with _connection() as connection, connection.cursor() as cursor:
            _insert_validator_result(cursor, terminalize_job=False)


def test_calibration_record_anchor_reads_nested_identity_hashes(migrated_database: None) -> None:
    aggregate, _ = _payloads()
    assert isinstance(aggregate, dict)
    identity = aggregate["identity"]
    assert isinstance(identity, dict)
    identity["profile_source_sha256"] = _hash("substituted-profile-source")

    with pytest.raises(psycopg.Error, match="does not close over its exact accepted validator result"):
        with _connection() as connection, connection.cursor() as cursor:
            _insert_validator_result(cursor, aggregate_payload=aggregate)


@pytest.mark.parametrize(
    ("state", "failure_code"),
    (("denied", "CALIBRATION_RECORD_INVALID"), ("failed", "CALIBRATION_RECORD_VALIDATION_INDETERMINATE")),
)
def test_non_successful_validator_receipts_are_generic_only(
    migrated_database: None, state: str, failure_code: str
) -> None:
    with _connection() as connection, connection.cursor() as cursor:
        slot_id = _insert_terminal_rejection(
            cursor, state=state, failure_code=failure_code, profile_version="2"
        )
        cursor.execute("SELECT count(*) FROM runtime.artifact_sets WHERE command_slot_id = %s", (slot_id,))
        assert cursor.fetchone() == (0,)
        cursor.execute("SELECT count(*) FROM runtime.calibration_record_anchors")
        assert cursor.fetchone() == (0,)

    with pytest.raises(psycopg.Error, match="may own only its protected four-member artifact set"):
        with _connection() as connection, connection.cursor() as cursor:
            _attach_ordinary_artifact_set(cursor, slot_id)


def test_migration_rejects_preexisting_protected_artifacts(migrated_database: None) -> None:
    _reset_database(through_0016=True)
    with _connection() as connection, connection.cursor() as cursor:
        job_id, slot_id, set_id, artifact_id = uuid4(), uuid4(), uuid4(), uuid4()
        cursor.execute(
            "INSERT INTO runtime.jobs (job_id, job_key, profile, state) VALUES (%s, 'legacy', 'authority', 'running')",
            (job_id,),
        )
        cursor.execute(
            """
            INSERT INTO runtime.command_slots (command_slot_id, job_id, idempotency_key, command_name, request_hash, state)
            VALUES (%s, %s, 'legacy', 'legacy', %s, 'running')
            """,
            (slot_id, job_id, _hash("n")),
        )
        cursor.execute(
            "INSERT INTO runtime.artifact_sets (artifact_set_id, command_slot_id, job_id, set_hash, member_count) VALUES (%s, %s, %s, %s, 1)",
            (set_id, slot_id, job_id, _hash("o")),
        )
        cursor.execute(
            """
            INSERT INTO runtime.artifacts
                (artifact_id, artifact_set_id, artifact_type, logical_id, revision,
                 namespace, scope_kind, scope_key, content_hash, payload_json, job_id)
            VALUES (%s, %s, 'legacy', 'legacy/1', 1, 'autocut_authority', 'calibration', %s, %s, '{}'::jsonb, %s)
            """,
            (artifact_id, set_id, PROFILE_KEY, _hash("p"), job_id),
        )
        cursor.execute(
            "INSERT INTO runtime.artifact_set_members (artifact_set_id, ordinal, artifact_id) VALUES (%s, 0, %s)",
            (set_id, artifact_id),
        )
    with pytest.raises(psycopg.Error, match="0017 refuses pre-existing protected calibration artifacts"):
        with _connection() as connection, connection.cursor() as cursor:
            cursor.execute(MIGRATION.read_text(encoding="utf-8"))


def test_migration_rejects_preexisting_anchor_relation(migrated_database: None) -> None:
    _reset_database(through_0016=True)
    with _connection() as connection, connection.cursor() as cursor:
        cursor.execute("CREATE TABLE runtime.calibration_record_anchors (legacy_id uuid PRIMARY KEY)")
    with pytest.raises(psycopg.Error, match="0017 refuses a pre-existing calibration record anchor relation"):
        with _connection() as connection, connection.cursor() as cursor:
            cursor.execute(MIGRATION.read_text(encoding="utf-8"))


def test_calibration_record_migration_preserves_native_recovery_boundary() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "shadow_calibration_measurement" not in sql
    assert "recovery_lease" not in sql
    assert "native invocation" not in sql


def test_calibration_record_migration_statically_closes_nullable_and_preexisting_paths() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "IS DISTINCT FROM 'calibration-record-v1'" in sql
    assert "IS DISTINCT FROM 'calibration-record-validation-receipt-v1'" in sql
    assert "aggregate_payload->'identity'->>'profile_source_sha256'" in sql
    assert "aggregate_payload->'identity'->>'registry_snapshot_sha256'" in sql
    assert "validator_job_state IS DISTINCT FROM 'succeeded'" in sql
    assert "assert_exact_calibration_validator_finalization" in sql
    assert "0017 refuses pre-existing protected calibration artifacts" in sql
    assert "0017 refuses a pre-existing calibration record anchor relation" in sql
    assert "non-successful calibration validator receipt cannot own artifact sets, artifacts, or anchors" in sql
