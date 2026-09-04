"""Raw-SQL closure negatives on the disposable verification PostgreSQL only.

All invalid rows are inserted with the installed production guards enabled.
Migration preflight cases build genuine 0059 history, not disabled-trigger data.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from autocut_kernel.store import CommandRejection, CommandSuccess, PostgresRuntimeStore
from autocut_kernel.store.models import ProductionQcCollectorCapabilityBinding

from tests.store.test_production_qc_collector_capability_postgres import (
    DSN,
    MIGRATIONS,
    _unique_binding,
    migrated_database,  # noqa: F401 -- reuse the guarded, module-scoped fixture
    store,  # noqa: F401 -- reuse the real Store fixture
)

psycopg = pytest.importorskip("psycopg")
from psycopg import sql  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402

pytestmark = pytest.mark.skipif(
    not DSN, reason="set AUTOCUT_TEST_POSTGRES_DSN to disposable ac_autocut_verify"
)
CLOSURE_MIGRATION = MIGRATIONS / "0060_production_qc_collector_capability_closure.sql"
OTHER_HASH = "sha256:" + "e" * 64


def _insert(cursor, table: str, values: dict[str, object]) -> None:
    cursor.execute(
        sql.SQL("INSERT INTO runtime.{} ({}) VALUES ({})").format(
            sql.Identifier(table),
            sql.SQL(", ").join(map(sql.Identifier, values)),
            sql.SQL(", ").join(sql.Placeholder() for _ in values),
        ),
        tuple(values.values()),
    )


def _capability_values(
    binding: ProductionQcCollectorCapabilityBinding, ids: dict[str, UUID]
) -> dict[str, object]:
    policy, live = binding.policy, binding.request.live_profile
    return {
        "namespace": binding.scope.namespace,
        "scope_kind": binding.scope.kind,
        "scope_key": binding.scope_key,
        "profile_id": policy.profile_id,
        "qc_runner_identity_sha256": live.canonical_sha256,
        "policy_source_sha256": policy.policy_source_sha256,
        "registry_snapshot_sha256": policy.registry_snapshot_sha256,
        "collector_registry_sha256": policy.collector_registry_sha256,
        "required_check_set_version": policy.required_check_set_version,
        "runner_schema_version": policy.runner_schema_version,
        "fixed_environment_sha256": policy.fixed_environment_sha256,
        "ffmpeg_executable_sha256": live.ffmpeg_identity.executable_sha256,
        "ffmpeg_executable_byte_length": live.ffmpeg_identity.executable_byte_length,
        "ffmpeg_version_output_sha256": live.ffmpeg_identity.version_output_sha256,
        "ffprobe_executable_sha256": live.ffprobe_identity.executable_sha256,
        "ffprobe_executable_byte_length": live.ffprobe_identity.executable_byte_length,
        "ffprobe_version_output_sha256": live.ffprobe_identity.version_output_sha256,
        "capability_request_json": json.dumps(
            binding.request.to_mapping(), sort_keys=True, separators=(",", ":")
        ),
        "capability_request_sha256": binding.request.canonical_sha256,
        "measurement_member_sha256": binding.measurement_member.content_hash,
        "capability_member_sha256": binding.decision_member.content_hash,
        "decision": "accepted",
        "receipt_id": ids["receipt_id"],
        "artifact_set_id": ids["artifact_set_id"],
        "command_slot_id": ids["command_slot_id"],
        **binding.provenance.to_mapping(),
    }


def _stage(
    cursor,
    binding: ProductionQcCollectorCapabilityBinding,
    *,
    include_capability: bool = True,
    with_output: bool = True,
    state: str = "succeeded",
    row_first: bool = False,
    row_changes: dict[str, object] | None = None,
    slot_changes: dict[str, object] | None = None,
    member_changes: dict[str, object] | None = None,
    set_changes: dict[str, object] | None = None,
    reverse_members: bool = False,
) -> dict[str, UUID]:
    """Stage a complete, valid writer-shaped transaction, with one chosen drift."""
    ids = {name: uuid4() for name in ("job_id", "command_slot_id", "artifact_set_id", "receipt_id")}
    _insert(cursor, "jobs", {
        "job_id": ids["job_id"], "job_key": binding.job.job_key,
        "profile": "authority", "state": "pending" if state == "pending" else "running",
    })
    _insert(cursor, "command_slots", {
        "command_slot_id": ids["command_slot_id"], "job_id": ids["job_id"],
        "idempotency_key": binding.attempt_idempotency_key,
        "command_name": binding.claim.command_name, "request_hash": binding.request_hash,
        "execution_kind": "deterministic", "state": state,
        "completed_at": datetime.now(timezone.utc) if state in {"succeeded", "denied", "failed"} else None,
        **(slot_changes or {}),
    })
    if with_output:
        _insert(cursor, "artifact_sets", {
            "artifact_set_id": ids["artifact_set_id"], "job_id": ids["job_id"],
            "command_slot_id": ids["command_slot_id"],
            "set_hash": binding.expected_set_hash, "member_count": 2,
            **(set_changes or {}),
        })
    if state in {"succeeded", "denied", "failed"}:
        receipt: dict[str, object] = {
            "receipt_id": ids["receipt_id"], "command_slot_id": ids["command_slot_id"],
            "outcome": state,
        }
        if state == "succeeded":
            receipt["result_artifact_set_id"] = ids["artifact_set_id"]
        else:
            receipt.update(failure_code="QC_REJECTED", failure_detail=Jsonb({"reason": "test"}))
        _insert(cursor, "command_receipts", receipt)
    row = {**_capability_values(binding, ids), **(row_changes or {})}
    if include_capability and row_first:
        _insert(cursor, "production_qc_collector_capabilities", row)
    if with_output:
        for ordinal, member in enumerate(binding.members):
            artifact_id = uuid4()
            _insert(cursor, "artifacts", {
                "artifact_id": artifact_id, "artifact_set_id": ids["artifact_set_id"],
                "job_id": ids["job_id"], "artifact_type": member.artifact_type,
                "logical_id": member.logical_id, "revision": member.revision,
                "namespace": member.scope.namespace, "scope_kind": member.scope.kind,
                "scope_key": member.scope.key, "content_hash": member.content_hash,
                "payload_json": Jsonb(json.loads(member.payload_json)),
                **((member_changes or {}) if ordinal == 0 else {}),
            })
            _insert(cursor, "artifact_set_members", {
                "artifact_set_id": ids["artifact_set_id"], "artifact_id": artifact_id,
                "ordinal": 1 - ordinal if reverse_members else ordinal,
            })
    if include_capability and not row_first:
        _insert(cursor, "production_qc_collector_capabilities", row)
    if state in {"succeeded", "denied", "failed"}:
        cursor.execute("UPDATE runtime.jobs SET state = %s WHERE job_id = %s", (state, ids["job_id"]))
    return ids


@pytest.mark.parametrize("row_first", [False, True], ids=["members-first", "row-first"])
def test_valid_sql_closure_allows_either_insertion_order(row_first: bool) -> None:
    binding = _unique_binding(f"closure-valid-order-{row_first}")
    with psycopg.connect(DSN) as connection, connection.cursor() as cursor:
        _stage(cursor, binding, row_first=row_first)
    verified = PostgresRuntimeStore(lambda: psycopg.connect(DSN)).resolve_accepted_production_qc_collector_capability(
        binding.request
    )
    assert verified.request == binding.request


def test_real_writer_and_replay_keep_exact_closure(store: PostgresRuntimeStore) -> None:
    binding = _unique_binding("closure-writer-replay")
    claim = store.claim_qc_collector_capability_command(binding.claim)
    success = CommandSuccess(claim.command_slot_id, binding.expected_set_hash, binding.members)
    first = store.commit_qc_collector_capability_success(success, binding)
    replay = store.commit_qc_collector_capability_success(success, binding)
    assert (first.receipt_id, first.artifact_set_id, first.command_slot_id) == (
        replay.receipt_id, replay.artifact_set_id, replay.command_slot_id
    )
    assert store.resolve_accepted_production_qc_collector_capability(binding.request).receipt_id == first.receipt_id


@pytest.mark.parametrize("reference", ["receipt_id", "artifact_set_id", "command_slot_id", "all"])
def test_capability_cannot_reference_foreign_valid_closure(reference: str) -> None:
    # Both closures are new in the same transaction, so no uniqueness violation
    # can mask the missing cross-reference check. The foreign set deliberately
    # has no row; all foreign UUIDs really exist and satisfy the base FKs.
    with pytest.raises(psycopg.errors.RaiseException, match="exact succeeded Job/slot/Receipt/set closure"):
        with psycopg.connect(DSN) as connection, connection.cursor() as cursor:
            foreign = _stage(cursor, _unique_binding(f"foreign-{reference}"), include_capability=False)
            columns = ("receipt_id", "artifact_set_id", "command_slot_id") if reference == "all" else (reference,)
            _stage(cursor, _unique_binding(f"victim-{reference}"), row_changes={key: foreign[key] for key in columns})
            # Check the substituted row itself before the foreign Job's
            # independent missing-row constraint can mask this violation.
            cursor.execute("SET CONSTRAINTS runtime.production_qc_capability_closure_from_row IMMEDIATE")


@pytest.mark.parametrize("field", ["request_hash", "idempotency_key", "command_name", "execution_kind"])
def test_slot_must_bind_exact_deterministic_request(field: str) -> None:
    binding = _unique_binding(f"closure-slot-{field}")
    values = {
        "request_hash": OTHER_HASH,
        "idempotency_key": binding.attempt_idempotency_key[:-64] + "e" * 64,
        "command_name": "OtherValidator",
        "execution_kind": "generation",
    }
    with pytest.raises(psycopg.errors.RaiseException):
        with psycopg.connect(DSN) as connection, connection.cursor() as cursor:
            _stage(cursor, binding, slot_changes={field: values[field]})


@pytest.mark.parametrize("field", [
    "collector_registry_sha256", "fixed_environment_sha256",
    "ffmpeg_executable_sha256", "ffmpeg_executable_byte_length",
    "ffprobe_version_output_sha256", "authority_revision", "source_commit",
    "measurement_member_sha256", "capability_member_sha256",
])
def test_denormalized_row_must_match_request_and_provenance(field: str) -> None:
    value: object = OTHER_HASH
    if field in {"ffmpeg_executable_byte_length", "authority_revision"}:
        value = 999
    elif field == "source_commit":
        value = "e" * 40
    with pytest.raises(psycopg.errors.RaiseException, match="production QC capability"):
        with psycopg.connect(DSN) as connection, connection.cursor() as cursor:
            _stage(cursor, _unique_binding(f"closure-row-{field}"), row_changes={field: value})


@pytest.mark.parametrize("drift", ["extra", "duplicate", "whitespace", "static", "live"])
def test_request_json_is_exact_canonical_closed_projection(drift: str) -> None:
    import hashlib

    binding = _unique_binding(f"closure-request-{drift}")
    document = binding.request.to_mapping()
    if drift == "extra":
        document["extra"] = True
    elif drift == "static":
        document["policy_source"]["collector_registry_sha256"] = OTHER_HASH
    elif drift == "live":
        document["live_profile"]["ffmpeg_identity"]["executable_byte_length"] = 999
    raw = json.dumps(document, sort_keys=True, separators=(",", ":"))
    if drift == "whitespace":
        raw += "\n"
    elif drift == "duplicate":
        raw = '{"authority_state":"store_acceptance_required",' + raw[1:]
    digest = "sha256:" + hashlib.sha256(raw.encode()).hexdigest()
    with pytest.raises(psycopg.errors.RaiseException, match="production QC capability"):
        with psycopg.connect(DSN) as connection, connection.cursor() as cursor:
            _stage(cursor, binding, row_changes={
                "capability_request_json": raw, "capability_request_sha256": digest,
            })


@pytest.mark.parametrize("drift", ["ordinal", "payload", "hash", "logical-id", "scope", "set-hash"])
def test_members_and_set_must_match_exact_ordered_semantics(drift: str) -> None:
    binding = _unique_binding(f"closure-member-{drift}")
    changes: dict[str, object] = {}
    if drift == "payload":
        payload = json.loads(binding.measurement_payload_json)
        payload["extra"] = True
        changes["payload_json"] = Jsonb(payload)
    elif drift == "hash":
        changes["content_hash"] = OTHER_HASH
    elif drift == "logical-id":
        changes["logical_id"] = "foreign"
    elif drift == "scope":
        changes["scope_kind"] = "other_scope"
    with pytest.raises(psycopg.errors.RaiseException):
        with psycopg.connect(DSN) as connection, connection.cursor() as cursor:
            _stage(
                cursor, binding, member_changes=changes,
                reverse_members=drift == "ordinal",
                set_changes={"set_hash": OTHER_HASH} if drift == "set-hash" else None,
            )


def test_succeeded_validator_cannot_omit_capability_row() -> None:
    with pytest.raises(psycopg.errors.RaiseException, match="requires exactly one accepted row/set"):
        with psycopg.connect(DSN) as connection, connection.cursor() as cursor:
            _stage(cursor, _unique_binding("closure-missing-row"), include_capability=False)


@pytest.mark.parametrize("state", ["pending", "running", "denied", "failed"])
def test_non_success_validator_cannot_own_output(state: str) -> None:
    with pytest.raises(psycopg.errors.RaiseException, match="non-success validator cannot own accepted output"):
        with psycopg.connect(DSN) as connection, connection.cursor() as cursor:
            _stage(cursor, _unique_binding(f"closure-nonsuccess-{state}"), state=state, include_capability=False)


@pytest.mark.parametrize("state", ["pending", "running", "denied", "failed"])
def test_empty_non_success_claim_is_legal(state: str) -> None:
    with psycopg.connect(DSN) as connection, connection.cursor() as cursor:
        _stage(cursor, _unique_binding(f"closure-empty-{state}"), state=state, include_capability=False, with_output=False)


@pytest.mark.parametrize("state", ["denied", "failed"])
def test_existing_rejection_writer_keeps_empty_running_job(store: PostgresRuntimeStore, state: str) -> None:
    binding = _unique_binding(f"closure-rejection-{state}")
    claim = store.claim_qc_collector_capability_command(binding.claim)
    outcome = store.commit_command_rejection(CommandRejection(claim.command_slot_id, "QC_REJECTED", "{}", state))
    assert outcome.state == state
    with psycopg.connect(DSN) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT state FROM runtime.jobs WHERE job_id = %s", (outcome.job_id,))
        assert cursor.fetchone() == ("running",)


def test_late_unlisted_artifact_cannot_extend_accepted_set(store: PostgresRuntimeStore) -> None:
    binding = _unique_binding("closure-late-artifact")
    claim = store.claim_qc_collector_capability_command(binding.claim)
    outcome = store.commit_qc_collector_capability_success(
        CommandSuccess(claim.command_slot_id, binding.expected_set_hash, binding.members), binding
    )
    with pytest.raises(psycopg.errors.RaiseException, match="exactly two ordered members"):
        with psycopg.connect(DSN) as connection, connection.cursor() as cursor:
            _insert(cursor, "artifacts", {
                "artifact_id": uuid4(), "artifact_set_id": outcome.artifact_set_id,
                "job_id": outcome.job_id, "artifact_type": "unlisted",
                "logical_id": "extra", "revision": 1, "namespace": "other",
                "scope_kind": "other", "scope_key": "other",
                "content_hash": OTHER_HASH, "payload_json": Jsonb({}),
            })


@pytest.mark.parametrize("table", ["jobs", "command_slots"])
def test_open_validator_identity_cannot_escape_guard(store: PostgresRuntimeStore, table: str) -> None:
    binding = _unique_binding(f"closure-identity-escape-{table}")
    claim = store.claim_qc_collector_capability_command(binding.claim)
    with pytest.raises(psycopg.errors.RaiseException, match="identity is immutable"):
        with psycopg.connect(DSN) as connection, connection.cursor() as cursor:
            if table == "jobs":
                cursor.execute("UPDATE runtime.jobs SET job_key = 'escaped-validator' WHERE job_id = %s", (claim.job_id,))
            else:
                cursor.execute("UPDATE runtime.command_slots SET command_name = 'OtherCommand' WHERE command_slot_id = %s", (claim.command_slot_id,))


def _reset_verification_database(*, before_closure: bool) -> None:
    with psycopg.connect(DSN, autocommit=True) as connection, connection.cursor() as cursor:
        if connection.info.dbname != "ac_autocut_verify":
            pytest.fail("closure migration tests may reset only ac_autocut_verify")
        cursor.execute("DROP SCHEMA IF EXISTS storage CASCADE")
        cursor.execute("DROP SCHEMA IF EXISTS runtime CASCADE")
        for migration in sorted(MIGRATIONS.glob("*.sql")):
            if before_closure and migration.name >= CLOSURE_MIGRATION.name:
                break
            cursor.execute(migration.read_text(encoding="utf-8"))


@pytest.mark.parametrize("history", ["valid", "empty-running", "missing-row", "foreign-row", "payload", "non-success"])
def test_migration_preflight_checks_real_0059_history(history: str) -> None:
    try:
        _reset_verification_database(before_closure=True)
        binding = _unique_binding(f"closure-history-{history}")
        with psycopg.connect(DSN) as connection, connection.cursor() as cursor:
            if history == "empty-running":
                _stage(cursor, binding, state="running", with_output=False, include_capability=False)
            elif history == "foreign-row":
                foreign = _stage(cursor, _unique_binding("closure-history-foreign"), include_capability=False)
                _stage(cursor, binding, row_changes={"receipt_id": foreign["receipt_id"]})
            else:
                _stage(
                    cursor, binding,
                    include_capability=history not in {"missing-row", "non-success"},
                    state="running" if history == "non-success" else "succeeded",
                    member_changes={"payload_json": Jsonb({"forged": True})} if history == "payload" else None,
                )
        if history in {"valid", "empty-running"}:
            with psycopg.connect(DSN, autocommit=True) as connection, connection.cursor() as cursor:
                cursor.execute(CLOSURE_MIGRATION.read_text(encoding="utf-8"))
        else:
            with pytest.raises(psycopg.errors.RaiseException, match="0060 refuses invalid production QC capability history"):
                with psycopg.connect(DSN, autocommit=True) as connection, connection.cursor() as cursor:
                    cursor.execute(CLOSURE_MIGRATION.read_text(encoding="utf-8"))
            with psycopg.connect(DSN) as connection, connection.cursor() as cursor:
                cursor.execute("SELECT to_regprocedure('runtime.assert_production_qc_capability_job(uuid)')")
                assert cursor.fetchone() == (None,), "failed migration must roll back its new guards"
                cursor.execute("SELECT count(*) FROM runtime.jobs WHERE job_key = %s", (binding.job.job_key,))
                assert cursor.fetchone() == (1,), "preflight must not repair/delete corrupt history"
    finally:
        _reset_verification_database(before_closure=False)
