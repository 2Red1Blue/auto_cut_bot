"""Contract tests for independent private production-QC evaluation."""

from __future__ import annotations

import ast
import hashlib
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from autocut_kernel.rendering.production_qc_collectors import PRODUCTION_QC_COLLECTORS
from autocut_kernel.rendering.production_qc_evaluator import (
    PRODUCTION_RENDER_QC_EVALUATOR_IDENTITY_SHA256,
    PRODUCTION_RENDER_QC_POLICY_V1,
    PRODUCTION_RENDER_QC_RULE_IDS,
    PRODUCTION_RENDER_QC_TECHNICAL_RULE_IDS,
    ProductionRenderQcCollectorIdentity,
    ProductionRenderQcEvaluationError,
    ProductionRenderQcPolicy,
    evaluate_production_render_qc,
)
from autocut_kernel.store.models import (
    BlobRef,
    ProductionRenderQcCheckEvidence,
    ProductionRenderQcEvidenceReport,
    ProductionRenderQcMeasurement,
)


def _hash(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


_COLLECTOR_IDENTITY = ProductionRenderQcCollectorIdentity(
    _hash(b"runner"), _hash(b"ffmpeg"), _hash(b"ffprobe")
)


def _measurement(spec, value: str) -> ProductionRenderQcMeasurement:
    return ProductionRenderQcMeasurement(spec.name, spec.value_kind, value, spec.unit)


def _measurement_values(check_id: str) -> dict[str, str]:
    return {
        "exact_object_identity.file_byte_length": "1234",
        "exact_object_identity.file_sha256": _hash(b"media"),
        "exact_object_identity.regular_file": "true",
        "exact_object_identity.stable_file_identity": "true",
        "container_stream_topology.audio_stream_count": "1",
        "container_stream_topology.stream_count": "2",
        "container_stream_topology.video_stream_count": "1",
        "packet_timeline_integrity.packet_count": "200",
        "packet_timeline_integrity.stream_count": "2",
        "packet_timeline_integrity.timestamp_anomaly_count": "0",
        "decoded_frame_timeline.frame_count": "100",
        "decoded_frame_timeline.sample_count": "48000",
        "decoded_frame_timeline.timestamp_anomaly_count": "0",
        "full_video_decode.framehash_row_count": "100",
        "full_video_decode.video_stream_count": "1",
        "full_audio_decode.audio_stream_count": "1",
        "full_audio_decode.framehash_row_count": "48",
        "video_black_intervals.interval_count": "0",
        "video_black_intervals.right_censored_interval_count": "0",
        "video_freeze_intervals.interval_count": "0",
        "video_freeze_intervals.right_censored_interval_count": "0",
        "audio_silence_intervals.channel_count": "2",
        "audio_silence_intervals.interval_count": "0",
        "audio_silence_intervals.right_censored_interval_count": "0",
        "audio_sample_health.channel_count": "2",
        "audio_sample_health.nonfinite_value_count": "0",
        "audio_sample_health.snapshot_count": "1",
        "av_presentation_envelope.audio_stream_count": "1",
        "av_presentation_envelope.video_stream_count": "1",
        "edit_junction_continuity.junction_count": "1",
        "edit_junction_continuity.observation_count": "100",
    }


def _report() -> ProductionRenderQcEvidenceReport:
    values = _measurement_values("")
    checks: list[ProductionRenderQcCheckEvidence] = []
    for spec in PRODUCTION_QC_COLLECTORS:
        measurements = tuple(
            _measurement(item, values[f"{spec.check_id}.{item.name}"])
            for item in spec.measurements
        )
        checks.append(
            ProductionRenderQcCheckEvidence(
                check_ordinal=spec.ordinal,
                check_id=spec.check_id,
                collection_status="completed",
                coverage="full_file",
                parser_schema_version=spec.parser_schema_version,
                tool_identity_sha256=(
                    _COLLECTOR_IDENTITY.ffmpeg_tool_identity_sha256
                    if spec.argv_template[0] == "ffmpeg"
                    else _COLLECTOR_IDENTITY.ffprobe_tool_identity_sha256
                    if spec.argv_template[0] == "ffprobe"
                    else _COLLECTOR_IDENTITY.qc_runner_identity_sha256
                ),
                argv_sha256=spec.canonical_argv_sha256,
                measurements=measurements,
                evidence_blob=BlobRef(
                    uuid4(),
                    _hash(f"evidence:{spec.check_id}".encode()),
                    100,
                    "application/json",
                ),
            )
        )
    output = BlobRef(uuid4(), _hash(b"media"), 1234, "video/mp4")
    return ProductionRenderQcEvidenceReport(
        qc_attempt_id=uuid4(),
        render_attempt_id=uuid4(),
        job_id=uuid4(),
        command_slot_id=uuid4(),
        output_blob=output,
        render_facts_sha256=_hash(b"render-facts"),
        qc_policy_sha256=PRODUCTION_RENDER_QC_POLICY_V1.canonical_hash,
        required_check_set_version="production-av-qc-v1",
        qc_runner_identity_sha256=_COLLECTOR_IDENTITY.qc_runner_identity_sha256,
        checks=tuple(checks),
    )


def _evaluate(report: ProductionRenderQcEvidenceReport):
    return evaluate_production_render_qc(
        report,
        report_sha256=report.canonical_hash,
        policy=PRODUCTION_RENDER_QC_POLICY_V1,
        evaluator_identity_sha256=PRODUCTION_RENDER_QC_EVALUATOR_IDENTITY_SHA256,
        collector_identity=_COLLECTOR_IDENTITY,
    )


def test_policy_is_closed_canonical_and_bound_to_the_report() -> None:
    policy = ProductionRenderQcPolicy.from_mapping(
        PRODUCTION_RENDER_QC_POLICY_V1.to_mapping()
    )
    assert policy == PRODUCTION_RENDER_QC_POLICY_V1
    assert policy.canonical_hash == _hash(policy.canonical_json.encode())
    assert _report().qc_policy_sha256 == policy.canonical_hash

    changed = dict(policy.to_mapping())
    changed["policy_version"] = "production-av-qc-policy-v2"
    with pytest.raises(ProductionRenderQcEvaluationError, match="policy version"):
        ProductionRenderQcPolicy.from_mapping(changed)
    changed = dict(policy.to_mapping())
    changed["unknown"] = True
    with pytest.raises(ProductionRenderQcEvaluationError, match="closed object"):
        ProductionRenderQcPolicy.from_mapping(changed)


def test_all_known_technical_rules_pass_but_stage5_facts_deny_eligibility() -> None:
    evaluation = _evaluate(_report())

    assert tuple(item.rule_id for item in evaluation.rules) == PRODUCTION_RENDER_QC_RULE_IDS
    assert all(
        item.rule_result == "pass" and item.eligibility == "pass"
        for item in evaluation.rules[: len(PRODUCTION_RENDER_QC_TECHNICAL_RULE_IDS)]
    )
    missing_stage5 = evaluation.rules[-1]
    assert missing_stage5.rule_result == "indeterminate"
    assert missing_stage5.eligibility == "deny"
    assert missing_stage5.repairability == "repairable"
    assert evaluation.technical_eligibility == "pass"
    assert evaluation.eligibility == "deny"
    assert evaluation.repairability == "repairable"
    assert "publish_decision" not in evaluation.to_mapping()
    assert "receipt" not in evaluation.to_mapping()
    assert "artifact_set" not in evaluation.to_mapping()


def test_evaluation_order_and_hash_are_deterministic() -> None:
    report = _report()
    first = _evaluate(report)
    second = _evaluate(report)

    assert first == second
    assert first.canonical_json == second.canonical_json
    assert first.canonical_hash == _hash(first.canonical_json.encode())


def test_report_policy_and_evaluator_identity_mismatches_are_rejected() -> None:
    report = _report()
    with pytest.raises(ProductionRenderQcEvaluationError, match="report identity"):
        evaluate_production_render_qc(
            report,
            report_sha256=_hash(b"another-report"),
            policy=PRODUCTION_RENDER_QC_POLICY_V1,
            evaluator_identity_sha256=PRODUCTION_RENDER_QC_EVALUATOR_IDENTITY_SHA256,
            collector_identity=_COLLECTOR_IDENTITY,
        )
    with pytest.raises(ProductionRenderQcEvaluationError, match="policy identity"):
        evaluate_production_render_qc(
            replace(report, qc_policy_sha256=_hash(b"another-policy")),
            report_sha256=replace(
                report, qc_policy_sha256=_hash(b"another-policy")
            ).canonical_hash,
            policy=PRODUCTION_RENDER_QC_POLICY_V1,
            evaluator_identity_sha256=PRODUCTION_RENDER_QC_EVALUATOR_IDENTITY_SHA256,
            collector_identity=_COLLECTOR_IDENTITY,
        )
    with pytest.raises(ProductionRenderQcEvaluationError, match="evaluator identity"):
        evaluate_production_render_qc(
            report,
            report_sha256=report.canonical_hash,
            policy=PRODUCTION_RENDER_QC_POLICY_V1,
            evaluator_identity_sha256=_hash(b"unknown-evaluator"),
            collector_identity=_COLLECTOR_IDENTITY,
        )


def test_report_collector_and_per_tool_identity_mismatches_are_rejected() -> None:
    report = _report()
    changed_runner = replace(report, qc_runner_identity_sha256=_hash(b"other-runner"))
    with pytest.raises(ProductionRenderQcEvaluationError, match="runner identity mismatch"):
        _evaluate(changed_runner)

    changed_check = replace(
        report.checks[4], tool_identity_sha256=_hash(b"other-ffmpeg")
    )
    changed_tools = replace(report, checks=(*report.checks[:4], changed_check, *report.checks[5:]))
    with pytest.raises(ProductionRenderQcEvaluationError, match="tool identity mismatch"):
        _evaluate(changed_tools)


def test_incomplete_check_and_missing_required_metric_are_indeterminate_denials() -> None:
    report = _report()
    incomplete = replace(
        report.checks[2],
        collection_status="incomplete",
        coverage="partial",
        measurements=(),
        diagnostic_code="process_exit_nonzero",
    )
    report = replace(report, checks=(*report.checks[:2], incomplete, *report.checks[3:]))
    evaluation = _evaluate(report)
    assert evaluation.rules[0].rule_result == "indeterminate"
    assert evaluation.rules[4].rule_result == "indeterminate"
    assert evaluation.eligibility == "deny"

    report = _report()
    identity = replace(report.checks[0], measurements=report.checks[0].measurements[1:])
    report = replace(report, checks=(identity, *report.checks[1:]))
    evaluation = _evaluate(report)
    assert evaluation.rules[1].rule_result == "indeterminate"
    assert evaluation.rules[5].rule_result == "indeterminate"
    assert evaluation.eligibility == "deny"


def test_objective_violation_is_fail_and_repairable_remains_denied() -> None:
    report = _report()
    topology = report.checks[1]
    changed = tuple(
        replace(item, value="2") if item.name == "video_stream_count" else item
        for item in topology.measurements
    )
    report = replace(
        report,
        checks=(report.checks[0], replace(topology, measurements=changed), *report.checks[2:]),
    )

    evaluation = _evaluate(report)
    rule = evaluation.rules[2]
    assert rule.rule_result == "fail"
    assert rule.eligibility == "deny"
    assert rule.repairability == "repairable"
    assert evaluation.eligibility == "deny"


def test_conditionally_absent_audio_checks_are_neutral_not_applicable_results() -> None:
    report = _report()
    checks = list(report.checks)
    topology = checks[1]
    checks[1] = replace(
        topology,
        measurements=tuple(
            replace(item, value="0")
            if item.name == "audio_stream_count"
            else replace(item, value="1")
            if item.name == "stream_count"
            else item
            for item in topology.measurements
        ),
    )
    frames = checks[3]
    checks[3] = replace(
        frames,
        measurements=tuple(
            replace(item, value="0") if item.name == "sample_count" else item
            for item in frames.measurements
        ),
    )
    packets = checks[2]
    checks[2] = replace(
        packets,
        measurements=tuple(
            replace(item, value="1") if item.name == "stream_count" else item
            for item in packets.measurements
        ),
    )
    for ordinal in (5, 8, 9, 10):
        checks[ordinal] = replace(
            checks[ordinal],
            collection_status="not_applicable",
            coverage="not_applicable",
            measurements=(),
        )
    report = replace(report, checks=tuple(checks))

    evaluation = _evaluate(report)
    assert evaluation.technical_eligibility == "pass"
    assert evaluation.eligibility == "deny"


def test_nonfinite_audio_measurement_is_an_objective_failure() -> None:
    report = _report()
    health = report.checks[9]
    changed = tuple(
        replace(item, value="1") if item.name == "nonfinite_value_count" else item
        for item in health.measurements
    )
    report = replace(
        report,
        checks=(*report.checks[:9], replace(health, measurements=changed), *report.checks[10:]),
    )

    evaluation = _evaluate(report)
    assert evaluation.rules[5].rule_result == "fail"
    assert evaluation.rules[5].eligibility == "deny"


def test_unknown_report_schema_checkset_and_registry_identity_fail_closed() -> None:
    report = _report()
    object.__setattr__(report, "schema_version", "production-render-qc-evidence-v2")
    with pytest.raises(ProductionRenderQcEvaluationError, match="schema version"):
        _evaluate(report)

    report = _report()
    object.__setattr__(report, "required_check_set_version", "production-av-qc-v2")
    with pytest.raises(ProductionRenderQcEvaluationError, match="check-set version"):
        _evaluate(report)

    report = _report()
    check = replace(report.checks[0], parser_schema_version="production-qc-collector-v2")
    report = replace(report, checks=(check, *report.checks[1:]))
    with pytest.raises(ProductionRenderQcEvaluationError, match="collector identity"):
        _evaluate(report)


def test_evaluator_has_no_effectful_layer_imports() -> None:
    source_path = Path(
        "packages/autocut-kernel/src/autocut_kernel/rendering/production_qc_evaluator.py"
    )
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports.update(
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    )
    assert not any(
        token in imported
        for imported in imports
        for token in ("postgres", "process", "runner", "http", "release", "publication")
    )
