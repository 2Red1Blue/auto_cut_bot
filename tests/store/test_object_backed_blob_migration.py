"""Static shape checks for the backwards-compatible large-object migration.

The subsequent Store slice supplies disposable-PostgreSQL round-trip tests.
These checks prevent the migration itself from silently putting rendered media
back into ``bytea`` or weakening the existing immutable trigger.
"""

from pathlib import Path

MIGRATION = Path("packages/autocut-kernel/migrations/0054_object_backed_blob_metadata.sql")


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_large_object_metadata_preserves_inline_rows_without_rewriting_bytes() -> None:
    sql = _sql()

    assert "ALTER COLUMN content_bytes DROP NOT NULL" in sql
    assert "storage_kind text NOT NULL DEFAULT 'postgres_inline'" in sql
    assert "storage_kind IN ('postgres_inline', 's3_compatible')" in sql
    assert "storage_kind = 'postgres_inline'" in sql
    assert "content_bytes IS NOT NULL" in sql
    assert "UPDATE storage.blob_objects" not in sql
    assert "DROP TRIGGER" not in sql
    assert "DROP FUNCTION" not in sql


def test_external_shape_is_locator_only_verified_and_immutable() -> None:
    sql = _sql()

    assert "storage_kind = 's3_compatible'" in sql
    assert "content_bytes IS NULL" in sql
    assert "byte_length > 0" in sql
    assert "write_strategy = 's3-single-put-v1'" in sql
    assert "verified_at IS NOT NULL" in sql
    assert "storage_locator !~ '(^|/)\\.\\.(/|$)'" in sql
    assert "storage_blob_objects_external_locator_unique" in sql
    assert "WHERE storage_kind = 's3_compatible'" in sql
    assert "not publication authority" in sql


def test_external_write_requires_a_durable_one_way_reservation() -> None:
    sql = _sql()

    assert "CREATE TABLE storage.object_write_intents" in sql
    assert "reservation_token uuid NOT NULL" in sql
    assert "state text NOT NULL CHECK (state IN ('reserved', 'resolved'))" in sql
    assert "resolved_object_id uuid REFERENCES storage.blob_objects" in sql
    assert "UNIQUE (storage_backend_id, storage_region, storage_locator)" in sql
    assert "NEW.version <> OLD.version + 1" in sql
    assert "resolved object write intents are immutable" in sql
    assert "BEFORE INSERT OR UPDATE OR DELETE ON storage.object_write_intents" in sql
    assert "object write intents must begin reserved at version zero" in sql


def test_migration_does_not_create_visibility_or_publication_state() -> None:
    sql = _sql().lower()

    for forbidden in (
        "publish_decision",
        "local_visibility",
        "current.json",
        "publication_allow",
    ):
        assert forbidden not in sql
