"""Focused execution-kind contract checks independent of a PostgreSQL server."""

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest
from autocut_kernel.store import (
    BlobRef,
    CommandClaim,
    CommandStateError,
    Job,
    StoreValidationError,
)
from autocut_kernel.store.postgres import PostgresRuntimeStore


def _digest(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode("utf-8")).hexdigest()


class _KindCursor:
    def __init__(self, result: tuple[object, ...] | None) -> None:
        self.result = result
        self.executed: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, query: str, params: tuple[object, ...] = ()) -> None:
        self.executed.append((query, params))

    def fetchone(self) -> tuple[object, ...] | None:
        return self.result


def test_command_claim_requires_explicit_closed_execution_kind() -> None:
    job = Job("execution-kind-unit", "test")

    with pytest.raises(TypeError):
        CommandClaim(job, "claim", "BuildNarrativeGraph@2.1.3", _digest("request"))
    with pytest.raises(StoreValidationError, match="execution_kind"):
        CommandClaim(
            job,
            "claim",
            "BuildNarrativeGraph@2.1.3",
            _digest("request"),
            execution_kind="provider",  # type: ignore[arg-type]
        )


def test_generation_kind_helper_accepts_arbitrary_generation_name_without_name_lookup() -> None:
    cursor = _KindCursor(("generation",))

    PostgresRuntimeStore._require_slot_execution_kind(cursor, uuid4(), "generation")

    assert len(cursor.executed) == 1
    assert "execution_kind" in cursor.executed[0][0]
    assert "command_name" not in cursor.executed[0][0]


def test_generation_kind_helper_rejects_deterministic_slot() -> None:
    with pytest.raises(CommandStateError, match="execution kind must be generation"):
        PostgresRuntimeStore._require_slot_execution_kind(
            _KindCursor(("deterministic",)), uuid4(), "generation"
        )


def test_reserve_generation_attempt_uses_slot_kind_not_command_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = PostgresRuntimeStore(lambda: None)  # type: ignore[arg-type]
    slot_id, job_id = uuid4(), uuid4()
    request_hash = _digest("request")
    cursor = _KindCursor(("deterministic",))
    monkeypatch.setattr(
        store,
        "_transaction",
        lambda operation: operation(cursor),
    )
    monkeypatch.setattr(
        store,
        "_locked_job_then_slot",
        lambda _cursor, _slot_id: (job_id, "running", "BuildNarrativeGraph@2.1.3", request_hash),
    )

    with pytest.raises(CommandStateError, match="execution kind must be generation"):
        store.reserve_generation_attempt(
            slot_id,
            request_hash,
            provider_id="fixture-provider",
            provider_idempotency_key="fixture-key",
            request_payload=BlobRef(uuid4(), _digest("payload"), 7, "application/json"),
        )

    assert len(cursor.executed) == 1
    assert "execution_kind" in cursor.executed[0][0]
    assert "command_name" not in cursor.executed[0][0]


def test_execution_kind_migration_has_closed_backfill_and_deferred_kind_constraints() -> None:
    sql = Path("packages/autocut-kernel/migrations/0018_command_execution_kind.sql").read_text()

    assert "ADD COLUMN execution_kind text" in sql
    assert "ALTER COLUMN execution_kind SET NOT NULL" in sql
    assert "DEFAULT" not in sql
    assert "DISABLE TRIGGER runtime_terminal_slot_no_rewrite" in sql
    assert "SET CONSTRAINTS ALL IMMEDIATE" in sql
    assert sql.index("SET CONSTRAINTS ALL IMMEDIATE") < sql.index(
        "ENABLE TRIGGER runtime_terminal_slot_no_rewrite"
    ) < sql.index("ALTER COLUMN execution_kind SET NOT NULL")
    assert "slot.execution_kind = 'generation'" in sql
    assert "checked_execution_kind <> 'generation'" in sql
    assert "NEW.execution_kind IS DISTINCT FROM OLD.execution_kind" in sql
    assert "slot.command_name = 'GenerateVlmEvidenceCommand'" not in sql.split(
        "CREATE OR REPLACE FUNCTION runtime.assert_generation_attempt_integrity()"
    )[1]


def test_every_store_owned_slot_insert_sets_execution_kind() -> None:
    source = Path("packages/autocut-kernel/src/autocut_kernel/store/postgres.py").read_text()
    inserts = source.split("INSERT INTO runtime.command_slots")[1:]

    assert len(inserts) == 2
    assert all("execution_kind" in insert.split("VALUES", maxsplit=1)[0] for insert in inserts)
    assert "'deterministic', 'running'" in inserts[1]
