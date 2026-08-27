"""Closed, canonical persistence contract for accepted calibration records.

This module is deliberately pure: it models the four persisted authority
members and verifies their cross-closure, but performs no Store, provider,
configuration, or network work.

The public media facade stops at unaccepted candidates. The validator command
must independently replay immutable raw evidence before supplying its result
to the internal assembly seam. Python types and private/exported names do not
prove authority; the validator command and protected Store writer do.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import cast

from ..contracts.compiler.canonical import (
    canonical_json_bytes,
    canonical_json_hash,
    load_canonical_json_bytes,
)
from ..contracts.compiler.errors import CanonicalizationError
from .calibration import CalibrationRecordError
from .runtime_measurement_identity import RuntimeMeasurementIdentity
from .types import TickRange, TimeBase, sha256_prefixed

CALIBRATION_RECORD_SCHEMA = "calibration-record-v1"
CALIBRATION_RECORD_MEMBER_SCHEMA = "calibration-record-member-v1"
CALIBRATION_VALIDATION_RECEIPT_SCHEMA = "calibration-record-validation-receipt-v1"
CALIBRATION_VALIDATION_INPUT_SCHEMA = "calibration-record-validation-input-v1"
CALIBRATION_VALIDATION_RESULT_SCHEMA = "calibration-record-validation-result-v1"
CALIBRATION_RECORD_ARTIFACT_TYPE = "calibration_record"
CALIBRATION_RECORD_MEMBER_ARTIFACT_TYPE = "calibration_record_member"
CALIBRATION_VALIDATION_RECEIPT_ARTIFACT_TYPE = "calibration_validation_receipt"
CALIBRATION_RECORD_NAMESPACE = "autocut_authority"
CALIBRATION_RECORD_SCOPE_KIND = "calibration"
CALIBRATION_RECORD_REVISION = 1
CALIBRATION_RECORD_VERSION = 1
RUNTIME_CALIBRATION_CAPABILITY_SCHEMA = "runtime-calibration-capability-v2"
CALIBRATION_VALIDATOR_COMMAND = "ValidateCalibrationRecord@2.1.3"
CALIBRATION_VALIDATOR_PRINCIPAL = "autocut-calibration-validator"
CALIBRATION_VALIDATION_CHECKS = (
    "strict_raw_envelope",
    "producer_identity",
    "source_clock_and_time_base",
    "anchor_pairing",
    "integer_bound_recomputation",
    "positive_bounds",
    "projection_equality",
)
CALIBRATION_BOUND_ALGORITHM_SHA256 = canonical_json_hash({
    "accepted_bound": "absolute_maximum_tick",
    "absolute_tick": "max(early_tick,late_tick)",
    "alignment": "complete-ordered-one-to-one",
    "early_tick": "max(0,expected_in-observed_in,expected_out-observed_out)",
    "integer_only": True,
    "late_tick": "max(0,observed_in-expected_in,observed_out-expected_out)",
    "producer_aggregation": "maximum-over-all-required-matches",
    "schema_version": "calibration-bound-algorithm-v1",
})

_ZERO_SHA256 = "sha256:" + "0" * 64
_PROFILE_VERSION = re.compile(r"^[1-9][0-9]*$")


class CalibrationRecordRole(str, Enum):
    ASR = "asr"
    VAD = "vad"


def _fail(detail: str) -> CalibrationRecordError:
    return CalibrationRecordError(f"invalid calibration record: {detail}")


def _text(value: object, field_name: str) -> str:
    if type(value) is not str or not value or value != value.strip():  # noqa: E721
        raise _fail(f"{field_name} must be canonical non-empty text")
    return value


def _integer(value: object, field_name: str, *, minimum: int = 0) -> int:
    if type(value) is not int:  # noqa: E721 - bool is not an integer in this contract.
        raise _fail(f"{field_name} must be an integer")
    if value < minimum:
        raise _fail(f"{field_name} must be at least {minimum}")
    return value


def _sha(value: object, field_name: str) -> str:
    try:
        digest = sha256_prefixed(value, field_name)
    except ValueError as error:
        raise _fail(str(error)) from error
    if digest == _ZERO_SHA256:
        raise _fail(f"{field_name} must be non-zero")
    return digest


def _closed(value: object, fields: frozenset[str], field_name: str) -> dict[str, object]:
    if type(value) is not dict or frozenset(cast(dict[str, object], value)) != fields:  # noqa: E721
        raise _fail(f"{field_name} does not match its closed schema")
    return cast(dict[str, object], value)


def _array(value: object, field_name: str, *, non_empty: bool = False) -> list[object]:
    if type(value) is not list or (non_empty and not value):  # noqa: E721
        raise _fail(f"{field_name} must be a{' non-empty' if non_empty else ''} array")
    return cast(list[object], value)


def _strict_object(raw: bytes, field_name: str) -> dict[str, object]:
    if type(raw) is not bytes:  # noqa: E721
        raise _fail(f"{field_name} must be bytes")
    try:
        value, canonical = load_canonical_json_bytes(raw, origin=field_name)
    except (CanonicalizationError, ValueError) as error:
        raise _fail(f"{field_name} must be strict UTF-8 canonical-subset JSON") from error
    if raw != canonical:
        raise _fail(f"{field_name} bytes must already use the exact canonical encoding")
    if type(value) is not dict:  # noqa: E721
        raise _fail(f"{field_name} root must be an object")
    return cast(dict[str, object], value)


def calibration_profile_key(profile_version: object) -> str:
    version = _text(profile_version, "profile_version")
    if _PROFILE_VERSION.fullmatch(version) is None:
        raise _fail("profile_version must be a canonical positive decimal string")
    return f"shadow_calibration@{version}"


def runtime_calibration_profile_key(profile_version: object, runtime_capability_id: object) -> str:
    """Return the v2 environment-specific accepted-record scope key."""
    version = _text(profile_version, "profile_version")
    if _PROFILE_VERSION.fullmatch(version) is None:
        raise _fail("profile_version must be a canonical positive decimal string")
    if runtime_capability_id not in {"pc_cuda", "mac_cpu"}:
        raise _fail("runtime capability id is invalid")
    return f"runtime_calibration@{runtime_capability_id}@{version}"


@dataclass(frozen=True, slots=True)
class CalibrationRecordScope:
    namespace: str
    kind: str
    key: str

    def __post_init__(self) -> None:
        if self.namespace != CALIBRATION_RECORD_NAMESPACE:
            raise _fail("scope namespace is not autocut_authority")
        if self.kind != CALIBRATION_RECORD_SCOPE_KIND:
            raise _fail("scope kind is not calibration")
        _scope_key(self.key)

    @classmethod
    def for_profile(
        cls, profile_version: str, runtime_capability_id: str | None = None
    ) -> CalibrationRecordScope:
        return cls(
            CALIBRATION_RECORD_NAMESPACE,
            CALIBRATION_RECORD_SCOPE_KIND,
            (
                calibration_profile_key(profile_version)
                if runtime_capability_id is None
                else runtime_calibration_profile_key(profile_version, runtime_capability_id)
            ),
        )

    def to_mapping(self) -> dict[str, str]:
        return {"namespace": self.namespace, "kind": self.kind, "key": self.key}


@dataclass(frozen=True, slots=True)
class CalibrationRecordIdentity:
    """Identity shared by the aggregate and both independently derived children."""

    profile_source_sha256: str
    registry_snapshot_sha256: str
    calibration_corpus_set_sha256: str
    native_port_identity_sha256: str
    source_clock_id: str
    source_time_base: TimeBase
    timed_speech_policy_sha256: str
    word_gap_policy_sha256: str
    vad_merge_policy_sha256: str
    alignment_policy_sha256: str
    acceptance_policy_sha256: str
    runtime_measurement_identity_sha256: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "profile_source_sha256",
            "registry_snapshot_sha256",
            "calibration_corpus_set_sha256",
            "native_port_identity_sha256",
            "timed_speech_policy_sha256",
            "word_gap_policy_sha256",
            "vad_merge_policy_sha256",
            "alignment_policy_sha256",
            "acceptance_policy_sha256",
        ):
            _sha(getattr(self, field_name), f"identity.{field_name}")
        _text(self.source_clock_id, "identity.source_clock_id")
        if type(self.source_time_base) is not TimeBase:  # noqa: E721
            raise _fail("identity.source_time_base must be an exact TimeBase")
        if self.runtime_measurement_identity_sha256 is not None:
            _sha(
                self.runtime_measurement_identity_sha256,
                "identity.runtime_measurement_identity_sha256",
            )

    def to_mapping(self) -> dict[str, object]:
        mapping: dict[str, object] = {
            "acceptance_policy_sha256": self.acceptance_policy_sha256,
            "alignment_policy_sha256": self.alignment_policy_sha256,
            "bound_algorithm_sha256": CALIBRATION_BOUND_ALGORITHM_SHA256,
            "calibration_corpus_set_sha256": self.calibration_corpus_set_sha256,
            "native_port_identity_sha256": self.native_port_identity_sha256,
            "profile_source_sha256": self.profile_source_sha256,
            "registry_snapshot_sha256": self.registry_snapshot_sha256,
            "source_clock_id": self.source_clock_id,
            "source_time_base": {
                "denominator": self.source_time_base.denominator,
                "numerator": self.source_time_base.numerator,
            },
            "timed_speech_policy_sha256": self.timed_speech_policy_sha256,
            "vad_merge_policy_sha256": self.vad_merge_policy_sha256,
            "word_gap_policy_sha256": self.word_gap_policy_sha256,
        }
        if self.runtime_measurement_identity_sha256 is not None:
            mapping["runtime_measurement_identity_sha256"] = self.runtime_measurement_identity_sha256
        return mapping

    @property
    def bound_algorithm_sha256(self) -> str:
        return CALIBRATION_BOUND_ALGORITHM_SHA256


@dataclass(frozen=True, slots=True)
class CalibrationRecordProducerIdentity:
    role: CalibrationRecordRole
    producer_id: str
    producer_version: str
    generation_policy_sha256: str
    detector_sha256: str
    calibration_policy_sha256: str
    model_id: str
    model_revision: str
    model_sha256: str
    inference_kind: str
    service_sha256: str

    def __post_init__(self) -> None:
        if type(self.role) is not CalibrationRecordRole:  # noqa: E721
            raise _fail("producer identity role is invalid")
        for field_name in ("producer_id", "producer_version", "model_id", "model_revision"):
            _text(getattr(self, field_name), f"producer_identity.{field_name}")
        for field_name in (
            "generation_policy_sha256",
            "detector_sha256",
            "calibration_policy_sha256",
            "model_sha256",
            "service_sha256",
        ):
            _sha(getattr(self, field_name), f"producer_identity.{field_name}")
        expected = {
            CalibrationRecordRole.ASR: "sensevoice-word-timestamp",
            CalibrationRecordRole.VAD: "fsmn-vad-direct",
        }[self.role]
        if self.inference_kind != expected:
            raise _fail("producer identity inference_kind is invalid for its role")
        expected_model_id = {
            CalibrationRecordRole.ASR: "SenseVoiceSmall",
            CalibrationRecordRole.VAD: "fsmn-vad",
        }[self.role]
        if self.model_id != expected_model_id:
            raise _fail("producer identity model_id is invalid for its role")

    def to_mapping(self) -> dict[str, object]:
        return {
            "calibration_policy_sha256": self.calibration_policy_sha256,
            "detector_sha256": self.detector_sha256,
            "generation_policy_sha256": self.generation_policy_sha256,
            "inference_kind": self.inference_kind,
            "model_id": self.model_id,
            "model_revision": self.model_revision,
            "model_sha256": self.model_sha256,
            "producer_id": self.producer_id,
            "producer_version": self.producer_version,
            "producer_kind": self.role.value,
            "service_sha256": self.service_sha256,
        }


@dataclass(frozen=True, slots=True)
class CalibrationEvidenceMember:
    ordinal: int
    corpus_member_reference_sha256: str
    expected_anchor_reference_sha256: str
    raw_response_blob_sha256: str
    projection_sha256: str

    def __post_init__(self) -> None:
        _integer(self.ordinal, "evidence_member.ordinal")
        for field_name in (
            "corpus_member_reference_sha256",
            "expected_anchor_reference_sha256",
            "raw_response_blob_sha256",
            "projection_sha256",
        ):
            _sha(getattr(self, field_name), f"evidence_member.{field_name}")

    def to_mapping(self) -> dict[str, object]:
        return {
            "corpus_member_reference_sha256": self.corpus_member_reference_sha256,
            "expected_anchor_reference_sha256": self.expected_anchor_reference_sha256,
            "ordinal": self.ordinal,
            "projection_sha256": self.projection_sha256,
            "raw_response_blob_sha256": self.raw_response_blob_sha256,
        }


@dataclass(frozen=True, slots=True)
class CalibrationMatchEvidence:
    anchor_id: str
    observation_id: str
    expected_range: TickRange
    observed_range: TickRange
    early_tick: int
    late_tick: int
    absolute_tick: int

    def __post_init__(self) -> None:
        _text(self.anchor_id, "match.anchor_id")
        _text(self.observation_id, "match.observation_id")
        if type(self.expected_range) is not TickRange or type(self.observed_range) is not TickRange:  # noqa: E721
            raise _fail("match ranges must be exact TickRange values")
        for field_name, interval in (
            ("expected_range", self.expected_range),
            ("observed_range", self.observed_range),
        ):
            _integer(interval.start_pts, f"match.{field_name}.in_tick")
            _integer(interval.end_pts, f"match.{field_name}.out_tick")
        expected_early = max(
            0,
            self.expected_range.start_pts - self.observed_range.start_pts,
            self.expected_range.end_pts - self.observed_range.end_pts,
        )
        expected_late = max(
            0,
            self.observed_range.start_pts - self.expected_range.start_pts,
            self.observed_range.end_pts - self.expected_range.end_pts,
        )
        values = (
            _integer(self.early_tick, "match.early_tick"),
            _integer(self.late_tick, "match.late_tick"),
            _integer(self.absolute_tick, "match.absolute_tick"),
        )
        if values != (expected_early, expected_late, max(expected_early, expected_late)):
            raise _fail("match errors do not equal integer endpoint recomputation")

    @classmethod
    def from_ranges(
        cls,
        anchor_id: str,
        observation_id: str,
        expected_range: TickRange,
        observed_range: TickRange,
    ) -> CalibrationMatchEvidence:
        early = max(
            0,
            expected_range.start_pts - observed_range.start_pts,
            expected_range.end_pts - observed_range.end_pts,
        )
        late = max(
            0,
            observed_range.start_pts - expected_range.start_pts,
            observed_range.end_pts - expected_range.end_pts,
        )
        return cls(anchor_id, observation_id, expected_range, observed_range, early, late, max(early, late))

    def to_mapping(self) -> dict[str, object]:
        return {
            "absolute_tick": self.absolute_tick,
            "anchor_id": self.anchor_id,
            "early_tick": self.early_tick,
            "expected_range": _range_mapping(self.expected_range),
            "late_tick": self.late_tick,
            "observation_id": self.observation_id,
            "observed_range": _range_mapping(self.observed_range),
        }


@dataclass(frozen=True, slots=True)
class CalibrationRecordMemberPayload:
    identity: CalibrationRecordIdentity
    producer_identity: CalibrationRecordProducerIdentity
    evidence_members: tuple[CalibrationEvidenceMember, ...]
    matches: tuple[CalibrationMatchEvidence, ...]
    early_maximum_tick: int
    late_maximum_tick: int
    absolute_maximum_tick: int
    accepted_bound_tick: int

    def __post_init__(self) -> None:
        if type(self.identity) is not CalibrationRecordIdentity:  # noqa: E721
            raise _fail("child identity must be exact")
        if type(self.producer_identity) is not CalibrationRecordProducerIdentity:  # noqa: E721
            raise _fail("child producer identity must be exact")
        if type(self.evidence_members) is not tuple or not self.evidence_members:  # noqa: E721
            raise _fail("child evidence_members must be a non-empty tuple")
        if any(type(item) is not CalibrationEvidenceMember for item in self.evidence_members):  # noqa: E721
            raise _fail("child evidence member type is invalid")
        if tuple(item.ordinal for item in self.evidence_members) != tuple(range(len(self.evidence_members))):
            raise _fail("child evidence members must be in exact contiguous ordinal order")
        for field_name in (
            "corpus_member_reference_sha256",
            "expected_anchor_reference_sha256",
            "raw_response_blob_sha256",
            "projection_sha256",
        ):
            identities = tuple(getattr(item, field_name) for item in self.evidence_members)
            if len(identities) != len(set(identities)):
                raise _fail(f"child evidence members duplicate {field_name}")
        if type(self.matches) is not tuple or not self.matches:  # noqa: E721
            raise _fail("child matches must be a non-empty tuple")
        if any(type(item) is not CalibrationMatchEvidence for item in self.matches):  # noqa: E721
            raise _fail("child match type is invalid")
        anchor_ids = tuple(item.anchor_id for item in self.matches)
        observation_ids = tuple(item.observation_id for item in self.matches)
        if len(anchor_ids) != len(set(anchor_ids)):
            raise _fail("child matches duplicate anchor_id")
        if len(observation_ids) != len(set(observation_ids)):
            raise _fail("child matches duplicate observation_id")
        maxima = (
            max(item.early_tick for item in self.matches),
            max(item.late_tick for item in self.matches),
            max(item.absolute_tick for item in self.matches),
        )
        supplied = (
            _integer(self.early_maximum_tick, "child.early_maximum_tick"),
            _integer(self.late_maximum_tick, "child.late_maximum_tick"),
            _integer(self.absolute_maximum_tick, "child.absolute_maximum_tick", minimum=1),
        )
        if supplied != maxima:
            raise _fail("child maxima do not equal the canonical match maxima")
        if _integer(self.accepted_bound_tick, "child.accepted_bound_tick", minimum=1) != maxima[2]:
            raise _fail("child accepted bound must equal the positive absolute maximum")

    @classmethod
    def from_matches(
        cls,
        identity: CalibrationRecordIdentity,
        producer_identity: CalibrationRecordProducerIdentity,
        evidence_members: tuple[CalibrationEvidenceMember, ...],
        matches: tuple[CalibrationMatchEvidence, ...],
    ) -> CalibrationRecordMemberPayload:
        if type(matches) is not tuple or not matches:  # noqa: E721
            raise _fail("child matches must be a non-empty tuple")
        return cls(
            identity,
            producer_identity,
            evidence_members,
            matches,
            max(item.early_tick for item in matches),
            max(item.late_tick for item in matches),
            max(item.absolute_tick for item in matches),
            max(item.absolute_tick for item in matches),
        )

    @property
    def role(self) -> CalibrationRecordRole:
        return self.producer_identity.role

    def to_mapping(self) -> dict[str, object]:
        return {
            "absolute_maximum_tick": self.absolute_maximum_tick,
            "accepted_bound_tick": self.accepted_bound_tick,
            "early_maximum_tick": self.early_maximum_tick,
            "evidence_members": [item.to_mapping() for item in self.evidence_members],
            "identity": self.identity.to_mapping(),
            "late_maximum_tick": self.late_maximum_tick,
            "matches": [item.to_mapping() for item in self.matches],
            "producer_kind": self.role.value,
            "producer_identity": self.producer_identity.to_mapping(),
            "record_role": self.role.value,
            "schema_version": CALIBRATION_RECORD_MEMBER_SCHEMA,
        }

    @property
    def content_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())


@dataclass(frozen=True, slots=True)
class CalibrationRecordPayload:
    identity: CalibrationRecordIdentity
    measurement_manifest_sha256: str
    measurement_results_sha256: str
    asr_member_sha256: str
    vad_member_sha256: str
    asr_accepted_bound_tick: int
    vad_accepted_bound_tick: int

    def __post_init__(self) -> None:
        if type(self.identity) is not CalibrationRecordIdentity:  # noqa: E721
            raise _fail("aggregate identity must be exact")
        for field_name in (
            "measurement_manifest_sha256",
            "measurement_results_sha256",
            "asr_member_sha256",
            "vad_member_sha256",
        ):
            _sha(getattr(self, field_name), f"aggregate.{field_name}")
        if self.measurement_manifest_sha256 == self.measurement_results_sha256:
            raise _fail("measurement manifest and results identities must be distinct")
        if self.asr_member_sha256 == self.vad_member_sha256:
            raise _fail("ASR and VAD child hashes must be distinct")
        _integer(self.asr_accepted_bound_tick, "aggregate.asr_accepted_bound_tick", minimum=1)
        _integer(self.vad_accepted_bound_tick, "aggregate.vad_accepted_bound_tick", minimum=1)

    def to_mapping(self) -> dict[str, object]:
        return {
            "asr_accepted_bound_tick": self.asr_accepted_bound_tick,
            "asr_member_sha256": self.asr_member_sha256,
            "identity": self.identity.to_mapping(),
            "measurement_manifest_sha256": self.measurement_manifest_sha256,
            "measurement_results_sha256": self.measurement_results_sha256,
            "member_count": 2,
            "record_kind": "shadow_native_timing",
            "schema_version": CALIBRATION_RECORD_SCHEMA,
            "vad_accepted_bound_tick": self.vad_accepted_bound_tick,
            "vad_member_sha256": self.vad_member_sha256,
        }

    @property
    def content_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())


@dataclass(frozen=True, slots=True)
class CalibrationValidationReceiptPayload:
    bound_algorithm_sha256: str
    measurement_manifest_sha256: str
    measurement_results_sha256: str
    validation_input_sha256: str
    validation_result_sha256: str
    record_sha256: str
    asr_member_sha256: str
    vad_member_sha256: str
    checks: tuple[str, ...] = CALIBRATION_VALIDATION_CHECKS

    def __post_init__(self) -> None:
        for field_name in (
            "bound_algorithm_sha256",
            "measurement_manifest_sha256",
            "measurement_results_sha256",
            "validation_input_sha256",
            "validation_result_sha256",
            "record_sha256",
            "asr_member_sha256",
            "vad_member_sha256",
        ):
            _sha(getattr(self, field_name), f"validation_receipt.{field_name}")
        if type(self.checks) is not tuple or self.checks != CALIBRATION_VALIDATION_CHECKS:  # noqa: E721
            raise _fail("validation receipt checks are not the exact recomputation set")
        if self.bound_algorithm_sha256 != CALIBRATION_BOUND_ALGORITHM_SHA256:
            raise _fail("validation receipt bound algorithm is not the module-owned algorithm")
        if len({self.record_sha256, self.asr_member_sha256, self.vad_member_sha256}) != 3:
            raise _fail("validation receipt aggregate and child hashes must be distinct")

    def to_mapping(self) -> dict[str, object]:
        return {
            "asr_member_sha256": self.asr_member_sha256,
            "bound_algorithm_sha256": self.bound_algorithm_sha256,
            "checks": list(self.checks),
            "decision": "accepted",
            "measurement_manifest_sha256": self.measurement_manifest_sha256,
            "measurement_results_sha256": self.measurement_results_sha256,
            "record_sha256": self.record_sha256,
            "schema_version": CALIBRATION_VALIDATION_RECEIPT_SCHEMA,
            "vad_member_sha256": self.vad_member_sha256,
            "validation_input_sha256": self.validation_input_sha256,
            "validation_result_sha256": self.validation_result_sha256,
            "validator_command": CALIBRATION_VALIDATOR_COMMAND,
            "validator_principal": CALIBRATION_VALIDATOR_PRINCIPAL,
        }

    @property
    def content_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())


CalibrationPayload = (
    CalibrationRecordPayload | CalibrationRecordMemberPayload | CalibrationValidationReceiptPayload
)


@dataclass(frozen=True, slots=True)
class CalibrationRecordArtifactMember:
    ordinal: int
    artifact_type: str
    logical_id: str
    revision: int
    scope: CalibrationRecordScope
    content_hash: str
    payload: CalibrationPayload

    def __post_init__(self) -> None:
        _integer(self.ordinal, "artifact.ordinal")
        _text(self.artifact_type, "artifact.artifact_type")
        _text(self.logical_id, "artifact.logical_id")
        if type(self.revision) is not int or self.revision != CALIBRATION_RECORD_REVISION:  # noqa: E721
            raise _fail("artifact revision must be exactly 1")
        if type(self.scope) is not CalibrationRecordScope:  # noqa: E721
            raise _fail("artifact scope must be exact")
        digest = _sha(self.content_hash, "artifact.content_hash")
        if type(self.payload) not in (  # noqa: E721
            CalibrationRecordPayload,
            CalibrationRecordMemberPayload,
            CalibrationValidationReceiptPayload,
        ):
            raise _fail("artifact payload type is invalid")
        if digest != self.payload.content_hash:
            raise _fail("artifact content hash does not equal its canonical payload")

    @property
    def payload_bytes(self) -> bytes:
        return canonical_json_bytes(self.payload.to_mapping())

    @property
    def payload_json(self) -> str:
        return self.payload_bytes.decode("utf-8")


@dataclass(frozen=True, slots=True)
class CalibrationRecordArtifactSet:
    members: tuple[CalibrationRecordArtifactMember, ...]

    def __post_init__(self) -> None:
        verify_calibration_record_artifact_set(self.members)

    @property
    def aggregate(self) -> CalibrationRecordPayload:
        return cast(CalibrationRecordPayload, self.members[0].payload)

    @property
    def asr(self) -> CalibrationRecordMemberPayload:
        return cast(CalibrationRecordMemberPayload, self.members[1].payload)

    @property
    def vad(self) -> CalibrationRecordMemberPayload:
        return cast(CalibrationRecordMemberPayload, self.members[2].payload)

    @property
    def validation(self) -> CalibrationValidationReceiptPayload:
        return cast(CalibrationValidationReceiptPayload, self.members[3].payload)


@dataclass(frozen=True, slots=True)
class RuntimeCalibrationCapability:
    """The v2 admission identity attached to an immutable accepted v2 record.

    The separate v2 scope keeps historical v1 records readable without
    allowing them to authorize a normal runtime by themselves.
    """

    runtime_measurement_identity: RuntimeMeasurementIdentity
    profile_source_sha256: str
    registry_snapshot_sha256: str
    record_sha256: str
    validation_receipt_sha256: str

    def __post_init__(self) -> None:
        if type(self.runtime_measurement_identity) is not RuntimeMeasurementIdentity:  # noqa: E721
            raise _fail("runtime capability requires an exact measurement identity")
        for field_name in (
            "profile_source_sha256",
            "registry_snapshot_sha256",
            "record_sha256",
            "validation_receipt_sha256",
        ):
            _sha(getattr(self, field_name), f"runtime capability.{field_name}")
        if self.record_sha256 == self.validation_receipt_sha256:
            raise _fail("runtime capability record and validation receipt must differ")

    @classmethod
    def from_record(
        cls,
        record: CalibrationRecordArtifactSet,
        runtime_measurement_identity: RuntimeMeasurementIdentity,
    ) -> RuntimeCalibrationCapability:
        if type(record) is not CalibrationRecordArtifactSet:  # noqa: E721
            raise _fail("runtime capability requires an exact accepted record")
        identity = record.aggregate.identity
        return cls(
            runtime_measurement_identity,
            identity.profile_source_sha256,
            identity.registry_snapshot_sha256,
            record.members[0].content_hash,
            record.members[3].content_hash,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": RUNTIME_CALIBRATION_CAPABILITY_SCHEMA,
            "runtime_measurement_identity": self.runtime_measurement_identity.to_mapping(),
            "profile_source_sha256": self.profile_source_sha256,
            "registry_snapshot_sha256": self.registry_snapshot_sha256,
            "record_sha256": self.record_sha256,
            "validation_receipt_sha256": self.validation_receipt_sha256,
        }

    @property
    def content_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())


@dataclass(frozen=True, slots=True)
class CalibrationRecordCandidate:
    """Unaccepted payload closure; only the authority validator may persist it."""

    profile_version: str
    aggregate: CalibrationRecordPayload
    asr: CalibrationRecordMemberPayload
    vad: CalibrationRecordMemberPayload
    runtime_capability_id: str | None = None

    def __post_init__(self) -> None:
        if self.runtime_capability_id is None:
            calibration_profile_key(self.profile_version)
        else:
            runtime_calibration_profile_key(self.profile_version, self.runtime_capability_id)
        verify_calibration_record_candidate(self)

    @property
    def profile_key(self) -> str:
        return (
            calibration_profile_key(self.profile_version)
            if self.runtime_capability_id is None
            else runtime_calibration_profile_key(self.profile_version, self.runtime_capability_id)
        )


@dataclass(frozen=True, slots=True)
class IndependentlyRecomputedCalibrationResult:
    """Closed internal validator output, never a replacement for raw-evidence replay.

    Structural construction only checks result binding. It cannot establish
    who performed recomputation, so only the independently running validator
    may supply this object to the protected Store path.
    """

    candidate: CalibrationRecordCandidate
    checks: tuple[str, ...]
    validation_input_sha256: str
    validation_result_sha256: str
    validator_command: str
    validator_principal: str

    def __post_init__(self) -> None:
        if type(self.candidate) is not CalibrationRecordCandidate:  # noqa: E721
            raise _fail("independent result candidate must be exact")
        if type(self.checks) is not tuple or self.checks != CALIBRATION_VALIDATION_CHECKS:  # noqa: E721
            raise _fail("independent result lacks the exact recomputation checks")
        if self.validator_command != CALIBRATION_VALIDATOR_COMMAND:
            raise _fail("independent result validator command is invalid")
        if self.validator_principal != CALIBRATION_VALIDATOR_PRINCIPAL:
            raise _fail("independent result validator principal is invalid")
        expected_input = calibration_validation_input_hash(
            profile_key=self.candidate.profile_key,
            identity=self.candidate.aggregate.identity,
            measurement_manifest_sha256=self.candidate.aggregate.measurement_manifest_sha256,
            measurement_results_sha256=self.candidate.aggregate.measurement_results_sha256,
            asr=self.candidate.asr,
            vad=self.candidate.vad,
        )
        expected_result = calibration_validation_result_hash(
            self.candidate.aggregate, self.candidate.asr, self.candidate.vad
        )
        if self.validation_input_sha256 != expected_input:
            raise _fail("independent validation input proof does not recompute")
        if self.validation_result_sha256 != expected_result:
            raise _fail("independent validation result proof does not recompute")


def calibration_validation_input_hash(
    *,
    profile_key: str,
    identity: CalibrationRecordIdentity,
    measurement_manifest_sha256: str,
    measurement_results_sha256: str,
    asr: CalibrationRecordMemberPayload,
    vad: CalibrationRecordMemberPayload,
) -> str:
    """Hash every immutable input that the independent validator must recompute."""
    return canonical_json_hash(
        {
            "asr_evidence_members": [item.to_mapping() for item in asr.evidence_members],
            "asr_matches": [item.to_mapping() for item in asr.matches],
            "identity": identity.to_mapping(),
            "measurement_manifest_sha256": _sha(
                measurement_manifest_sha256, "measurement_manifest_sha256"
            ),
            "measurement_results_sha256": _sha(
                measurement_results_sha256, "measurement_results_sha256"
            ),
            "profile_key": _scope_key(profile_key),
            "schema_version": CALIBRATION_VALIDATION_INPUT_SCHEMA,
            "vad_evidence_members": [item.to_mapping() for item in vad.evidence_members],
            "vad_matches": [item.to_mapping() for item in vad.matches],
        }
    )


def calibration_validation_result_hash(
    aggregate: CalibrationRecordPayload,
    asr: CalibrationRecordMemberPayload,
    vad: CalibrationRecordMemberPayload,
) -> str:
    """Hash the independently recomputed accepted result without receipt self-reference."""
    return canonical_json_hash(
        {
            "asr_accepted_bound_tick": asr.accepted_bound_tick,
            "asr_member_sha256": asr.content_hash,
            "checks": list(CALIBRATION_VALIDATION_CHECKS),
            "record_sha256": aggregate.content_hash,
            "schema_version": CALIBRATION_VALIDATION_RESULT_SCHEMA,
            "vad_accepted_bound_tick": vad.accepted_bound_tick,
            "vad_member_sha256": vad.content_hash,
        }
    )


def build_calibration_record_candidate(
    *,
    profile_version: str,
    identity: CalibrationRecordIdentity,
    measurement_manifest_sha256: str,
    measurement_results_sha256: str,
    asr: CalibrationRecordMemberPayload,
    vad: CalibrationRecordMemberPayload,
    runtime_capability_id: str | None = None,
) -> CalibrationRecordCandidate:
    """Build an unaccepted candidate; this function cannot create a receipt or ArtifactSet."""
    if type(identity) is not CalibrationRecordIdentity:  # noqa: E721
        raise _fail("builder identity must be exact")
    if type(asr) is not CalibrationRecordMemberPayload or asr.role is not CalibrationRecordRole.ASR:  # noqa: E721
        raise _fail("builder ASR child must be an exact ASR member")
    if type(vad) is not CalibrationRecordMemberPayload or vad.role is not CalibrationRecordRole.VAD:  # noqa: E721
        raise _fail("builder VAD child must be an exact VAD member")
    if asr.identity != identity or vad.identity != identity:
        raise _fail("candidate child profile/source/RegistrySet/policy identities do not agree")
    aggregate = CalibrationRecordPayload(
        identity,
        _sha(measurement_manifest_sha256, "measurement_manifest_sha256"),
        _sha(measurement_results_sha256, "measurement_results_sha256"),
        asr.content_hash,
        vad.content_hash,
        asr.accepted_bound_tick,
        vad.accepted_bound_tick,
    )
    return CalibrationRecordCandidate(profile_version, aggregate, asr, vad, runtime_capability_id)


def verify_calibration_record_candidate(candidate: CalibrationRecordCandidate) -> None:
    """Verify candidate closure without granting or asserting accepted status."""
    if type(candidate) is not CalibrationRecordCandidate:  # noqa: E721
        raise _fail("candidate must be an exact CalibrationRecordCandidate")
    aggregate, asr, vad = candidate.aggregate, candidate.asr, candidate.vad
    if type(aggregate) is not CalibrationRecordPayload:  # noqa: E721
        raise _fail("candidate aggregate must be exact")
    if type(asr) is not CalibrationRecordMemberPayload or asr.role is not CalibrationRecordRole.ASR:  # noqa: E721
        raise _fail("candidate ASR child must be exact")
    if type(vad) is not CalibrationRecordMemberPayload or vad.role is not CalibrationRecordRole.VAD:  # noqa: E721
        raise _fail("candidate VAD child must be exact")
    if asr.identity != aggregate.identity or vad.identity != aggregate.identity:
        raise _fail("candidate has mismatched profile/source/RegistrySet/policy identity")
    if asr.evidence_members != vad.evidence_members:
        raise _fail("candidate children must cover the same ordered corpus evidence")
    asr_producer, vad_producer = asr.producer_identity, vad.producer_identity
    for field_name in ("producer_id", "detector_sha256", "model_id", "model_sha256"):
        if getattr(asr_producer, field_name) == getattr(vad_producer, field_name):
            raise _fail(f"ASR and VAD {field_name} values must be distinct")
    if len({aggregate.content_hash, asr.content_hash, vad.content_hash}) != 3:
        raise _fail("aggregate and child content hashes must all be distinct")
    if (
        aggregate.asr_member_sha256 != asr.content_hash
        or aggregate.vad_member_sha256 != vad.content_hash
        or aggregate.asr_accepted_bound_tick != asr.accepted_bound_tick
        or aggregate.vad_accepted_bound_tick != vad.accepted_bound_tick
    ):
        raise _fail("candidate aggregate does not close over child hashes and positive bounds")


def validator_internal_assemble_accepted_artifact_set(
    result: IndependentlyRecomputedCalibrationResult,
) -> CalibrationRecordArtifactSet:
    """Internal validator-only persistence seam; Store ownership remains authoritative."""
    if type(result) is not IndependentlyRecomputedCalibrationResult:  # noqa: E721
        raise _fail("accepted assembly requires an independently recomputed result")
    candidate = result.candidate
    aggregate, asr, vad = candidate.aggregate, candidate.asr, candidate.vad
    profile_key = candidate.profile_key
    validation = CalibrationValidationReceiptPayload(
        CALIBRATION_BOUND_ALGORITHM_SHA256,
        aggregate.measurement_manifest_sha256,
        aggregate.measurement_results_sha256,
        result.validation_input_sha256,
        result.validation_result_sha256,
        aggregate.content_hash,
        asr.content_hash,
        vad.content_hash,
    )
    scope = CalibrationRecordScope.for_profile(candidate.profile_version, candidate.runtime_capability_id)
    payloads: tuple[tuple[str, str, CalibrationPayload], ...] = (
        (CALIBRATION_RECORD_ARTIFACT_TYPE, f"calibration-record/aggregate/{profile_key}/1", aggregate),
        (CALIBRATION_RECORD_MEMBER_ARTIFACT_TYPE, f"calibration-record/member/asr/{profile_key}/1", asr),
        (CALIBRATION_RECORD_MEMBER_ARTIFACT_TYPE, f"calibration-record/member/vad/{profile_key}/1", vad),
        (CALIBRATION_VALIDATION_RECEIPT_ARTIFACT_TYPE, f"calibration-record/validation/{profile_key}/1", validation),
    )
    return CalibrationRecordArtifactSet(
        tuple(
            CalibrationRecordArtifactMember(
                ordinal,
                artifact_type,
                logical_id,
                CALIBRATION_RECORD_REVISION,
                scope,
                payload.content_hash,
                payload,
            )
            for ordinal, (artifact_type, logical_id, payload) in enumerate(payloads)
        )
    )


def verify_calibration_record_artifact_set(
    members: tuple[CalibrationRecordArtifactMember, ...],
) -> None:
    """Fail closed unless ``members`` are the exact independently checkable closure."""
    if type(members) is not tuple or len(members) != 4:  # noqa: E721
        raise _fail("accepted artifact set must contain exactly four members")
    if any(type(member) is not CalibrationRecordArtifactMember for member in members):  # noqa: E721
        raise _fail("artifact set members must be exact typed values")
    if tuple(member.ordinal for member in members) != (0, 1, 2, 3):
        raise _fail("artifact set member order must be exactly 0,1,2,3")
    expected_types = (
        CALIBRATION_RECORD_ARTIFACT_TYPE,
        CALIBRATION_RECORD_MEMBER_ARTIFACT_TYPE,
        CALIBRATION_RECORD_MEMBER_ARTIFACT_TYPE,
        CALIBRATION_VALIDATION_RECEIPT_ARTIFACT_TYPE,
    )
    if tuple(member.artifact_type for member in members) != expected_types:
        raise _fail("artifact set member types are not the accepted four-member sequence")
    if any(member.revision != CALIBRATION_RECORD_REVISION for member in members):
        raise _fail("artifact set revision must be exactly 1")
    if len({member.scope for member in members}) != 1:
        raise _fail("artifact set scopes must be identical")
    scope = members[0].scope
    expected_ids = (
        f"calibration-record/aggregate/{scope.key}/1",
        f"calibration-record/member/asr/{scope.key}/1",
        f"calibration-record/member/vad/{scope.key}/1",
        f"calibration-record/validation/{scope.key}/1",
    )
    if tuple(member.logical_id for member in members) != expected_ids:
        raise _fail("artifact set logical IDs do not match the frozen grammar")
    expected_payload_types = (
        CalibrationRecordPayload,
        CalibrationRecordMemberPayload,
        CalibrationRecordMemberPayload,
        CalibrationValidationReceiptPayload,
    )
    if tuple(type(member.payload) for member in members) != expected_payload_types:
        raise _fail("artifact set payload types do not match their ordinals")
    aggregate = cast(CalibrationRecordPayload, members[0].payload)
    asr = cast(CalibrationRecordMemberPayload, members[1].payload)
    vad = cast(CalibrationRecordMemberPayload, members[2].payload)
    receipt = cast(CalibrationValidationReceiptPayload, members[3].payload)
    if scope.key.startswith("shadow_calibration@"):
        candidate = CalibrationRecordCandidate(
            scope.key.removeprefix("shadow_calibration@"), aggregate, asr, vad
        )
    else:
        capability_id, _, version = scope.key.removeprefix("runtime_calibration@").partition("@")
        candidate = CalibrationRecordCandidate(version, aggregate, asr, vad, capability_id)
    if candidate.profile_key != scope.key:
        raise _fail("artifact set scope does not close to its candidate identity")
    if (
        receipt.bound_algorithm_sha256 != aggregate.identity.bound_algorithm_sha256
        or receipt.measurement_manifest_sha256 != aggregate.measurement_manifest_sha256
        or receipt.measurement_results_sha256 != aggregate.measurement_results_sha256
        or receipt.record_sha256 != members[0].content_hash
        or receipt.asr_member_sha256 != members[1].content_hash
        or receipt.vad_member_sha256 != members[2].content_hash
    ):
        raise _fail("validation receipt does not bind the recomputed record inputs and result")
    expected_input = calibration_validation_input_hash(
        profile_key=scope.key,
        identity=aggregate.identity,
        measurement_manifest_sha256=aggregate.measurement_manifest_sha256,
        measurement_results_sha256=aggregate.measurement_results_sha256,
        asr=asr,
        vad=vad,
    )
    expected_result = calibration_validation_result_hash(aggregate, asr, vad)
    if receipt.validation_input_sha256 != expected_input or receipt.validation_result_sha256 != expected_result:
        raise _fail("validation receipt acceptance is not backed by recomputed input/result bindings")


def decode_calibration_record_payload(raw: bytes) -> CalibrationRecordPayload:
    mapping = _closed(
        _strict_object(raw, "calibration record payload"),
        frozenset(
            {
                "schema_version", "record_kind", "identity", "measurement_manifest_sha256",
                "measurement_results_sha256", "asr_member_sha256", "vad_member_sha256",
                "asr_accepted_bound_tick", "vad_accepted_bound_tick", "member_count",
            }
        ),
        "calibration record payload",
    )
    if mapping["schema_version"] != CALIBRATION_RECORD_SCHEMA:
        raise _fail("aggregate schema_version is invalid")
    if mapping["record_kind"] != "shadow_native_timing" or mapping["member_count"] != 2:
        raise _fail("aggregate kind/member_count is invalid")
    return CalibrationRecordPayload(
        _decode_identity(mapping["identity"], "aggregate.identity"),
        cast(str, mapping["measurement_manifest_sha256"]),
        cast(str, mapping["measurement_results_sha256"]),
        cast(str, mapping["asr_member_sha256"]),
        cast(str, mapping["vad_member_sha256"]),
        cast(int, mapping["asr_accepted_bound_tick"]),
        cast(int, mapping["vad_accepted_bound_tick"]),
    )


def decode_calibration_record_member_payload(raw: bytes) -> CalibrationRecordMemberPayload:
    mapping = _closed(
        _strict_object(raw, "calibration record member payload"),
        frozenset(
            {
                "schema_version", "record_role", "producer_kind", "identity", "producer_identity",
                "evidence_members", "matches", "early_maximum_tick", "late_maximum_tick",
                "absolute_maximum_tick", "accepted_bound_tick",
            }
        ),
        "calibration record member payload",
    )
    if mapping["schema_version"] != CALIBRATION_RECORD_MEMBER_SCHEMA:
        raise _fail("child schema_version is invalid")
    try:
        role = CalibrationRecordRole(_text(mapping["record_role"], "child.record_role"))
    except ValueError as error:
        raise _fail("child record_role is invalid") from error
    if mapping["producer_kind"] != role.value:
        raise _fail("child producer_kind and record_role must agree")
    producer_identity = _decode_producer_identity(mapping["producer_identity"])
    if producer_identity.role is not role:
        raise _fail("child producer identity role does not agree")
    evidence = tuple(
        _decode_evidence(item, f"child.evidence_members[{position}]")
        for position, item in enumerate(_array(mapping["evidence_members"], "child.evidence_members", non_empty=True))
    )
    matches = tuple(
        _decode_match(item, f"child.matches[{position}]")
        for position, item in enumerate(_array(mapping["matches"], "child.matches", non_empty=True))
    )
    return CalibrationRecordMemberPayload(
        _decode_identity(mapping["identity"], "child.identity"),
        producer_identity,
        evidence,
        matches,
        cast(int, mapping["early_maximum_tick"]),
        cast(int, mapping["late_maximum_tick"]),
        cast(int, mapping["absolute_maximum_tick"]),
        cast(int, mapping["accepted_bound_tick"]),
    )


def decode_calibration_validation_receipt_payload(raw: bytes) -> CalibrationValidationReceiptPayload:
    mapping = _closed(
        _strict_object(raw, "calibration validation receipt payload"),
        frozenset(
            {
                "schema_version", "decision", "validator_command", "validator_principal",
                "bound_algorithm_sha256", "measurement_manifest_sha256",
                "measurement_results_sha256", "validation_input_sha256",
                "validation_result_sha256", "record_sha256", "asr_member_sha256",
                "vad_member_sha256", "checks",
            }
        ),
        "calibration validation receipt payload",
    )
    constants = (
        ("schema_version", CALIBRATION_VALIDATION_RECEIPT_SCHEMA),
        ("decision", "accepted"),
        ("validator_command", CALIBRATION_VALIDATOR_COMMAND),
        ("validator_principal", CALIBRATION_VALIDATOR_PRINCIPAL),
    )
    if any(type(mapping[name]) is not str or mapping[name] != expected for name, expected in constants):
        raise _fail("validation receipt constants are invalid")
    checks = tuple(
        _text(item, f"validation_receipt.checks[{position}]")
        for position, item in enumerate(_array(mapping["checks"], "validation_receipt.checks"))
    )
    return CalibrationValidationReceiptPayload(
        cast(str, mapping["bound_algorithm_sha256"]),
        cast(str, mapping["measurement_manifest_sha256"]),
        cast(str, mapping["measurement_results_sha256"]),
        cast(str, mapping["validation_input_sha256"]),
        cast(str, mapping["validation_result_sha256"]),
        cast(str, mapping["record_sha256"]),
        cast(str, mapping["asr_member_sha256"]),
        cast(str, mapping["vad_member_sha256"]),
        checks,
    )


def _decode_identity(value: object, field_name: str) -> CalibrationRecordIdentity:
    fields = {
                "profile_source_sha256", "registry_snapshot_sha256", "calibration_corpus_set_sha256",
                "native_port_identity_sha256", "source_clock_id", "source_time_base",
                "timed_speech_policy_sha256", "word_gap_policy_sha256",
                "vad_merge_policy_sha256", "alignment_policy_sha256",
                "acceptance_policy_sha256", "bound_algorithm_sha256",
            }
    if type(value) is dict and "runtime_measurement_identity_sha256" in value:  # noqa: E721
        fields.add("runtime_measurement_identity_sha256")
    mapping = _closed(value, frozenset(fields), field_name)
    time_base = _closed(
        mapping["source_time_base"], frozenset({"numerator", "denominator"}), f"{field_name}.source_time_base"
    )
    try:
        decoded_time_base = TimeBase(
            _integer(time_base["numerator"], f"{field_name}.source_time_base.numerator", minimum=1),
            _integer(time_base["denominator"], f"{field_name}.source_time_base.denominator", minimum=1),
        )
    except ValueError as error:
        raise _fail(f"{field_name}.source_time_base is invalid") from error
    if (
        _sha(mapping["bound_algorithm_sha256"], f"{field_name}.bound_algorithm_sha256")
        != CALIBRATION_BOUND_ALGORITHM_SHA256
    ):
        raise _fail(f"{field_name}.bound_algorithm_sha256 is not the frozen algorithm")
    return CalibrationRecordIdentity(
        cast(str, mapping["profile_source_sha256"]),
        cast(str, mapping["registry_snapshot_sha256"]),
        cast(str, mapping["calibration_corpus_set_sha256"]),
        cast(str, mapping["native_port_identity_sha256"]),
        cast(str, mapping["source_clock_id"]),
        decoded_time_base,
        cast(str, mapping["timed_speech_policy_sha256"]),
        cast(str, mapping["word_gap_policy_sha256"]),
        cast(str, mapping["vad_merge_policy_sha256"]),
        cast(str, mapping["alignment_policy_sha256"]),
        cast(str, mapping["acceptance_policy_sha256"]),
        cast(str | None, mapping.get("runtime_measurement_identity_sha256")),
    )


def _decode_producer_identity(value: object) -> CalibrationRecordProducerIdentity:
    mapping = _closed(
        value,
        frozenset(
            {
                "producer_kind", "producer_id", "producer_version", "generation_policy_sha256",
                "detector_sha256", "calibration_policy_sha256", "model_id", "model_revision",
                "model_sha256", "inference_kind", "service_sha256",
            }
        ),
        "producer_identity",
    )
    try:
        role = CalibrationRecordRole(
            _text(mapping["producer_kind"], "producer_identity.producer_kind")
        )
    except ValueError as error:
        raise _fail("producer_identity.role is invalid") from error
    return CalibrationRecordProducerIdentity(
        role,
        cast(str, mapping["producer_id"]),
        cast(str, mapping["producer_version"]),
        cast(str, mapping["generation_policy_sha256"]),
        cast(str, mapping["detector_sha256"]),
        cast(str, mapping["calibration_policy_sha256"]),
        cast(str, mapping["model_id"]),
        cast(str, mapping["model_revision"]),
        cast(str, mapping["model_sha256"]),
        cast(str, mapping["inference_kind"]),
        cast(str, mapping["service_sha256"]),
    )


def _decode_evidence(value: object, field_name: str) -> CalibrationEvidenceMember:
    mapping = _closed(
        value,
        frozenset(
            {
                "ordinal", "corpus_member_reference_sha256", "expected_anchor_reference_sha256",
                "raw_response_blob_sha256", "projection_sha256",
            }
        ),
        field_name,
    )
    return CalibrationEvidenceMember(
        cast(int, mapping["ordinal"]),
        cast(str, mapping["corpus_member_reference_sha256"]),
        cast(str, mapping["expected_anchor_reference_sha256"]),
        cast(str, mapping["raw_response_blob_sha256"]),
        cast(str, mapping["projection_sha256"]),
    )


def _decode_match(value: object, field_name: str) -> CalibrationMatchEvidence:
    mapping = _closed(
        value,
        frozenset(
            {
                "anchor_id", "observation_id", "expected_range", "observed_range",
                "early_tick", "late_tick", "absolute_tick",
            }
        ),
        field_name,
    )
    return CalibrationMatchEvidence(
        cast(str, mapping["anchor_id"]),
        cast(str, mapping["observation_id"]),
        _decode_range(mapping["expected_range"], f"{field_name}.expected_range"),
        _decode_range(mapping["observed_range"], f"{field_name}.observed_range"),
        cast(int, mapping["early_tick"]),
        cast(int, mapping["late_tick"]),
        cast(int, mapping["absolute_tick"]),
    )


def _decode_range(value: object, field_name: str) -> TickRange:
    mapping = _closed(value, frozenset({"in_tick", "out_tick"}), field_name)
    try:
        return TickRange(
            _integer(mapping["in_tick"], f"{field_name}.in_tick"),
            _integer(mapping["out_tick"], f"{field_name}.out_tick"),
        )
    except ValueError as error:
        raise _fail(f"{field_name} is invalid") from error


def _range_mapping(value: TickRange) -> dict[str, int]:
    return {"in_tick": value.start_pts, "out_tick": value.end_pts}


def _scope_key(value: str) -> str:
    if type(value) is not str:  # noqa: E721
        raise _fail("profile_key is invalid")
    if value.startswith("shadow_calibration@"):
        return calibration_profile_key(value.removeprefix("shadow_calibration@"))
    prefix = "runtime_calibration@"
    if value.startswith(prefix):
        capability_id, separator, version = value.removeprefix(prefix).partition("@")
        if separator:
            return runtime_calibration_profile_key(version, capability_id)
    raise _fail("profile_key is invalid")


__all__ = [
    "CALIBRATION_BOUND_ALGORITHM_SHA256",
    "CALIBRATION_RECORD_ARTIFACT_TYPE",
    "CALIBRATION_RECORD_MEMBER_ARTIFACT_TYPE",
    "CALIBRATION_RECORD_NAMESPACE",
    "CALIBRATION_RECORD_REVISION",
    "CALIBRATION_RECORD_SCOPE_KIND",
    "CALIBRATION_RECORD_SCHEMA",
    "CALIBRATION_VALIDATION_RECEIPT_ARTIFACT_TYPE",
    "CalibrationEvidenceMember",
    "CalibrationMatchEvidence",
    "CalibrationRecordCandidate",
    "CalibrationRecordIdentity",
    "CalibrationRecordMemberPayload",
    "CalibrationRecordPayload",
    "CalibrationRecordProducerIdentity",
    "CalibrationRecordRole",
    "build_calibration_record_candidate",
    "calibration_profile_key",
    "runtime_calibration_profile_key",
    "decode_calibration_record_member_payload",
    "decode_calibration_record_payload",
    "verify_calibration_record_candidate",
]
