"""Static closure checks for durable production Render QC evidence."""

import hashlib
import json
import re
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import get_args
from uuid import uuid4

import pytest
from autocut_kernel.store import (
    PRODUCTION_RENDER_QC_CHECK_SET_VERSION,
    PRODUCTION_RENDER_QC_EVIDENCE_SCHEMA_VERSION,
    PRODUCTION_RENDER_QC_REQUIRED_CHECKS,
    BlobRef,
    ProductionRenderQcAttempt,
    ProductionRenderQcCheckEvidence,
    ProductionRenderQcEvidenceReport,
    ProductionRenderQcMeasurement,
    ProductionRenderQcMeasurementKind,
    ProductionRenderQcMeasurementUnit,
    StoreValidationError,
)

MIGRATION = Path(
    "packages/autocut-kernel/migrations/0058_production_render_qc_evidence.sql"
)
SHA256_A = "sha256:" + "a" * 64
SHA256_B = "sha256:" + "b" * 64


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _measurement(**changes: object) -> ProductionRenderQcMeasurement:
    measurement = ProductionRenderQcMeasurement(
        name="frame_count",
        value_kind="integer",
        value="1",
        unit="count",
    )
    return replace(measurement, **changes)


def _check(
    ordinal: int,
    *,
    measurements: tuple[ProductionRenderQcMeasurement, ...] | None = None,
    evidence_byte_length: int = 128,
) -> ProductionRenderQcCheckEvidence:
    return ProductionRenderQcCheckEvidence(
        check_ordinal=ordinal,
        check_id=PRODUCTION_RENDER_QC_REQUIRED_CHECKS[ordinal],
        collection_status="completed",
        coverage="full_file",
        parser_schema_version="ffprobe-json-v1",
        tool_identity_sha256=SHA256_A,
        argv_sha256=SHA256_B,
        measurements=measurements if measurements is not None else (_measurement(),),
        evidence_blob=BlobRef(
            uuid4(),
            SHA256_A,
            evidence_byte_length,
            "application/json",
        ),
    )


def _report(
    *,
    checks: tuple[ProductionRenderQcCheckEvidence, ...] | None = None,
) -> ProductionRenderQcEvidenceReport:
    return ProductionRenderQcEvidenceReport(
        qc_attempt_id=uuid4(),
        render_attempt_id=uuid4(),
        job_id=uuid4(),
        command_slot_id=uuid4(),
        output_blob=BlobRef(uuid4(), SHA256_A, 4096, "video/mp4"),
        render_facts_sha256=SHA256_A,
        qc_policy_sha256=SHA256_B,
        required_check_set_version=PRODUCTION_RENDER_QC_CHECK_SET_VERSION,
        qc_runner_identity_sha256=SHA256_A,
        checks=(
            checks
            if checks is not None
            else tuple(_check(index) for index in range(len(PRODUCTION_RENDER_QC_REQUIRED_CHECKS)))
        ),
    )


def _evidence_ready_attempt(
    report: ProductionRenderQcEvidenceReport | None = None,
) -> ProductionRenderQcAttempt:
    evidence = report if report is not None else _report()
    now = datetime.now(timezone.utc)
    return ProductionRenderQcAttempt(
        qc_attempt_id=evidence.qc_attempt_id,
        render_attempt_id=evidence.render_attempt_id,
        job_id=evidence.job_id,
        command_slot_id=evidence.command_slot_id,
        rendered_version=2,
        output_blob=evidence.output_blob,
        render_facts_sha256=evidence.render_facts_sha256,
        qc_policy_sha256=evidence.qc_policy_sha256,
        required_check_set_version=evidence.required_check_set_version,
        qc_runner_identity_sha256=evidence.qc_runner_identity_sha256,
        state="evidence_ready",
        version=2,
        reserved_at=now,
        evidence_report=evidence,
        evidence_report_sha256=evidence.canonical_hash,
        evidence_ready_at=now,
    )


def test_evidence_ready_shape_is_closed_bounded_and_lease_free() -> None:
    sql = _sql()

    assert "ADD COLUMN evidence_report_json text" in sql
    assert "ADD COLUMN evidence_report_sha256 text" in sql
    assert "ADD COLUMN evidence_ready_at timestamptz" in sql
    assert "state IN ('reserved', 'scanning', 'evidence_ready')" in sql
    assert "octet_length(evidence_report_json) BETWEEN 1 AND 1048576" in sql
    assert "state = 'evidence_ready' AND version >= 2" in sql
    assert "lease_token IS NULL AND lease_expires_at IS NULL" in sql
    assert "NEW.evidence_ready_at := clock_timestamp()" in sql


