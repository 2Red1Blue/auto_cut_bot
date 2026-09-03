"""Opt-in PostgreSQL acceptance for a restricted Runtime login role.

The tracked migrations currently do not create a Runtime role.  Consequently
this suite never manufactures one or grants privileges to make its own oracle
pass.  The database fixture must provision the candidate role and expose its
credentials through both ``TEST_AUTOCUT_RUNTIME_ROLE_DSN`` and
``TEST_AUTOCUT_RUNTIME_ROLE_NAME``.

Only session-local temporary data is written.  All probes against durable
Task03 Store relations are either privilege introspection or zero-row DML, so
the suite is safe to run against the repository's disposable verification
database without seeding authority-owned facts.
"""

from __future__ import annotations

import os
from collections.abc import Iterable

import pytest

psycopg = pytest.importorskip("psycopg")

RUNTIME_ROLE_DSN = os.environ.get("TEST_AUTOCUT_RUNTIME_ROLE_DSN")
RUNTIME_ROLE_NAME = os.environ.get("TEST_AUTOCUT_RUNTIME_ROLE_NAME")

pytestmark = pytest.mark.skipif(
    not RUNTIME_ROLE_DSN or not RUNTIME_ROLE_NAME,
    reason=(
        "set TEST_AUTOCUT_RUNTIME_ROLE_DSN and TEST_AUTOCUT_RUNTIME_ROLE_NAME "
        "to run restricted-role PostgreSQL acceptance"
    ),
)

_AUTHORITY_OWNED_RELATIONS = (
    "runtime.jobs",
    "runtime.command_slots",
    "runtime.artifact_sets",
    "runtime.artifacts",
    "runtime.artifact_set_members",
    "runtime.command_receipts",
    "runtime.logical_heads",
)


def _assert_no_privileges(
    cursor: object,
    *,
    relation_oid: int,
    privileges: Iterable[str],
) -> None:
    for privilege in privileges:
        cursor.execute(  # type: ignore[attr-defined]
            "SELECT has_table_privilege(current_user, %s, %s)",
            (relation_oid, privilege),
        )
        assert cursor.fetchone() == (False,)  # type: ignore[attr-defined]


def test_runtime_role_identity_and_minimal_session_local_read_write() -> None:
    """The fixture is the named restricted role, not a privileged substitute."""

    assert RUNTIME_ROLE_DSN is not None
    assert RUNTIME_ROLE_NAME is not None
    with psycopg.connect(RUNTIME_ROLE_DSN, autocommit=True) as connection:
        if connection.info.dbname != "ac_autocut_verify":
            pytest.fail(
                "TEST_AUTOCUT_RUNTIME_ROLE_DSN must name disposable "
                "ac_autocut_verify, never a production database"
            )
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT current_user, session_user, rolsuper, rolcreaterole,
                       rolcreatedb, rolbypassrls
                  FROM pg_roles
                 WHERE rolname = current_user
                """
            )
            assert cursor.fetchone() == (
                RUNTIME_ROLE_NAME,
                RUNTIME_ROLE_NAME,
                False,
                False,
                False,
                False,
            )

            # Runtime-local scratch work is the only write capability assumed
            # here.  No durable business relation is needed for this proof.
            cursor.execute(
                """
                CREATE TEMP TABLE autocut_runtime_role_probe (
                    marker text NOT NULL
                ) ON COMMIT PRESERVE ROWS
                """
            )
            cursor.execute("INSERT INTO autocut_runtime_role_probe (marker) VALUES ('allowed')")
            cursor.execute("SELECT marker FROM autocut_runtime_role_probe")
            assert cursor.fetchone() == ("allowed",)


def test_runtime_role_has_no_table_or_authority_column_dml() -> None:
    """Catalog grants and executable probes both deny direct Store mutation."""

    assert RUNTIME_ROLE_DSN is not None
    with psycopg.connect(RUNTIME_ROLE_DSN, autocommit=True) as connection:
        with connection.cursor() as cursor:
            for relation in _AUTHORITY_OWNED_RELATIONS:
                cursor.execute("SELECT to_regclass(%s)::oid", (relation,))
                row = cursor.fetchone()
                assert row is not None and row[0] is not None, (
                    f"tracked Task03 relation {relation} is absent; apply the real migrations"
                )
                _assert_no_privileges(
                    cursor,
                    relation_oid=row[0],
                    privileges=("INSERT", "UPDATE", "DELETE", "TRUNCATE"),
                )
                cursor.execute(
                    """
                    SELECT attname,
                           has_column_privilege(current_user, attrelid, attnum, 'INSERT'),
                           has_column_privilege(current_user, attrelid, attnum, 'UPDATE')
                      FROM pg_attribute
                     WHERE attrelid = %s
                       AND attnum > 0
                       AND NOT attisdropped
                     ORDER BY attnum
                    """,
                    (row[0],),
                )
                column_privileges = cursor.fetchall()
                assert column_privileges, f"{relation} has no inspectable columns"
                assert all(
                    insert_allowed is False and update_allowed is False
                    for _, insert_allowed, update_allowed in column_privileges
                ), f"{relation} exposes authority-owned column DML: {column_privileges!r}"

            denied_statements = (
                """
                INSERT INTO runtime.jobs (job_id, job_key, profile, state)
                SELECT '00000000-0000-0000-0000-000000000001',
                       'runtime-role-forbidden', 'test', 'pending'
                 WHERE false
                """,
                "UPDATE runtime.jobs SET state = 'failed' WHERE false",
                "DELETE FROM runtime.jobs WHERE false",
            )
            for statement in denied_statements:
                with pytest.raises(psycopg.errors.InsufficientPrivilege) as denied:
                    cursor.execute(statement)
                assert denied.value.sqlstate == "42501"


def test_denied_authority_write_aborts_transaction_without_residue() -> None:
    """A denied durable write rolls back earlier permitted local work too."""

    assert RUNTIME_ROLE_DSN is not None
    with psycopg.connect(RUNTIME_ROLE_DSN, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TEMP TABLE autocut_runtime_role_rollback_probe (
                    marker text PRIMARY KEY
                ) ON COMMIT PRESERVE ROWS
                """
            )

            connection.autocommit = False
            cursor.execute(
                "INSERT INTO autocut_runtime_role_rollback_probe (marker) VALUES ('must-rollback')"
            )
            with pytest.raises(psycopg.errors.InsufficientPrivilege) as denied:
                cursor.execute("UPDATE runtime.command_slots SET state = 'failed' WHERE false")
            assert denied.value.sqlstate == "42501"
            connection.rollback()

            cursor.execute("SELECT count(*) FROM autocut_runtime_role_rollback_probe")
            assert cursor.fetchone() == (0,)
            connection.rollback()
