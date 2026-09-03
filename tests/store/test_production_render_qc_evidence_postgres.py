"""Disposable PostgreSQL acceptance for durable production QC evidence."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import uuid4

import pytest
from autocut_kernel.store import (
    PRODUCTION_RENDER_QC_CHECK_SET_VERSION,
    PRODUCTION_RENDER_QC_REQUIRED_CHECKS,
    BlobIntegrityError,
    BlobRef,
    CommandStateError,
    CommittedArtifactMemberReference,
    IdempotencyConflictError,
    Job,
    PostgresRuntimeStore,
    ProductionRenderAttempt,
    ProductionRenderQcCheckEvidence,
    ProductionRenderQcEvidenceReport,
    ProductionRenderQcLease,
    RuntimeStoreError,
)

from tests.store.test_production_render_attempt_postgres import _hash
from tests.store.test_production_render_qc_attempt_postgres import (
    _assert_render_command_open_without_receipt,
    _rejection,
    _rendered_parent,
    _wait_for_application_lock,
    _wait_for_qc_lease_expiry,
)
from tests.store.test_production_render_qc_attempt_postgres import (
    migrated_database as migrated_database,
)
from tests.store.test_production_render_qc_attempt_postgres import (
    qc_stage4_authority as qc_stage4_authority,
)

psycopg = pytest.importorskip("psycopg")

DSN = os.environ.get("AUTOCUT_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(
    not DSN,
    reason="set AUTOCUT_TEST_POSTGRES_DSN to disposable ac_autocut_verify",
)
def _reserve_qc(
    store: PostgresRuntimeStore,
    parent: ProductionRenderAttempt,
):
    return store.reserve_production_render_qc_attempt(
        parent.attempt_id,
        expected_render_version=parent.version,
        qc_policy_sha256=_hash(b"production-qc-policy"),
        required_check_set_version=PRODUCTION_RENDER_QC_CHECK_SET_VERSION,
        qc_runner_identity_sha256=_hash(b"production-qc-runner"),
    )


def _report(
    store: PostgresRuntimeStore,
    job: Job,
    parent: ProductionRenderAttempt,
    lease: ProductionRenderQcLease,
    *,
    suffix: str,
) -> ProductionRenderQcEvidenceReport:
    checks: list[ProductionRenderQcCheckEvidence] = []
    for ordinal, check_id in enumerate(PRODUCTION_RENDER_QC_REQUIRED_CHECKS):
        content = json.dumps(
            {"check_id": check_id, "fixture": suffix},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        evidence_blob = store.put_immutable_blob(
            job,
            content=content,
            content_hash=_hash(content),
            media_type="application/json",
        )
        checks.append(
            ProductionRenderQcCheckEvidence(
                check_ordinal=ordinal,
                check_id=check_id,
                collection_status="completed",
                coverage="full_file",
                parser_schema_version="fixture-json-v1",
                tool_identity_sha256=_hash(b"fixture-tool"),
                argv_sha256=_hash(("argv:" + check_id).encode("utf-8")),
                measurements=(),
                evidence_blob=evidence_blob,
            )
        )
    qc_attempt = store.read_production_render_qc_attempt(lease.qc_attempt_id)
    return ProductionRenderQcEvidenceReport(
        qc_attempt_id=lease.qc_attempt_id,
        render_attempt_id=lease.render_attempt_id,
        job_id=lease.job_id,
        command_slot_id=lease.command_slot_id,
        output_blob=parent.output_blob,
        render_facts_sha256=parent.render_facts_sha256,
        qc_policy_sha256=qc_attempt.qc_policy_sha256,
        required_check_set_version=qc_attempt.required_check_set_version,
        qc_runner_identity_sha256=qc_attempt.qc_runner_identity_sha256,
        checks=tuple(checks),
    )


def _leased_case(
    authority: tuple[
        PostgresRuntimeStore,
        Job,
        CommittedArtifactMemberReference,
    ],
    *,
    suffix: str,
    lease_seconds: int = 60,
) -> tuple[
    PostgresRuntimeStore,
    Job,
    ProductionRenderAttempt,
    ProductionRenderQcLease,
    ProductionRenderQcEvidenceReport,
]:
    store, job, recipe = authority
    parent = _rendered_parent(store, job, recipe, suffix=suffix)
    attempt = _reserve_qc(store, parent)
    lease = store.acquire_production_render_qc_lease(
        attempt.qc_attempt_id,
        expected_version=attempt.version,
        lease_seconds=lease_seconds,
    )
    assert lease is not None
    return store, job, parent, lease, _report(store, job, parent, lease, suffix=suffix)


def _different_report(
    report: ProductionRenderQcEvidenceReport,
) -> ProductionRenderQcEvidenceReport:
    first = replace(
        report.checks[0],
        argv_sha256=_hash(b"substituted-argv"),
    )
    return replace(report, checks=(first, *report.checks[1:]))


def test_success_restart_and_exact_post_ack_replay_are_private(
    qc_stage4_authority: tuple[
        PostgresRuntimeStore,
        Job,
        CommittedArtifactMemberReference,
    ],
) -> None:
    store, _job, parent, lease, report = _leased_case(
        qc_stage4_authority,
        suffix="evidence-success",
    )

    recorded = store.record_production_render_qc_evidence(lease, report)
    replay = store.record_production_render_qc_evidence(lease, report)
    restarted = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    reread = restarted.read_production_render_qc_attempt(recorded.qc_attempt_id)

    assert recorded.state == "evidence_ready"
    assert recorded.version == lease.version + 1
    assert recorded.lease_expires_at is None
    assert recorded.evidence_report == report
    assert recorded.evidence_report_sha256 == report.canonical_hash
    assert replay == recorded
    assert reread == recorded
    assert not hasattr(reread, "token")
    assert not hasattr(reread, "lease_token")
    assert not hasattr(reread.output_blob, "storage_locator")
    assert store.read_production_render_attempt(parent.attempt_id) == parent
    _assert_render_command_open_without_receipt(parent)

    assert DSN is not None
    with psycopg.connect(DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM runtime.production_render_qc_evidence_members "
            "WHERE qc_attempt_id = %s",
            (recorded.qc_attempt_id,),
        )
        assert cursor.fetchone() == (len(PRODUCTION_RENDER_QC_REQUIRED_CHECKS),)
        cursor.execute(
            "SELECT count(*) FROM runtime.artifact_sets WHERE command_slot_id = %s",
            (parent.command_slot_id,),
        )
        assert cursor.fetchone() == (0,)


def test_post_ack_substitution_conflicts_and_old_takeover_lease_is_stale(
    qc_stage4_authority: tuple[
        PostgresRuntimeStore,
        Job,
        CommittedArtifactMemberReference,
    ],
) -> None:
    store, job, parent, first, _initial_report = _leased_case(
        qc_stage4_authority,
        suffix="evidence-takeover",
        lease_seconds=1,
    )
    _wait_for_qc_lease_expiry(first.qc_attempt_id)
    expired = store.read_production_render_qc_attempt(first.qc_attempt_id)
    takeover = store.acquire_production_render_qc_lease(
        expired.qc_attempt_id,
        expected_version=expired.version,
        lease_seconds=60,
    )
    assert takeover is not None
    takeover_report = _report(
        store,
        job,
        parent,
        takeover,
        suffix="evidence-takeover-final",
    )
    recorded = store.record_production_render_qc_evidence(takeover, takeover_report)

    with pytest.raises(CommandStateError, match="stale|owned elsewhere"):
        store.record_production_render_qc_evidence(first, takeover_report)
    with pytest.raises(IdempotencyConflictError, match="replay differs"):
        store.record_production_render_qc_evidence(
            takeover,
            _different_report(takeover_report),
        )
    assert store.read_production_render_qc_attempt(recorded.qc_attempt_id) == recorded


def test_expired_and_renewed_stale_leases_cannot_attach_evidence(
    qc_stage4_authority: tuple[
        PostgresRuntimeStore,
        Job,
        CommittedArtifactMemberReference,
    ],
) -> None:
    store, _job, _parent, lease, report = _leased_case(
        qc_stage4_authority,
        suffix="evidence-expired",
        lease_seconds=1,
    )
    renewed = store.renew_production_render_qc_lease(lease, lease_seconds=2)
    with pytest.raises(CommandStateError, match="stale|owned elsewhere"):
        store.record_production_render_qc_evidence(lease, report)
    _wait_for_qc_lease_expiry(renewed.qc_attempt_id)
    with pytest.raises(CommandStateError, match="expired|CAS"):
        store.record_production_render_qc_evidence(renewed, report)


def test_wrong_token_and_report_binding_cannot_attach_evidence(
    qc_stage4_authority: tuple[
        PostgresRuntimeStore,
        Job,
        CommittedArtifactMemberReference,
    ],
) -> None:
    store, _job, _parent, lease, report = _leased_case(
        qc_stage4_authority,
        suffix="evidence-bindings",
    )
    with pytest.raises(CommandStateError, match="stale|owned elsewhere"):
        store.record_production_render_qc_evidence(
            replace(lease, token=uuid4()),
            report,
        )
    with pytest.raises(RuntimeStoreError, match="disagrees with reserved authority"):
        store.record_production_render_qc_evidence(
            lease,
            replace(report, job_id=uuid4()),
        )
    attempt = store.read_production_render_qc_attempt(lease.qc_attempt_id)
    assert attempt.state == "scanning"
    assert attempt.version == lease.version


def test_missing_wrong_job_and_tampered_evidence_bytes_fail_closed(
    qc_stage4_authority: tuple[
        PostgresRuntimeStore,
        Job,
        CommittedArtifactMemberReference,
    ],
) -> None:
    assert DSN is not None
    store, job, _parent, lease, report = _leased_case(
        qc_stage4_authority,
        suffix="evidence-invalid-blob",
    )
    missing_ref = BlobRef(uuid4(), _hash(b"missing"), 7, "application/json")
    missing_report = replace(
        report,
        checks=(replace(report.checks[0], evidence_blob=missing_ref), *report.checks[1:]),
    )
    with pytest.raises(BlobIntegrityError, match="not claimed"):
        store.record_production_render_qc_evidence(lease, missing_report)

    foreign_job = Job(f"foreign-evidence-{uuid4()}", "production")
    foreign_content = b'{"foreign":true}'
    foreign_ref = store.put_immutable_blob(
        foreign_job,
        content=foreign_content,
        content_hash=_hash(foreign_content),
        media_type="application/json",
    )
    foreign_report = replace(
        report,
        checks=(replace(report.checks[0], evidence_blob=foreign_ref), *report.checks[1:]),
    )
    with pytest.raises(BlobIntegrityError, match="not claimed"):
        store.record_production_render_qc_evidence(lease, foreign_report)

    target = report.checks[0].evidence_blob
    tampered = b"x" * target.byte_length
    with psycopg.connect(DSN, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute("ALTER TABLE storage.blob_objects DISABLE TRIGGER USER")
        try:
            cursor.execute(
                """UPDATE storage.blob_objects
                      SET content_bytes = %s, content_hash = %s
                    WHERE object_id = %s""",
                (tampered, _hash(tampered), target.object_id),
            )
            with pytest.raises(RuntimeStoreError, match="does not match durable blob metadata"):
                store.record_production_render_qc_evidence(lease, report)
        finally:
            original = json.dumps(
                {"check_id": report.checks[0].check_id, "fixture": "evidence-invalid-blob"},
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            cursor.execute(
                """UPDATE storage.blob_objects
                      SET content_bytes = %s, content_hash = %s
                    WHERE object_id = %s""",
                (original, target.content_hash, target.object_id),
            )
            cursor.execute("ALTER TABLE storage.blob_objects ENABLE TRIGGER USER")

    recorded = store.record_production_render_qc_evidence(lease, report)
    assert recorded.state == "evidence_ready"


def test_lock_wait_expiry_cannot_attach_members_or_report(
    qc_stage4_authority: tuple[
        PostgresRuntimeStore,
        Job,
        CommittedArtifactMemberReference,
    ],
) -> None:
    assert DSN is not None
    store, _job, _parent, lease, report = _leased_case(
        qc_stage4_authority,
        suffix="evidence-lock-wait",
        lease_seconds=1,
    )
    application_name = f"qc-evidence-lock-wait-{uuid4()}"
    waiting_store = PostgresRuntimeStore(
        lambda: psycopg.connect(DSN, application_name=application_name)
    )
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        with psycopg.connect(DSN) as blocker, blocker.cursor() as cursor:
            cursor.execute(
                "SELECT job_id FROM runtime.jobs WHERE job_id = %s FOR UPDATE",
                (lease.job_id,),
            )
            future = executor.submit(
                waiting_store.record_production_render_qc_evidence,
                lease,
                report,
            )
            _wait_for_application_lock(application_name)
            _wait_for_qc_lease_expiry(lease.qc_attempt_id)
        with pytest.raises(CommandStateError, match="expired|CAS"):
            future.result(timeout=5)
    finally:
        executor.shutdown(wait=True)

    attempt = store.read_production_render_qc_attempt(lease.qc_attempt_id)
    assert attempt.state == "scanning"
    with psycopg.connect(DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM runtime.production_render_qc_evidence_members "
            "WHERE qc_attempt_id = %s",
            (lease.qc_attempt_id,),
        )
        assert cursor.fetchone() == (0,)


def test_direct_sql_partial_member_mutation_and_delete_are_rejected(
    qc_stage4_authority: tuple[
        PostgresRuntimeStore,
        Job,
        CommittedArtifactMemberReference,
    ],
) -> None:
    assert DSN is not None
    store, _job, _parent, lease, report = _leased_case(
        qc_stage4_authority,
        suffix="evidence-sql-guard",
    )
    first = report.checks[0]
    with pytest.raises(
        psycopg.Error,
        match="member or same-Job Blob claim is not exact",
    ):
        with psycopg.connect(DSN) as connection, connection.cursor() as cursor:
            cursor.execute(
                """UPDATE runtime.production_render_qc_attempts
                      SET state = 'evidence_ready', version = version + 1,
                          lease_token = NULL, lease_expires_at = NULL,
                          evidence_report_json = %s,
                          evidence_report_sha256 = %s
                    WHERE qc_attempt_id = %s""",
                (report.canonical_json, report.canonical_hash, lease.qc_attempt_id),
            )

    with pytest.raises(psycopg.Error, match="evidence-ready|complete|journal"):
        with psycopg.connect(DSN) as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO runtime.production_render_qc_evidence_members "
                "(qc_attempt_id, check_ordinal, check_id, evidence_object_id, "
                "evidence_content_hash, evidence_byte_length, evidence_media_type) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    lease.qc_attempt_id,
                    first.check_ordinal,
                    first.check_id,
                    first.evidence_blob.object_id,
                    first.evidence_blob.content_hash,
                    first.evidence_blob.byte_length,
                    first.evidence_blob.media_type,
                ),
            )

    recorded = store.record_production_render_qc_evidence(lease, report)
    with pytest.raises(psycopg.Error, match="immutable and non-deletable"):
        with psycopg.connect(DSN) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE runtime.production_render_qc_evidence_members "
                "SET check_id = 'mutated' WHERE qc_attempt_id = %s",
                (recorded.qc_attempt_id,),
            )
    with pytest.raises(psycopg.Error, match="immutable and non-deletable"):
        with psycopg.connect(DSN) as connection, connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM runtime.production_render_qc_evidence_members "
                "WHERE qc_attempt_id = %s",
                (recorded.qc_attempt_id,),
            )


def test_restart_reread_detects_evidence_byte_tamper_and_parent_stays_barred(
    qc_stage4_authority: tuple[
        PostgresRuntimeStore,
        Job,
        CommittedArtifactMemberReference,
    ],
) -> None:
    assert DSN is not None
    store, _job, parent, lease, report = _leased_case(
        qc_stage4_authority,
        suffix="evidence-reread-tamper",
    )
    recorded = store.record_production_render_qc_evidence(lease, report)
    target = report.checks[-1].evidence_blob
    with psycopg.connect(DSN, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT content_bytes, content_hash
                 FROM storage.blob_objects
                WHERE object_id = %s""",
            (target.object_id,),
        )
        original = cursor.fetchone()
        assert original is not None
        cursor.execute("ALTER TABLE storage.blob_objects DISABLE TRIGGER USER")
        try:
            tampered = b"z" * target.byte_length
            cursor.execute(
                """UPDATE storage.blob_objects
                      SET content_bytes = %s, content_hash = %s
                    WHERE object_id = %s""",
                (tampered, _hash(tampered), target.object_id),
            )
            restarted = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
            with pytest.raises(RuntimeStoreError, match="does not match durable blob metadata"):
                restarted.read_production_render_qc_attempt(recorded.qc_attempt_id)
        finally:
            cursor.execute(
                """UPDATE storage.blob_objects
                      SET content_bytes = %s, content_hash = %s
                    WHERE object_id = %s""",
                (original[0], original[1], target.object_id),
            )
            cursor.execute("ALTER TABLE storage.blob_objects ENABLE TRIGGER USER")

    with pytest.raises(RuntimeStoreError, match="database operation failed"):
        store.commit_production_render_rejection(
            parent.attempt_id,
            expected_version=parent.version,
            rejection=_rejection(parent, "denied"),
        )
    assert store.read_production_render_attempt(parent.attempt_id) == parent
    assert store.read_production_render_qc_attempt(recorded.qc_attempt_id) == recorded
    _assert_render_command_open_without_receipt(parent)