def test_scanning_to_evidence_ready_is_exact_active_and_one_way() -> None:
    sql = _sql()

    assert "OLD.state = 'scanning' AND NEW.state = 'evidence_ready'" in sql
    assert "OLD.lease_expires_at <= clock_timestamp()" in sql
    assert "NEW.version <> OLD.version + 1" in sql
    assert "NEW.lease_token IS NOT NULL" in sql
    assert "NEW.lease_expires_at IS NOT NULL" in sql
    assert "evidence-ready production render QC attempts are immutable" in sql
    assert "production render QC attempt identity is immutable" in sql
    assert "OLD.state = 'reserved' AND NEW.state = 'scanning'" in sql
    assert "OLD.state = 'scanning' AND NEW.state = 'scanning'" in sql
    assert "active production render QC lease cannot be taken over" in sql
    assert "transaction_timestamp()" not in sql


def test_evidence_member_table_is_exact_immutable_and_strictly_capped() -> None:
    sql = _sql()

    assert "CREATE TABLE runtime.production_render_qc_evidence_members" in sql
    assert "PRIMARY KEY (qc_attempt_id, check_ordinal)" in sql
    assert "UNIQUE (qc_attempt_id, check_id)" in sql
    assert "REFERENCES runtime.production_render_qc_attempts (qc_attempt_id)" in sql
    assert "evidence_byte_length BETWEEN 1 AND 2097152" in sql
    assert "evidence_media_type text NOT NULL" in sql
    assert "evidence_media_type = 'application/json'" in sql
    assert "BEFORE UPDATE OR DELETE ON runtime.production_render_qc_evidence_members" in sql
    assert "production render QC evidence members are immutable and non-deletable" in sql
    assert "sum(member.evidence_byte_length)" in sql
    assert "> 16777216" in sql


def test_report_json_is_exact_canonical_closed_and_hash_bound() -> None:
    sql = _sql()

    assert "qc_attempt.evidence_report_json::jsonb" in sql
    assert "jsonb_object_keys(report)" in sql
    assert "runtime.canonical_json_ascii(report)" in sql
    assert "production render QC evidence report must use canonical JSON serialization" in sql
    assert "sha256(convert_to(qc_attempt.evidence_report_json, 'UTF8'))" in sql
    assert "production render QC evidence report hash does not bind" in sql
    assert "production-render-qc-evidence-v1" in sql
    assert "production-av-qc-v1" in sql
    for binding in (
        "qc_attempt_id",
        "render_attempt_id",
        "job_id",
        "command_slot_id",
        "output_blob",
        "render_facts_sha256",
        "qc_policy_sha256",
        "required_check_set_version",
        "qc_runner_identity_sha256",
        "checks",
    ):
        assert binding in sql


def test_required_checks_are_complete_unique_and_in_canonical_order() -> None:
    sql = _sql()

    required_checks = (
        "exact_object_identity",
        "container_stream_topology",
        "packet_timeline_integrity",
        "decoded_frame_timeline",
        "full_video_decode",
        "full_audio_decode",
        "video_black_intervals",
        "video_freeze_intervals",
        "audio_silence_intervals",
        "audio_sample_health",
        "av_presentation_envelope",
        "edit_junction_continuity",
    )
    positions = [sql.index(f"'{check_id}'") for check_id in required_checks]
    assert positions == sorted(positions)
    assert "jsonb_array_length(report->'checks') > 64" in sql
    assert "check_ordinal <> check_index" in sql
    assert "check_id <> required_checks[check_index + 1]" in sql
    assert "SELECT count(*)" in sql
    assert ") <> cardinality(required_checks)" in sql


def test_check_and_measurement_domains_are_closed_and_canonical() -> None:
    sql = _sql()

    assert "collection_status NOT IN (" in sql
    assert "'completed', 'incomplete', 'not_run', 'not_applicable'" in sql
    assert "coverage NOT IN ('full_file', 'partial', 'none', 'not_applicable')" in sql
    assert "collection_status = 'completed'" in sql
    assert "coverage NOT IN ('full_file', 'not_applicable')" in sql
    assert "jsonb_array_length(check_document->'measurements') > 256" in sql
    assert "measurement names must be unique and strictly increasing" in sql
    assert "value_kind NOT IN (" in sql
    assert "'integer', 'decimal', 'rational', 'boolean', 'text', 'sha256'" in sql
    assert "runtime.production_render_qc_rational_is_canonical" in sql
    assert "measurement_unit NOT IN (" in sql
    assert "measurement text cannot carry locator or path semantics" in sql


