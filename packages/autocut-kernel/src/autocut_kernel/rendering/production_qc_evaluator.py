"""Pure, fail-closed evaluation of immutable production-QC evidence.

The result is a private candidate.  It is deliberately incapable of creating
publication authority, a Receipt, an ArtifactSet, or any local visibility.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal, cast

from ..store.models import (
    PRODUCTION_RENDER_QC_CHECK_SET_VERSION,
    PRODUCTION_RENDER_QC_EVIDENCE_SCHEMA_VERSION,
    PRODUCTION_RENDER_QC_REQUIRED_CHECKS,
    ProductionRenderQcCheckEvidence,
    ProductionRenderQcEvidenceReport,
)
from .production_qc_collectors import (
    COLLECTOR_CHECK_SCHEMA_VERSION,
    PRODUCTION_RENDER_QC_COLLECTOR_REGISTRY,
    CollectorSpec,
)

PRODUCTION_RENDER_QC_POLICY_SCHEMA_VERSION: Final = "production-render-qc-policy-v1"
PRODUCTION_RENDER_QC_POLICY_VERSION: Final = "production-av-qc-policy-v1"
PRODUCTION_RENDER_QC_EVALUATION_SCHEMA_VERSION: Final = (
    "production-render-qc-private-evaluation-v1"
)
PRODUCTION_RENDER_QC_EVALUATOR_VERSION: Final = "production-render-qc-private-evaluator-v1"

PRODUCTION_RENDER_QC_TECHNICAL_RULE_IDS: Final = (
    "PQC-EVIDENCE-001",
    "PQC-IDENTITY-001",
    "PQC-TOPOLOGY-001",
    "PQC-DECODE-001",
    "PQC-TIMELINE-001",
    "PQC-METRICS-001",
)
PRODUCTION_RENDER_QC_STAGE5_RULE_ID: Final = "PQC-STAGE5-001"
PRODUCTION_RENDER_QC_RULE_IDS: Final = (
    *PRODUCTION_RENDER_QC_TECHNICAL_RULE_IDS,
    PRODUCTION_RENDER_QC_STAGE5_RULE_ID,
)
PRODUCTION_RENDER_QC_MISSING_STAGE5_FACTS: Final = (
    "av_drift",
    "black_interval_duration",
    "edit_junction_quality",
    "freeze_interval_duration",
    "integrated_loudness",
    "loudness_range",
    "platform_rules",
    "source_lineage",
    "source_usage_policy",
    "structural_story_completeness",
    "true_peak",
)

ProductionRenderQcRuleResult = Literal["pass", "fail", "indeterminate", "not_applicable"]
ProductionRenderQcEligibility = Literal["pass", "deny"]
ProductionRenderQcRepairability = Literal["none", "repairable", "nonrepairable"]

_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_STABLE_CODE = re.compile(r"[A-Z][A-Z0-9_]*\Z")


class ProductionRenderQcEvaluationError(ValueError):
    """The policy, report, evaluator, or closed registry identity is invalid."""


@dataclass(frozen=True, slots=True)
class ProductionRenderQcCollectorIdentity:
    """Admission-approved collector identities, supplied independently of a report.

    The report's own hashes are evidence, not their own authorization.  A
    caller obtains this closed identity from the QC attempt/profile already
    reserved by Admission, then gives it to this side-effect-free evaluator.
    """

    qc_runner_identity_sha256: str
    ffmpeg_tool_identity_sha256: str
    ffprobe_tool_identity_sha256: str

    def __post_init__(self) -> None:
        _require_sha256(self.qc_runner_identity_sha256, "production QC runner identity")
        _require_sha256(self.ffmpeg_tool_identity_sha256, "production QC FFmpeg identity")
        _require_sha256(self.ffprobe_tool_identity_sha256, "production QC FFprobe identity")


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _canonical_hash(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _require_sha256(value: object, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:  # noqa: E721
        raise ProductionRenderQcEvaluationError(f"{label} must be a canonical sha256")
    return value


def _closed_mapping(
    value: object, fields: tuple[str, ...], label: str
) -> Mapping[str, object]:
    if type(value) is not dict:  # noqa: E721
        raise ProductionRenderQcEvaluationError(f"{label} must be a closed object")
    raw = cast(dict[object, object], value)
    if any(type(key) is not str for key in raw) or set(raw) != set(fields):  # noqa: E721
        raise ProductionRenderQcEvaluationError(f"{label} must be a closed object")
    return cast(Mapping[str, object], raw)


def _evaluator_identity() -> str:
    return _canonical_hash(
        {
            "evaluation_schema_version": PRODUCTION_RENDER_QC_EVALUATION_SCHEMA_VERSION,
            "evaluator_version": PRODUCTION_RENDER_QC_EVALUATOR_VERSION,
            "rule_ids": list(PRODUCTION_RENDER_QC_RULE_IDS),
        }
    )


PRODUCTION_RENDER_QC_EVALUATOR_IDENTITY_SHA256: Final = _evaluator_identity()


@dataclass(frozen=True, slots=True)
class ProductionRenderQcPolicy:
    """The only registered first-wave technical QC policy body."""

    schema_version: str = PRODUCTION_RENDER_QC_POLICY_SCHEMA_VERSION
    policy_version: str = PRODUCTION_RENDER_QC_POLICY_VERSION
    evidence_schema_version: str = PRODUCTION_RENDER_QC_EVIDENCE_SCHEMA_VERSION
    required_check_set_version: str = PRODUCTION_RENDER_QC_CHECK_SET_VERSION
    evaluator_version: str = PRODUCTION_RENDER_QC_EVALUATOR_VERSION
    evaluator_identity_sha256: str = PRODUCTION_RENDER_QC_EVALUATOR_IDENTITY_SHA256
    rule_ids: tuple[str, ...] = PRODUCTION_RENDER_QC_RULE_IDS
    required_video_stream_count: int = 1
    allowed_audio_stream_counts: tuple[int, ...] = (0, 1)
    maximum_timestamp_anomaly_count: int = 0
    missing_stage5_facts: tuple[str, ...] = PRODUCTION_RENDER_QC_MISSING_STAGE5_FACTS

    def __post_init__(self) -> None:
        if (
            any(
                type(item) is not str
                for item in (
                    self.schema_version,
                    self.policy_version,
                    self.evidence_schema_version,
                    self.required_check_set_version,
                    self.evaluator_version,
                    self.evaluator_identity_sha256,
                )
            )
            or type(self.rule_ids) is not tuple  # noqa: E721
            or any(type(item) is not str for item in self.rule_ids)
            or type(self.required_video_stream_count) is not int  # noqa: E721
            or type(self.allowed_audio_stream_counts) is not tuple  # noqa: E721
            or any(type(item) is not int for item in self.allowed_audio_stream_counts)
            or type(self.maximum_timestamp_anomaly_count) is not int  # noqa: E721
            or type(self.missing_stage5_facts) is not tuple  # noqa: E721
            or any(type(item) is not str for item in self.missing_stage5_facts)
        ):
            raise ProductionRenderQcEvaluationError(
                "production QC policy body is not canonical"
            )
        expected = (
            PRODUCTION_RENDER_QC_POLICY_SCHEMA_VERSION,
            PRODUCTION_RENDER_QC_POLICY_VERSION,
            PRODUCTION_RENDER_QC_EVIDENCE_SCHEMA_VERSION,
            PRODUCTION_RENDER_QC_CHECK_SET_VERSION,
            PRODUCTION_RENDER_QC_EVALUATOR_VERSION,
            PRODUCTION_RENDER_QC_EVALUATOR_IDENTITY_SHA256,
            PRODUCTION_RENDER_QC_RULE_IDS,
            1,
            (0, 1),
            0,
            PRODUCTION_RENDER_QC_MISSING_STAGE5_FACTS,
        )
        actual = (
            self.schema_version,
            self.policy_version,
            self.evidence_schema_version,
            self.required_check_set_version,
            self.evaluator_version,
            self.evaluator_identity_sha256,
            self.rule_ids,
            self.required_video_stream_count,
            self.allowed_audio_stream_counts,
            self.maximum_timestamp_anomaly_count,
            self.missing_stage5_facts,
        )
        if actual != expected:
            if self.policy_version != PRODUCTION_RENDER_QC_POLICY_VERSION:
                raise ProductionRenderQcEvaluationError("production QC policy version is unknown")
            raise ProductionRenderQcEvaluationError(
                "production QC policy body is not the registered closed policy"
            )

    def to_mapping(self) -> dict[str, object]:
        return {
            "allowed_audio_stream_counts": list(self.allowed_audio_stream_counts),
            "evaluator_identity_sha256": self.evaluator_identity_sha256,
            "evaluator_version": self.evaluator_version,
            "evidence_schema_version": self.evidence_schema_version,
            "maximum_timestamp_anomaly_count": self.maximum_timestamp_anomaly_count,
            "missing_stage5_facts": list(self.missing_stage5_facts),
            "policy_version": self.policy_version,
            "required_check_set_version": self.required_check_set_version,
            "required_video_stream_count": self.required_video_stream_count,
            "rule_ids": list(self.rule_ids),
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_mapping(cls, value: object) -> ProductionRenderQcPolicy:
        fields = tuple(cls().to_mapping())
        raw = _closed_mapping(value, fields, "production QC policy")
        for name in ("allowed_audio_stream_counts", "missing_stage5_facts", "rule_ids"):
            if type(raw[name]) is not list:  # noqa: E721
                raise ProductionRenderQcEvaluationError(
                    f"production QC policy {name} must be an array"
                )
        return cls(
            schema_version=cast(str, raw["schema_version"]),
            policy_version=cast(str, raw["policy_version"]),
            evidence_schema_version=cast(str, raw["evidence_schema_version"]),
            required_check_set_version=cast(str, raw["required_check_set_version"]),
            evaluator_version=cast(str, raw["evaluator_version"]),
            evaluator_identity_sha256=cast(str, raw["evaluator_identity_sha256"]),
            rule_ids=tuple(cast(list[str], raw["rule_ids"])),
            required_video_stream_count=cast(int, raw["required_video_stream_count"]),
            allowed_audio_stream_counts=tuple(
                cast(list[int], raw["allowed_audio_stream_counts"])
            ),
            maximum_timestamp_anomaly_count=cast(
                int, raw["maximum_timestamp_anomaly_count"]
            ),
            missing_stage5_facts=tuple(cast(list[str], raw["missing_stage5_facts"])),
        )

    @property
    def canonical_json(self) -> str:
        return _canonical_json(self.to_mapping())

    @property
    def canonical_hash(self) -> str:
        return "sha256:" + hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest()


PRODUCTION_RENDER_QC_POLICY_V1: Final = ProductionRenderQcPolicy()
PRODUCTION_RENDER_QC_POLICY_SHA256: Final = PRODUCTION_RENDER_QC_POLICY_V1.canonical_hash


@dataclass(frozen=True, slots=True)
class ProductionRenderQcRuleEvaluation:
    """One derived rule result with independent eligibility and repairability."""

    ordinal: int
    rule_id: str
    rule_result: ProductionRenderQcRuleResult
    eligibility: ProductionRenderQcEligibility
    repairability: ProductionRenderQcRepairability
    evidence_check_ids: tuple[str, ...]
    diagnostic_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            type(self.ordinal) is not int  # noqa: E721
            or not 0 <= self.ordinal < len(PRODUCTION_RENDER_QC_RULE_IDS)
            or self.rule_id != PRODUCTION_RENDER_QC_RULE_IDS[self.ordinal]
        ):
            raise ProductionRenderQcEvaluationError("production QC rule order is not closed")
        if self.rule_result not in ("pass", "fail", "indeterminate", "not_applicable"):
            raise ProductionRenderQcEvaluationError("production QC rule result is unsupported")
        if self.eligibility not in ("pass", "deny"):
            raise ProductionRenderQcEvaluationError("production QC eligibility is unsupported")
        if self.repairability not in ("none", "repairable", "nonrepairable"):
            raise ProductionRenderQcEvaluationError("production QC repairability is unsupported")
        expected_eligibility = (
            "pass" if self.rule_result in ("pass", "not_applicable") else "deny"
        )
        if self.eligibility != expected_eligibility:
            raise ProductionRenderQcEvaluationError(
                "production QC rule eligibility contradicts its fail-closed result"
            )
        if (self.rule_result in ("pass", "not_applicable")) != (
            self.repairability == "none"
        ):
            raise ProductionRenderQcEvaluationError(
                "production QC rule repairability contradicts its result"
            )
        if (
            type(self.evidence_check_ids) is not tuple  # noqa: E721
            or any(item not in PRODUCTION_RENDER_QC_REQUIRED_CHECKS for item in self.evidence_check_ids)
            or len(set(self.evidence_check_ids)) != len(self.evidence_check_ids)
            or self.evidence_check_ids
            != tuple(
                item
                for item in PRODUCTION_RENDER_QC_REQUIRED_CHECKS
                if item in self.evidence_check_ids
            )
        ):
            raise ProductionRenderQcEvaluationError("production QC rule evidence is not closed")
        if (
            type(self.diagnostic_codes) is not tuple  # noqa: E721
            or any(_STABLE_CODE.fullmatch(item) is None for item in self.diagnostic_codes)
            or tuple(sorted(set(self.diagnostic_codes))) != self.diagnostic_codes
            or (self.rule_result in ("pass", "not_applicable")) == bool(self.diagnostic_codes)
        ):
            raise ProductionRenderQcEvaluationError(
                "production QC rule diagnostic codes contradict its result"
            )

    def to_mapping(self) -> dict[str, object]:
        return {
            "diagnostic_codes": list(self.diagnostic_codes),
            "eligibility": self.eligibility,
            "evidence_check_ids": list(self.evidence_check_ids),
            "ordinal": self.ordinal,
            "repairability": self.repairability,
            "rule_id": self.rule_id,
            "rule_result": self.rule_result,
        }


@dataclass(frozen=True, slots=True)
class ProductionRenderQcEvaluation:
    """Canonical private evaluation candidate without publication authority."""

    report_sha256: str
    qc_policy_sha256: str
    evaluator_identity_sha256: str
    rules: tuple[ProductionRenderQcRuleEvaluation, ...]
    technical_eligibility: ProductionRenderQcEligibility
    eligibility: ProductionRenderQcEligibility
    repairability: ProductionRenderQcRepairability
    schema_version: str = PRODUCTION_RENDER_QC_EVALUATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_sha256(self.report_sha256, "production QC evaluation report identity")
        _require_sha256(self.qc_policy_sha256, "production QC evaluation policy identity")
        _require_sha256(
            self.evaluator_identity_sha256, "production QC evaluation evaluator identity"
        )
        if self.schema_version != PRODUCTION_RENDER_QC_EVALUATION_SCHEMA_VERSION:
            raise ProductionRenderQcEvaluationError("production QC evaluation schema is unknown")
        if (
            type(self.rules) is not tuple  # noqa: E721
            or tuple(item.rule_id for item in self.rules) != PRODUCTION_RENDER_QC_RULE_IDS
        ):
            raise ProductionRenderQcEvaluationError(
                "production QC evaluation requires the exact ordered rule set"
            )
        stage5 = self.rules[-1]
        if (
            stage5.rule_result != "indeterminate"
            or stage5.eligibility != "deny"
            or stage5.repairability != "repairable"
            or stage5.evidence_check_ids
            or stage5.diagnostic_codes != ("PQC_STAGE5_FACTS_UNAVAILABLE",)
        ):
            raise ProductionRenderQcEvaluationError(
                "production QC v1 evaluation cannot claim Stage 5 fact closure"
            )
        technical = self.rules[: len(PRODUCTION_RENDER_QC_TECHNICAL_RULE_IDS)]
        expected_technical = (
            "pass" if all(item.eligibility == "pass" for item in technical) else "deny"
        )
        expected_eligibility = (
            "pass" if all(item.eligibility == "pass" for item in self.rules) else "deny"
        )
        denied_repairs = {
            item.repairability for item in self.rules if item.eligibility == "deny"
        }
        expected_repairability: ProductionRenderQcRepairability
        if "nonrepairable" in denied_repairs:
            expected_repairability = "nonrepairable"
        elif "repairable" in denied_repairs:
            expected_repairability = "repairable"
        else:
            expected_repairability = "none"
        if (
            self.technical_eligibility != expected_technical
            or self.eligibility != expected_eligibility
            or self.repairability != expected_repairability
        ):
            raise ProductionRenderQcEvaluationError(
                "production QC evaluation aggregates contradict ordered rules"
            )

    def to_mapping(self) -> dict[str, object]:
        return {
            "eligibility": self.eligibility,
            "evaluator_identity_sha256": self.evaluator_identity_sha256,
            "qc_policy_sha256": self.qc_policy_sha256,
            "repairability": self.repairability,
            "report_sha256": self.report_sha256,
            "rules": [item.to_mapping() for item in self.rules],
            "schema_version": self.schema_version,
            "technical_eligibility": self.technical_eligibility,
        }

    @property
    def canonical_json(self) -> str:
        return _canonical_json(self.to_mapping())

    @property
    def canonical_hash(self) -> str:
        return "sha256:" + hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest()


def _result(
    ordinal: int,
    result: ProductionRenderQcRuleResult,
    evidence_check_ids: tuple[str, ...],
    *codes: str,
) -> ProductionRenderQcRuleEvaluation:
    eligibility: ProductionRenderQcEligibility = (
        "pass" if result in ("pass", "not_applicable") else "deny"
    )
    repairability: ProductionRenderQcRepairability = (
        "none" if eligibility == "pass" else "repairable"
    )
    return ProductionRenderQcRuleEvaluation(
        ordinal,
        PRODUCTION_RENDER_QC_RULE_IDS[ordinal],
        result,
        eligibility,
        repairability,
        evidence_check_ids,
        tuple(sorted(set(codes))),
    )


def _check_by_id(
    report: ProductionRenderQcEvidenceReport,
) -> dict[str, ProductionRenderQcCheckEvidence]:
    return {item.check_id: item for item in report.checks}


def _measurement_values(
    check: ProductionRenderQcCheckEvidence, spec: CollectorSpec
) -> dict[str, str] | None:
    expected = {item.name: item for item in spec.measurements}
    actual = {item.name: item for item in check.measurements}
    if not set(actual) <= set(expected):
        raise ProductionRenderQcEvaluationError(
            f"production QC check {check.check_id} measurement schema is unknown"
        )
    for name, item in actual.items():
        expected_item = expected[name]
        if (item.value_kind, item.unit) != (expected_item.value_kind, expected_item.unit):
            raise ProductionRenderQcEvaluationError(
                f"production QC check {check.check_id} measurement schema is unknown"
            )
    if check.collection_status == "not_applicable":
        if actual:
            raise ProductionRenderQcEvaluationError(
                f"production QC check {check.check_id} not-applicable schema is invalid"
            )
        return None
    if check.collection_status != "completed" or check.coverage != "full_file":
        return None
    if set(actual) != set(expected):
        return None
    return {name: item.value for name, item in actual.items()}


def _integer(values: dict[str, str] | None, name: str) -> int | None:
    if values is None or name not in values:
        return None
    return int(values[name])


def _validate_bindings(
    report: ProductionRenderQcEvidenceReport,
    report_sha256: str,
    policy: ProductionRenderQcPolicy,
    evaluator_identity_sha256: str,
    collector_identity: ProductionRenderQcCollectorIdentity,
) -> None:
    if type(report) is not ProductionRenderQcEvidenceReport:  # noqa: E721
        raise ProductionRenderQcEvaluationError("production QC report must be exact")
    if type(policy) is not ProductionRenderQcPolicy:  # noqa: E721
        raise ProductionRenderQcEvaluationError("production QC policy must be exact")
    if type(collector_identity) is not ProductionRenderQcCollectorIdentity:  # noqa: E721
        raise ProductionRenderQcEvaluationError("production QC collector identity must be exact")
    policy.__post_init__()
    if report.schema_version != PRODUCTION_RENDER_QC_EVIDENCE_SCHEMA_VERSION:
        raise ProductionRenderQcEvaluationError("production QC report schema version is unknown")
    if report.required_check_set_version != PRODUCTION_RENDER_QC_CHECK_SET_VERSION:
        raise ProductionRenderQcEvaluationError(
            "production QC report check-set version is unknown"
        )
    if _require_sha256(report_sha256, "production QC report identity") != report.canonical_hash:
        raise ProductionRenderQcEvaluationError("production QC report identity mismatch")
    if report.qc_policy_sha256 != policy.canonical_hash:
        raise ProductionRenderQcEvaluationError("production QC policy identity mismatch")
    if (
        _require_sha256(evaluator_identity_sha256, "production QC evaluator identity")
        != PRODUCTION_RENDER_QC_EVALUATOR_IDENTITY_SHA256
        or evaluator_identity_sha256 != policy.evaluator_identity_sha256
    ):
        raise ProductionRenderQcEvaluationError("production QC evaluator identity mismatch")
    if report.qc_runner_identity_sha256 != collector_identity.qc_runner_identity_sha256:
        raise ProductionRenderQcEvaluationError("production QC runner identity mismatch")
    registry = PRODUCTION_RENDER_QC_COLLECTOR_REGISTRY
    if (
        tuple(item.ordinal for item in registry) != tuple(range(len(registry)))
        or tuple(item.check_id for item in registry) != PRODUCTION_RENDER_QC_REQUIRED_CHECKS
        or any(item.check_schema_version != COLLECTOR_CHECK_SCHEMA_VERSION for item in registry)
    ):
        raise ProductionRenderQcEvaluationError("production QC collector registry is unknown")
    identities = tuple((item.check_ordinal, item.check_id) for item in report.checks)
    if identities != tuple(enumerate(PRODUCTION_RENDER_QC_REQUIRED_CHECKS)):
        raise ProductionRenderQcEvaluationError("production QC report check identity mismatch")
    for check, spec in zip(report.checks, registry, strict=True):
        if (
            check.parser_schema_version != spec.parser_schema_version
            or check.argv_sha256 != spec.canonical_argv_sha256
        ):
            raise ProductionRenderQcEvaluationError(
                f"production QC check {check.check_id} collector identity mismatch"
            )
        expected_tool_identity = (
            collector_identity.ffmpeg_tool_identity_sha256
            if spec.argv_template[0] == "ffmpeg"
            else collector_identity.ffprobe_tool_identity_sha256
            if spec.argv_template[0] == "ffprobe"
            else collector_identity.qc_runner_identity_sha256
        )
        if check.tool_identity_sha256 != expected_tool_identity:
            raise ProductionRenderQcEvaluationError(
                f"production QC check {check.check_id} tool identity mismatch"
            )
        _measurement_values(check, spec)


def _evidence_completeness_rule(
    report: ProductionRenderQcEvidenceReport,
    values: Mapping[str, dict[str, str] | None],
) -> ProductionRenderQcRuleEvaluation:
    checks = _check_by_id(report)
    topology = values["container_stream_topology"]
    audio_count = _integer(topology, "audio_stream_count")
    video_count = _integer(topology, "video_stream_count")
    unavailable = False
    for check_id in PRODUCTION_RENDER_QC_REQUIRED_CHECKS:
        check = checks[check_id]
        if check.collection_status in ("incomplete", "not_run"):
            unavailable = True
        elif check.collection_status == "not_applicable":
            if check_id in ("full_audio_decode", "audio_silence_intervals", "audio_sample_health"):
                unavailable |= audio_count is None or audio_count != 0
            elif check_id in ("full_video_decode", "video_black_intervals", "video_freeze_intervals"):
                unavailable |= video_count is None or video_count != 0
            elif check_id == "av_presentation_envelope":
                unavailable |= (
                    audio_count is None
                    or video_count is None
                    or (audio_count > 0 and video_count > 0)
                )
            elif check_id != "edit_junction_continuity":
                unavailable = True
    if unavailable:
        return _result(
            0,
            "indeterminate",
            PRODUCTION_RENDER_QC_REQUIRED_CHECKS,
            "PQC_EVIDENCE_INCOMPLETE",
        )
    return _result(0, "pass", PRODUCTION_RENDER_QC_REQUIRED_CHECKS)


def _identity_rule(
    report: ProductionRenderQcEvidenceReport, values: dict[str, str] | None
) -> ProductionRenderQcRuleEvaluation:
    evidence = ("exact_object_identity",)
    if values is None:
        return _result(1, "indeterminate", evidence, "PQC_IDENTITY_METRICS_UNAVAILABLE")
    stable = values["stable_file_identity"] == "true"
    regular = values["regular_file"] == "true"
    exact = (
        values["file_sha256"] == report.output_blob.content_hash
        and int(values["file_byte_length"]) == report.output_blob.byte_length
    )
    if not (stable and regular and exact):
        return _result(1, "fail", evidence, "PQC_OUTPUT_IDENTITY_VIOLATION")
    return _result(1, "pass", evidence)


def _topology_rule(
    policy: ProductionRenderQcPolicy, values: dict[str, str] | None
) -> ProductionRenderQcRuleEvaluation:
    evidence = ("container_stream_topology",)
    if values is None:
        return _result(2, "indeterminate", evidence, "PQC_TOPOLOGY_UNAVAILABLE")
    audio = int(values["audio_stream_count"])
    video = int(values["video_stream_count"])
    total = int(values["stream_count"])
    supported = (
        video == policy.required_video_stream_count
        and audio in policy.allowed_audio_stream_counts
        and total == video + audio
    )
    if not supported:
        return _result(2, "fail", evidence, "PQC_STREAM_TOPOLOGY_UNSUPPORTED")
    return _result(2, "pass", evidence)


def _decode_rule(
    values: Mapping[str, dict[str, str] | None]
) -> ProductionRenderQcRuleEvaluation:
    evidence = ("container_stream_topology", "full_video_decode", "full_audio_decode")
    topology = values["container_stream_topology"]
    if topology is None:
        return _result(3, "indeterminate", evidence, "PQC_DECODE_APPLICABILITY_UNKNOWN")
    audio = int(topology["audio_stream_count"])
    video = int(topology["video_stream_count"])
    for media, count in (("video", video), ("audio", audio)):
        observed = values[f"full_{media}_decode"]
        if count == 0:
            if observed is not None:
                return _result(3, "fail", evidence, "PQC_DECODE_TOPOLOGY_MISMATCH")
            continue
        if observed is None:
            return _result(3, "indeterminate", evidence, "PQC_FULL_DECODE_UNAVAILABLE")
        if (
            int(observed[f"{media}_stream_count"]) != count
            or int(observed["framehash_row_count"]) <= 0
        ):
            return _result(3, "fail", evidence, "PQC_FULL_DECODE_VIOLATION")
    return _result(3, "pass", evidence)


def _timeline_rule(
    policy: ProductionRenderQcPolicy,
    values: Mapping[str, dict[str, str] | None],
) -> ProductionRenderQcRuleEvaluation:
    evidence = (
        "container_stream_topology",
        "packet_timeline_integrity",
        "decoded_frame_timeline",
    )
    topology = values["container_stream_topology"]
    packets = values["packet_timeline_integrity"]
    frames = values["decoded_frame_timeline"]
    if topology is None or packets is None or frames is None:
        return _result(4, "indeterminate", evidence, "PQC_TIMELINE_UNAVAILABLE")
    audio = int(topology["audio_stream_count"])
    video = int(topology["video_stream_count"])
    total = int(topology["stream_count"])
    complete = (
        int(packets["stream_count"]) == total
        and (total == 0 or int(packets["packet_count"]) > 0)
        and (video == 0 or int(frames["frame_count"]) > 0)
        and (audio == 0 or int(frames["sample_count"]) > 0)
    )
    anomaly = (
        int(packets["timestamp_anomaly_count"])
        > policy.maximum_timestamp_anomaly_count
        or int(frames["timestamp_anomaly_count"])
        > policy.maximum_timestamp_anomaly_count
    )
    if not complete or anomaly:
        return _result(4, "fail", evidence, "PQC_TIMELINE_OBJECTIVE_VIOLATION")
    return _result(4, "pass", evidence)


def _metric_availability_rule(
    report: ProductionRenderQcEvidenceReport,
    values: Mapping[str, dict[str, str] | None],
) -> ProductionRenderQcRuleEvaluation:
    checks = _check_by_id(report)
    unavailable = tuple(
        check_id
        for check_id in PRODUCTION_RENDER_QC_REQUIRED_CHECKS
        if values[check_id] is None and checks[check_id].collection_status != "not_applicable"
    )
    if unavailable:
        return _result(5, "indeterminate", unavailable, "PQC_REQUIRED_METRIC_UNAVAILABLE")

    impossible: list[str] = []
    for check_id in (
        "video_black_intervals",
        "video_freeze_intervals",
        "audio_silence_intervals",
    ):
        item = values[check_id]
        if item is not None:
            total = int(item["interval_count"])
            censored = int(item["right_censored_interval_count"])
            if total < 0 or censored < 0 or censored > total:
                impossible.append(check_id)
    health = values["audio_sample_health"]
    if health is not None and (
        int(health["channel_count"]) <= 0
        or int(health["snapshot_count"]) <= 0
        or int(health["nonfinite_value_count"]) != 0
    ):
        impossible.append("audio_sample_health")
    topology = values["container_stream_topology"]
    envelope = values["av_presentation_envelope"]
    if topology is not None and envelope is not None and (
        envelope["audio_stream_count"] != topology["audio_stream_count"]
        or envelope["video_stream_count"] != topology["video_stream_count"]
    ):
        impossible.append("av_presentation_envelope")
    junction = values["edit_junction_continuity"]
    if junction is not None and (
        int(junction["junction_count"]) < 0
        or int(junction["observation_count"]) <= 0
    ):
        impossible.append("edit_junction_continuity")
    if impossible:
        evidence = tuple(
            check_id for check_id in PRODUCTION_RENDER_QC_REQUIRED_CHECKS if check_id in impossible
        )
        return _result(5, "fail", evidence, "PQC_MEDIA_METRIC_OBJECTIVE_VIOLATION")
    return _result(5, "pass", PRODUCTION_RENDER_QC_REQUIRED_CHECKS)


def evaluate_production_render_qc(
    report: ProductionRenderQcEvidenceReport,
    *,
    report_sha256: str,
    policy: ProductionRenderQcPolicy,
    evaluator_identity_sha256: str,
    collector_identity: ProductionRenderQcCollectorIdentity,
) -> ProductionRenderQcEvaluation:
    """Evaluate one exact report without IO or publication-level side effects."""

    _validate_bindings(
        report,
        report_sha256,
        policy,
        evaluator_identity_sha256,
        collector_identity,
    )
    values = {
        check.check_id: _measurement_values(check, spec)
        for check, spec in zip(
            report.checks, PRODUCTION_RENDER_QC_COLLECTOR_REGISTRY, strict=True
        )
    }
    rules = (
        _evidence_completeness_rule(report, values),
        _identity_rule(report, values["exact_object_identity"]),
        _topology_rule(policy, values["container_stream_topology"]),
        _decode_rule(values),
        _timeline_rule(policy, values),
        _metric_availability_rule(report, values),
        _result(
            6,
            "indeterminate",
            (),
            "PQC_STAGE5_FACTS_UNAVAILABLE",
        ),
    )
    technical_eligibility: ProductionRenderQcEligibility = (
        "pass" if all(item.eligibility == "pass" for item in rules[:-1]) else "deny"
    )
    eligibility: ProductionRenderQcEligibility = (
        "pass" if all(item.eligibility == "pass" for item in rules) else "deny"
    )
    denied_repairs = {item.repairability for item in rules if item.eligibility == "deny"}
    repairability: ProductionRenderQcRepairability
    if "nonrepairable" in denied_repairs:
        repairability = "nonrepairable"
    elif "repairable" in denied_repairs:
        repairability = "repairable"
    else:
        repairability = "none"
    return ProductionRenderQcEvaluation(
        report_sha256,
        policy.canonical_hash,
        evaluator_identity_sha256,
        rules,
        technical_eligibility,
        eligibility,
        repairability,
    )


__all__ = (
    "PRODUCTION_RENDER_QC_EVALUATION_SCHEMA_VERSION",
    "PRODUCTION_RENDER_QC_EVALUATOR_IDENTITY_SHA256",
    "PRODUCTION_RENDER_QC_EVALUATOR_VERSION",
    "PRODUCTION_RENDER_QC_MISSING_STAGE5_FACTS",
    "PRODUCTION_RENDER_QC_POLICY_SCHEMA_VERSION",
    "PRODUCTION_RENDER_QC_POLICY_SHA256",
    "PRODUCTION_RENDER_QC_POLICY_V1",
    "PRODUCTION_RENDER_QC_POLICY_VERSION",
    "PRODUCTION_RENDER_QC_RULE_IDS",
    "PRODUCTION_RENDER_QC_STAGE5_RULE_ID",
    "PRODUCTION_RENDER_QC_TECHNICAL_RULE_IDS",
    "ProductionRenderQcEligibility",
    "ProductionRenderQcCollectorIdentity",
    "ProductionRenderQcEvaluation",
    "ProductionRenderQcEvaluationError",
    "ProductionRenderQcPolicy",
    "ProductionRenderQcRepairability",
    "ProductionRenderQcRuleEvaluation",
    "ProductionRenderQcRuleResult",
    "evaluate_production_render_qc",
)