def test_restart_reread_rejects_blob_metadata_and_report_member_drift(
    qc_stage4_authority: tuple[
        PostgresRuntimeStore,
        Job,
        CommittedArtifactMemberReference,
    ],
) -> None:
    assert DSN is not None
    store, _job, _parent, lease, report = _leased_case(
        qc_stage4_authority,
        suffix="evidence-metadata-drift",
    )
    recorded = store.record_production_render_qc_evidence(lease, report)
    restarted = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    target = report.checks[0].evidence_blob

    with psycopg.connect(DSN, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute("ALTER TABLE storage.blob_objects DISABLE TRIGGER USER")
        try:
            cursor.execute(
                "UPDATE storage.blob_objects SET media_type = 'application/octet-stream' "
                "WHERE object_id = %s",
                (target.object_id,),
            )
            with pytest.raises(BlobIntegrityError, match="durable blob metadata"):
                restarted.read_production_render_qc_attempt(recorded.qc_attempt_id)
        finally:
            cursor.execute(
                "UPDATE storage.blob_objects SET media_type = %s WHERE object_id = %s",
                (target.media_type, target.object_id),
            )
            cursor.execute("ALTER TABLE storage.blob_objects ENABLE TRIGGER USER")

        drifted_first = replace(
            report.checks[0],
            evidence_blob=report.checks[1].evidence_blob,
        )
        drifted_report = replace(
            report,
            checks=(drifted_first, *report.checks[1:]),
        )
        cursor.execute(
            "ALTER TABLE runtime.production_render_qc_attempts DISABLE TRIGGER USER"
        )
        try:
            cursor.execute(
                "UPDATE runtime.production_render_qc_attempts "
                "SET evidence_report_json = %s, evidence_report_sha256 = %s "
                "WHERE qc_attempt_id = %s",
                (
                    drifted_report.canonical_json,
                    drifted_report.canonical_hash,
                    recorded.qc_attempt_id,
                ),
            )
            with pytest.raises(RuntimeStoreError, match="members disagree"):
                restarted.read_production_render_qc_attempt(recorded.qc_attempt_id)
        finally:
            cursor.execute(
                "UPDATE runtime.production_render_qc_attempts "
                "SET evidence_report_json = %s, evidence_report_sha256 = %s "
                "WHERE qc_attempt_id = %s",
                (report.canonical_json, report.canonical_hash, recorded.qc_attempt_id),
            )
            cursor.execute(
                "ALTER TABLE runtime.production_render_qc_attempts ENABLE TRIGGER USER"
            )

    assert restarted.read_production_render_qc_attempt(recorded.qc_attempt_id) == recorded