def test_members_bind_report_blobs_and_same_job_claims_deferred() -> None:
    sql = _sql()

    assert "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW" in sql
    assert "member.evidence_object_id::text" in sql
    assert "member.evidence_content_hash" in sql
    assert "member.evidence_byte_length::text" in sql
    assert "member.evidence_media_type" in sql
    assert "evidence_blob.object_id = member.evidence_object_id" in sql
    assert "evidence_blob.content_hash = member.evidence_content_hash" in sql
    assert "evidence_blob.byte_length = member.evidence_byte_length" in sql
    assert "evidence_blob.media_type = member.evidence_media_type" in sql
    assert "evidence_claim.job_id = qc_attempt.job_id" in sql
    assert "evidence_claim.object_id = evidence_blob.object_id" in sql


def test_deferred_member_trigger_rejects_a_partial_scanning_commit() -> None:
    sql = _sql()
    member_trigger_body = sql.split(
        "CREATE OR REPLACE FUNCTION "
        "runtime.assert_production_render_qc_evidence_member_integrity()",
        1,
    )[1].split("END $$;", 1)[0]

    assert "FROM runtime.production_render_qc_attempts AS owner" in member_trigger_body
    assert "owner.qc_attempt_id = NEW.qc_attempt_id" in member_trigger_body
    assert "owner.state = 'evidence_ready'" in member_trigger_body
    assert (
        "production render QC evidence members require an evidence-ready owner"
        in member_trigger_body
    )
    assert member_trigger_body.index("owner.state = 'evidence_ready'") < (
        member_trigger_body.index(
            "PERFORM runtime.validate_production_render_qc_evidence"
        )
    )


def test_parent_and_output_authority_are_revalidated_and_terminalization_stays_blocked() -> None:
    sql = _sql()

    assert "parent_render.state = 'rendered'" in sql
    assert "parent_render.version = qc_attempt.rendered_version" in sql
    assert "parent_render.output_object_id = qc_attempt.output_object_id" in sql
    assert "parent_render.render_facts_sha256 = qc_attempt.render_facts_sha256" in sql
    assert "render_slot.state = 'running'" in sql
    assert "render_slot.command_name = 'RenderProductionRecipeCommand@1'" in sql
    assert "render_slot.execution_kind = 'deterministic'" in sql
    assert "output_claim.job_id = qc_attempt.job_id" in sql
    assert "qc_attempt.state IN ('reserved', 'scanning', 'evidence_ready')" in sql
    assert "production render with an active QC journal cannot become terminal" in sql


def test_migration_grants_no_visibility_release_or_publication_authority() -> None:
    sql = _sql().lower()

    for forbidden in (
        "insert into runtime.command_receipts",
        "insert into runtime.artifact_sets",
        "publish_decision",
        "local_visibility",
        "current.json",
        "publication_allow",
    ):
        assert forbidden not in sql
    assert "grants no receipt, artifactset, visibility, release or" in sql
    assert "publication authority" in sql


def test_measurement_unit_and_kind_registries_match_the_migration() -> None:
    sql = _sql()
    unit_section = sql.split("IF measurement_unit NOT IN (", 1)[1].split(
        ") THEN", 1
    )[0]
    kind_section = sql.split("IF value_kind NOT IN (", 1)[1].split(")", 1)[0]

    assert tuple(re.findall(r"'([a-z]+)'", unit_section)) == get_args(
        ProductionRenderQcMeasurementUnit
    )
    assert tuple(re.findall(r"'([a-z0-9]+)'", kind_section)) == get_args(
        ProductionRenderQcMeasurementKind
    )
    assert PRODUCTION_RENDER_QC_CHECK_SET_VERSION == "production-av-qc-v1"
    assert (
        PRODUCTION_RENDER_QC_EVIDENCE_SCHEMA_VERSION
        == "production-render-qc-evidence-v1"
    )


@pytest.mark.parametrize(
    ("value_kind", "value", "unit"),
    (
        ("integer", "-12", "count"),
        ("decimal", "-0.125", "percent"),
        ("rational", "-2/3", "ratio"),
        ("boolean", "false", "none"),
        ("text", "媒体", "none"),
        ("sha256", SHA256_A, "none"),
    ),
)
def test_measurement_accepts_only_canonical_closed_scalar_values(
    value_kind: ProductionRenderQcMeasurementKind,
    value: str,
    unit: ProductionRenderQcMeasurementUnit,
) -> None:
    measurement = ProductionRenderQcMeasurement(
        name="observed_value",
        value_kind=value_kind,
        value=value,
        unit=unit,
    )

    assert measurement.to_mapping() == {
        "name": "observed_value",
        "unit": unit,
        "value": value,
        "value_kind": value_kind,
    }


@pytest.mark.parametrize(
    "changes",
    (
        {"name": ""},
        {"name": "Uppercase"},
        {"name": "a" * 129},
        {"value_kind": "float"},
        {"unit": "milliseconds"},
        {"value": "1" * 513},
        {"value_kind": "integer", "value": "+1"},
        {"value_kind": "integer", "value": "01"},
        {"value_kind": "integer", "value": "-0"},
        {"value_kind": "decimal", "value": "1.0"},
        {"value_kind": "decimal", "value": "1e-3"},
        {"value_kind": "decimal", "value": ".5"},
        {"value_kind": "rational", "value": "2/4"},
        {"value_kind": "rational", "value": "1/0"},
        {"value_kind": "boolean", "value": "False"},
        {"value_kind": "sha256", "value": "sha256:" + "A" * 64},
        {"name": "storage_locator", "value_kind": "text", "value": "opaque"},
        {"value_kind": "text", "value": ""},
    ),
)
def test_measurement_rejects_open_or_noncanonical_values(
    changes: dict[str, object],
) -> None:
    with pytest.raises(StoreValidationError):
        _measurement(**changes)


def test_check_evidence_mapping_is_closed_and_preserves_order() -> None:
    measurements = (
        ProductionRenderQcMeasurement("a_count", "integer", "1", "count"),
        ProductionRenderQcMeasurement("z_hash", "sha256", SHA256_A, "none"),
    )
    check = replace(
        _check(0, measurements=measurements),
        diagnostic_code="collector_complete",
    )

    assert check.to_mapping() == {
        "argv_sha256": check.argv_sha256,
        "check_id": check.check_id,
        "check_ordinal": 0,
        "collection_status": "completed",
        "coverage": "full_file",
        "diagnostic_code": "collector_complete",
        "evidence_blob": {
            "byte_length": check.evidence_blob.byte_length,
            "content_hash": check.evidence_blob.content_hash,
            "media_type": "application/json",
            "object_id": str(check.evidence_blob.object_id),
        },
        "measurements": [item.to_mapping() for item in measurements],
        "parser_schema_version": "ffprobe-json-v1",
        "tool_identity_sha256": check.tool_identity_sha256,
    }


@pytest.mark.parametrize(
    ("collection_status", "coverage"),
    (
        ("completed", "partial"),
        ("incomplete", "full_file"),
        ("not_run", "partial"),
        ("not_applicable", "none"),
        ("unknown", "none"),
    ),
)
def test_check_evidence_rejects_invalid_status_coverage_pairs(
    collection_status: str,
    coverage: str,
) -> None:
    with pytest.raises(StoreValidationError):
        replace(
            _check(0),
            collection_status=collection_status,
            coverage=coverage,
        )


def test_check_evidence_rejects_nonexact_unordered_or_unbounded_members() -> None:
    check = _check(0)
    first = ProductionRenderQcMeasurement("a", "integer", "1", "count")
    second = ProductionRenderQcMeasurement("b", "integer", "2", "count")
    too_many = tuple(
        ProductionRenderQcMeasurement(
            f"m{index:03d}",
            "integer",
            str(index),
            "count",
        )
        for index in range(257)
    )
    invalid_changes = (
        {"check_ordinal": True},
        {"check_ordinal": 64},
        {"check_id": "Invalid"},
        {"parser_schema_version": "a" * 129},
        {"tool_identity_sha256": SHA256_A.upper()},
        {"argv_sha256": "not-a-hash"},
        {"measurements": [first]},
        {"measurements": (second, first)},
        {"measurements": (first, first)},
        {"measurements": too_many},
        {"measurements": (object(),)},
        {"evidence_blob": object()},
        {"evidence_blob": BlobRef(uuid4(), SHA256_A, 0, "application/json")},
        {
            "evidence_blob": BlobRef(
                uuid4(), SHA256_A, 2 * 1024 * 1024 + 1, "application/json"
            )
        },
        {"evidence_blob": BlobRef(uuid4(), SHA256_A, 1, "text/plain")},
        {"diagnostic_code": "Not-Safe"},
    )
    for changes in invalid_changes:
        with pytest.raises(StoreValidationError):
            replace(check, **changes)


def test_evidence_report_mapping_json_and_hash_are_exact_and_canonical() -> None:
    checks = tuple(
        _check(index) for index in range(len(PRODUCTION_RENDER_QC_REQUIRED_CHECKS))
    )
    unicode_measurement = ProductionRenderQcMeasurement(
        "observation",
        "text",
        "媒体",
        "none",
    )
    checks = (replace(checks[0], measurements=(unicode_measurement,)), *checks[1:])
    report = _report(checks=checks)
    mapping = report.to_mapping()
    expected_json = json.dumps(
        mapping,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )

    assert mapping == {
        "checks": [check.to_mapping() for check in checks],
        "command_slot_id": str(report.command_slot_id),
        "job_id": str(report.job_id),
        "output_blob": {
            "byte_length": 4096,
            "content_hash": SHA256_A,
            "media_type": "video/mp4",
            "object_id": str(report.output_blob.object_id),
        },
        "qc_attempt_id": str(report.qc_attempt_id),
        "qc_policy_sha256": SHA256_B,
        "qc_runner_identity_sha256": SHA256_A,
        "render_attempt_id": str(report.render_attempt_id),
        "render_facts_sha256": SHA256_A,
        "required_check_set_version": "production-av-qc-v1",
        "schema_version": "production-render-qc-evidence-v1",
    }
    assert report.canonical_json == expected_json
    assert "媒体" not in report.canonical_json
    assert "\\u5a92\\u4f53" in report.canonical_json
    assert report.canonical_hash == (
        "sha256:" + hashlib.sha256(expected_json.encode("utf-8")).hexdigest()
    )


def test_evidence_report_rejects_wrong_bindings_order_types_and_caps() -> None:
    report = _report()
    reordered = (report.checks[1], report.checks[0], *report.checks[2:])
    aggregate_over_cap = tuple(
        replace(
            check,
            evidence_blob=BlobRef(
                uuid4(),
                SHA256_A,
                2 * 1024 * 1024,
                "application/json",
            ),
        )
        for check in report.checks
    )
    invalid_changes = (
        {"qc_attempt_id": str(uuid4())},
        {"output_blob": object()},
        {"output_blob": BlobRef(uuid4(), SHA256_A, 0, "video/mp4")},
        {"output_blob": BlobRef(uuid4(), SHA256_A, 1, "video/webm")},
        {"render_facts_sha256": "bad"},
        {"required_check_set_version": "production-av-qc-v2"},
        {"schema_version": "production-render-qc-evidence-v2"},
        {"checks": list(report.checks)},
        {"checks": (object(),)},
        {"checks": report.checks[:-1]},
        {"checks": reordered},
        {"checks": aggregate_over_cap},
    )
    for changes in invalid_changes:
        with pytest.raises(StoreValidationError):
            replace(report, **changes)

    large_measurements = tuple(
        ProductionRenderQcMeasurement(
            f"m{index:03d}",
            "text",
            "x" * 512,
            "none",
        )
        for index in range(256)
    )
    report_over_cap = tuple(
        replace(check, measurements=large_measurements) for check in report.checks
    )
    with pytest.raises(StoreValidationError, match="canonical JSON cap"):
        replace(report, checks=report_over_cap)


def test_evidence_ready_attempt_requires_exact_report_hash_and_pairing() -> None:
    attempt = _evidence_ready_attempt()
    report = attempt.evidence_report
    assert report is not None

    assert attempt.state == "evidence_ready"
    assert attempt.evidence_report_sha256 == report.canonical_hash
    assert attempt.lease_expires_at is None

    binding_changes = (
        {"qc_attempt_id": uuid4()},
        {"render_attempt_id": uuid4()},
        {"job_id": uuid4()},
        {"command_slot_id": uuid4()},
        {"output_blob": BlobRef(uuid4(), SHA256_A, 4096, "video/mp4")},
        {"render_facts_sha256": SHA256_B},
        {"qc_policy_sha256": SHA256_A},
        {"required_check_set_version": "production-av-qc-v2"},
        {"qc_runner_identity_sha256": SHA256_B},
    )
    for changes in binding_changes:
        with pytest.raises(StoreValidationError, match="disagrees with its attempt"):
            replace(attempt, **changes)

    invalid_shapes = (
        {"version": 1},
        {"lease_expires_at": datetime.now(timezone.utc)},
        {"evidence_report": None},
        {"evidence_report": object()},
        {"evidence_report_sha256": None},
        {"evidence_report_sha256": SHA256_A},
        {"evidence_ready_at": None},
        {"evidence_ready_at": datetime.now()},
        {"state": "reserved", "version": 0},
        {
            "state": "scanning",
            "version": 1,
            "lease_expires_at": datetime.now(timezone.utc),
        },
    )
    for changes in invalid_shapes:
        with pytest.raises(StoreValidationError):
            replace(attempt, **changes)
