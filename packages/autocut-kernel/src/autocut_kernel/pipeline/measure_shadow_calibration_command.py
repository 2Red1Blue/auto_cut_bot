"""Durable shadow-calibration measurement command over typed locked evidence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Literal, Mapping, Protocol, cast

from ..media import (
    CalibrationAnchor,
    CalibrationAnchorMatch,
    CalibrationMeasurementSummary,
    CalibrationProducer,
    CalibrationRecordError,
    ProducerCalibrationMeasurement,
    TimeBase,
)
from ..media.types import canonical_sha256, sha256_prefixed
from ..store import (
    ArtifactMember,
    ArtifactScope,
    BlobRef,
    CommandClaim,
    CommandOutcome,
    CommandRejection,
    CommandSuccess,
    Job,
)
from ..store.models import canonical_payload_hash

MEASURE_SHADOW_CALIBRATION_COMMAND = "MeasureShadowCalibrationCommand@2.1.3"
SHADOW_CALIBRATION_MEASUREMENT_PROTOCOL = "shadow-calibration-measurement-v1"


class ShadowCalibrationCommandError(ValueError):
    """Typed shadow-calibration evidence does not close over the locked request."""


class ShadowCalibrationProducerFailureCode(str, Enum):
    """The only native-port failures safe to persist in a terminal receipt."""

    REJECTED = "SHADOW_CALIBRATION_NATIVE_REJECTED"
    UNAVAILABLE = "SHADOW_CALIBRATION_NATIVE_UNAVAILABLE"


class ShadowCalibrationProducerError(RuntimeError):
    """A closed native-port terminal outcome after the command has claimed its slot."""

    def __init__(self, code: ShadowCalibrationProducerFailureCode) -> None:
        if type(code) is not ShadowCalibrationProducerFailureCode:  # noqa: E721
            raise ValueError("producer failure code must be exact")
        super().__init__(code.value)
        self.code = code


class ShadowCalibrationCoverage(str, Enum):
    COMPLETE = "complete"
    INDETERMINATE = "indeterminate"
    PARTIAL = "partial"


class ShadowCalibrationNativeResponseMediaType(str, Enum):
    """The sole raw native-response encoding accepted by this measurement boundary."""

    JSON = "application/vnd.autocut.funasr-native-response+json"


def _text(value: object, field_name: str) -> str:
    if type(value) is not str or not value.strip():  # noqa: E721
        raise ShadowCalibrationCommandError(f"{field_name} must be non-empty text")
    return value


def _sha(value: object, field_name: str) -> str:
    try:
        return sha256_prefixed(value, field_name)
    except ValueError as error:
        raise ShadowCalibrationCommandError(str(error)) from error


def _time_base(value: object, field_name: str) -> TimeBase:
    if type(value) is not TimeBase:  # noqa: E721
        raise ShadowCalibrationCommandError(f"{field_name} must be an exact TimeBase")
    return value


def _anchors(value: object, field_name: str) -> tuple[CalibrationAnchor, ...]:
    if type(value) is not tuple or not value:  # noqa: E721
        raise ShadowCalibrationCommandError(f"{field_name} must be a non-empty tuple")
    raw_anchors = cast(tuple[object, ...], value)
    if any(type(item) is not CalibrationAnchor for item in raw_anchors):  # noqa: E721
        raise ShadowCalibrationCommandError(f"{field_name} must contain exact CalibrationAnchor values")
    anchors = cast(tuple[CalibrationAnchor, ...], raw_anchors)
    identifiers = tuple(item.anchor_id for item in anchors)
    if len(identifiers) != len(set(identifiers)):
        raise ShadowCalibrationCommandError(f"{field_name} must not duplicate anchor IDs")
    return anchors


def _matches(value: object, field_name: str) -> tuple[CalibrationAnchorMatch, ...]:
    if type(value) is not tuple or not value:  # noqa: E721
        raise ShadowCalibrationCommandError(f"{field_name} must be a non-empty tuple")
    raw_matches = cast(tuple[object, ...], value)
    if any(type(item) is not CalibrationAnchorMatch for item in raw_matches):  # noqa: E721
        raise ShadowCalibrationCommandError(
            f"{field_name} must contain exact CalibrationAnchorMatch values"
        )
    matches = cast(tuple[CalibrationAnchorMatch, ...], raw_matches)
    return matches


@dataclass(frozen=True, slots=True)
class ShadowCalibrationInputs:
    """Frozen non-authority inputs; a future source compiler is the authority boundary."""

    profile_source_sha256: str
    registry_snapshot_sha256: str
    calibration_corpus_set_sha256: str
    native_port_identity_sha256: str
    word_gap_policy_sha256: str
    vad_merge_policy_sha256: str
    alignment_policy_sha256: str
    acceptance_policy_sha256: str
    asr_producer_id: str
    vad_producer_id: str
    source_clock_id: str
    source_time_base: TimeBase

    def __post_init__(self) -> None:
        for name in (
            "profile_source_sha256",
            "registry_snapshot_sha256",
            "calibration_corpus_set_sha256",
            "native_port_identity_sha256",
            "word_gap_policy_sha256",
            "vad_merge_policy_sha256",
            "alignment_policy_sha256",
            "acceptance_policy_sha256",
        ):
            _sha(getattr(self, name), f"shadow_inputs.{name}")
        _text(self.asr_producer_id, "shadow_inputs.asr_producer_id")
        _text(self.vad_producer_id, "shadow_inputs.vad_producer_id")
        if self.asr_producer_id == self.vad_producer_id:
            raise ShadowCalibrationCommandError("locked inputs require distinct ASR and VAD producers")
        _text(self.source_clock_id, "shadow_inputs.source_clock_id")
        _time_base(self.source_time_base, "shadow_inputs.source_time_base")

    def to_mapping(self) -> dict[str, object]:
        return {
            "acceptance_policy_sha256": self.acceptance_policy_sha256,
            "alignment_policy_sha256": self.alignment_policy_sha256,
            "asr_producer_id": self.asr_producer_id,
            "calibration_corpus_set_sha256": self.calibration_corpus_set_sha256,
            "native_port_identity_sha256": self.native_port_identity_sha256,
            "profile_source_sha256": self.profile_source_sha256,
            "registry_snapshot_sha256": self.registry_snapshot_sha256,
            "source_clock_id": self.source_clock_id,
            "source_time_base": _time_base_mapping(self.source_time_base),
            "vad_merge_policy_sha256": self.vad_merge_policy_sha256,
            "vad_producer_id": self.vad_producer_id,
            "word_gap_policy_sha256": self.word_gap_policy_sha256,
        }


@dataclass(frozen=True, slots=True)
class ShadowCalibrationCorpusMember:
    """One ordered locked corpus member with independently reviewed anchors."""

    corpus_member_reference_sha256: str
    expected_anchor_reference_sha256: str
    asr_anchors: tuple[CalibrationAnchor, ...]
    vad_anchors: tuple[CalibrationAnchor, ...]

    def __post_init__(self) -> None:
        _sha(self.corpus_member_reference_sha256, "corpus_member.corpus_member_reference_sha256")
        _sha(self.expected_anchor_reference_sha256, "corpus_member.expected_anchor_reference_sha256")
        _anchors(self.asr_anchors, "corpus_member.asr_anchors")
        _anchors(self.vad_anchors, "corpus_member.vad_anchors")


@dataclass(frozen=True, slots=True)
class MeasureShadowCalibrationRequest:
    """Closed administrative request; its slot and scope derive only from locked values."""

    shadow_inputs: ShadowCalibrationInputs
    corpus_members: tuple[ShadowCalibrationCorpusMember, ...]

    def __post_init__(self) -> None:
        if type(self.shadow_inputs) is not ShadowCalibrationInputs:  # noqa: E721
            raise ShadowCalibrationCommandError("request.shadow_inputs must be exact")
        if type(self.corpus_members) is not tuple or not self.corpus_members:  # noqa: E721
            raise ShadowCalibrationCommandError("request.corpus_members must be a non-empty tuple")
        if any(type(item) is not ShadowCalibrationCorpusMember for item in self.corpus_members):  # noqa: E721
            raise ShadowCalibrationCommandError("request.corpus_members must contain exact typed members")
        references = tuple(item.corpus_member_reference_sha256 for item in self.corpus_members)
        if len(references) != len(set(references)):
            raise ShadowCalibrationCommandError("request.corpus_members must not duplicate members")
        for member in self.corpus_members:
            self._validate_anchors(member.asr_anchors, CalibrationProducer.ASR, self.shadow_inputs.asr_producer_id)
            self._validate_anchors(member.vad_anchors, CalibrationProducer.VAD, self.shadow_inputs.vad_producer_id)

    def _validate_anchors(
        self,
        anchors: tuple[CalibrationAnchor, ...],
        producer: CalibrationProducer,
        producer_id: str,
    ) -> None:
        for anchor in anchors:
            if (
                anchor.producer is not producer
                or anchor.producer_id != producer_id
                or anchor.clock_id != self.shadow_inputs.source_clock_id
                or anchor.time_base != self.shadow_inputs.source_time_base
            ):
                raise ShadowCalibrationCommandError("corpus anchors do not bind locked producer clock")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "command": MEASURE_SHADOW_CALIBRATION_COMMAND,
            "corpus_members": [
                {
                    "asr_anchors": [_anchor_mapping(anchor) for anchor in member.asr_anchors],
                    "corpus_member_reference_sha256": member.corpus_member_reference_sha256,
                    "expected_anchor_reference_sha256": member.expected_anchor_reference_sha256,
                    "vad_anchors": [_anchor_mapping(anchor) for anchor in member.vad_anchors],
                }
                for member in self.corpus_members
            ],
            "shadow_inputs": self.shadow_inputs.to_mapping(),
            "measurement_protocol": SHADOW_CALIBRATION_MEASUREMENT_PROTOCOL,
        }

    @property
    def request_hash(self) -> str:
        return canonical_sha256(self.canonical_payload())

    @property
    def calibration_run_key(self) -> str:
        return self.request_hash.removeprefix("sha256:")

    @property
    def job(self) -> Job:
        return Job(self.calibration_run_key, "shadow")

    @property
    def idempotency_key(self) -> str:
        return f"shadow-calibration:{self.calibration_run_key}"

    @property
    def artifact_scope(self) -> ArtifactScope:
        return ArtifactScope("autocut_calibration", "shadow_run", self.calibration_run_key)


@dataclass(frozen=True, slots=True)
class ShadowCalibrationPortResult:
    """Typed direct-native observations for exactly one locked corpus member."""

    corpus_member_reference_sha256: str
    raw_native_response: bytes
    raw_native_response_media_type: ShadowCalibrationNativeResponseMediaType
    reported_native_identity_sha256: str
    coverage: ShadowCalibrationCoverage
    asr_matches: tuple[CalibrationAnchorMatch, ...]
    vad_matches: tuple[CalibrationAnchorMatch, ...]

    def __post_init__(self) -> None:
        _sha(self.corpus_member_reference_sha256, "port_result.corpus_member_reference_sha256")
        if type(self.raw_native_response) is not bytes or not self.raw_native_response:  # noqa: E721
            raise ShadowCalibrationCommandError("port_result.raw_native_response must be non-empty bytes")
        if type(self.raw_native_response_media_type) is not ShadowCalibrationNativeResponseMediaType:  # noqa: E721
            raise ShadowCalibrationCommandError("port_result.raw_native_response_media_type must be exact")
        _sha(self.reported_native_identity_sha256, "port_result.reported_native_identity_sha256")
        if type(self.coverage) is not ShadowCalibrationCoverage:  # noqa: E721
            raise ShadowCalibrationCommandError("port_result.coverage must be exact")
        _matches(self.asr_matches, "port_result.asr_matches")
        _matches(self.vad_matches, "port_result.vad_matches")


class ShadowCalibrationMeasurementPort(Protocol):
    """Native administrative port, invoked only after a durable command claim."""

    def measure(
        self,
        request: MeasureShadowCalibrationRequest,
        member: ShadowCalibrationCorpusMember,
    ) -> ShadowCalibrationPortResult: ...


class ShadowCalibrationMeasurementStore(Protocol):
    def claim_command(self, claim: CommandClaim) -> CommandOutcome: ...

    def put_immutable_blob(
        self,
        job: Job,
        *,
        content: bytes,
        content_hash: str,
        media_type: str,
    ) -> BlobRef: ...

    def commit_command_success(self, success: CommandSuccess) -> CommandOutcome: ...

    def commit_command_rejection(self, rejection: CommandRejection) -> CommandOutcome: ...


class MeasureShadowCalibrationCommand:
    """Collect and atomically commit the exact two-member shadow measurement set."""

    def __init__(
        self,
        store: ShadowCalibrationMeasurementStore,
        port: ShadowCalibrationMeasurementPort,
    ) -> None:
        self._store = store
        self._port = port

    def execute(self, request: MeasureShadowCalibrationRequest) -> CommandOutcome:
        if type(request) is not MeasureShadowCalibrationRequest:  # noqa: E721
            raise ShadowCalibrationCommandError("request must be an exact MeasureShadowCalibrationRequest")
        claimed = self._store.claim_command(
            CommandClaim(
                request.job,
                request.idempotency_key,
                MEASURE_SHADOW_CALIBRATION_COMMAND,
                request.request_hash,
            )
        )
        if not claimed.is_fresh_claim:
            return claimed
        try:
            results = tuple(
                (
                    result := self._validate_port_result(request, member, self._port.measure(request, member)),
                    self._persist_raw_native_response(request, result),
                )
                for member in request.corpus_members
            )
            artifacts = self._artifacts(request, results)
        except ShadowCalibrationProducerError as error:
            return self._reject(claimed, error.code.value)
        except (CalibrationRecordError, ShadowCalibrationCommandError):
            return self._reject(claimed, "SHADOW_CALIBRATION_INVALID")
        except ValueError:
            return self._reject(claimed, "SHADOW_CALIBRATION_MEASUREMENT_FAILED", outcome="failed")
        except Exception:
            return self._reject(
                claimed,
                "SHADOW_CALIBRATION_MEASUREMENT_FAILED",
                outcome="failed",
            )
        return self._store.commit_command_success(
            CommandSuccess(claimed.command_slot_id, _artifact_set_hash(artifacts), artifacts)
        )

    @staticmethod
    def _validate_port_result(
        request: MeasureShadowCalibrationRequest,
        member: ShadowCalibrationCorpusMember,
        result: ShadowCalibrationPortResult,
    ) -> ShadowCalibrationPortResult:
        if type(result) is not ShadowCalibrationPortResult:  # noqa: E721
            raise ShadowCalibrationCommandError("native port returned another result type")
        if result.coverage is not ShadowCalibrationCoverage.COMPLETE:
            raise ShadowCalibrationCommandError("native port returned incomplete calibration coverage")
        if result.corpus_member_reference_sha256 != member.corpus_member_reference_sha256:
            raise ShadowCalibrationCommandError("native port result does not bind the corpus member")
        if tuple(match.anchor for match in result.asr_matches) != member.asr_anchors:
            raise ShadowCalibrationCommandError("native ASR observations do not exactly match expected anchors")
        if tuple(match.anchor for match in result.vad_matches) != member.vad_anchors:
            raise ShadowCalibrationCommandError("native VAD observations do not exactly match expected anchors")
        inputs = request.shadow_inputs
        if result.reported_native_identity_sha256 != inputs.native_port_identity_sha256:
            raise ShadowCalibrationCommandError("native port identity does not match shadow inputs")
        for matches, producer, producer_id in (
            (result.asr_matches, CalibrationProducer.ASR, inputs.asr_producer_id),
            (result.vad_matches, CalibrationProducer.VAD, inputs.vad_producer_id),
        ):
            for match in matches:
                if (
                    match.anchor.producer is not producer
                    or match.anchor.producer_id != producer_id
                    or match.anchor.clock_id != inputs.source_clock_id
                    or match.anchor.time_base != inputs.source_time_base
                ):
                    raise ShadowCalibrationCommandError("native match does not bind locked producer clock")
        return result

    def _persist_raw_native_response(
        self,
        request: MeasureShadowCalibrationRequest,
        result: ShadowCalibrationPortResult,
    ) -> BlobRef:
        content_hash = _sha256_bytes(result.raw_native_response)
        blob = self._store.put_immutable_blob(
            request.job,
            content=result.raw_native_response,
            content_hash=content_hash,
            media_type=result.raw_native_response_media_type.value,
        )
        if (
            type(blob) is not BlobRef  # noqa: E721
            or blob.content_hash != content_hash
            or blob.byte_length != len(result.raw_native_response)
            or blob.media_type != result.raw_native_response_media_type.value
        ):
            raise ShadowCalibrationCommandError("stored native response blob does not bind raw response")
        return blob

    @staticmethod
    def _artifacts(
        request: MeasureShadowCalibrationRequest,
        results: tuple[tuple[ShadowCalibrationPortResult, BlobRef], ...],
    ) -> tuple[ArtifactMember, ArtifactMember]:
        inputs = request.shadow_inputs
        asr_matches = tuple(match for result, _ in results for match in result.asr_matches)
        vad_matches = tuple(match for result, _ in results for match in result.vad_matches)
        summary = CalibrationMeasurementSummary(
            ProducerCalibrationMeasurement(
                CalibrationProducer.ASR,
                inputs.asr_producer_id,
                "sensevoice-word-timestamp",
                inputs.source_clock_id,
                inputs.source_time_base,
                asr_matches,
                max(match.absolute_tick for match in asr_matches),
            ),
            ProducerCalibrationMeasurement(
                CalibrationProducer.VAD,
                inputs.vad_producer_id,
                "fsmn-vad-direct",
                inputs.source_clock_id,
                inputs.source_time_base,
                vad_matches,
                max(match.absolute_tick for match in vad_matches),
            ),
        )
        manifest_payload = {
            "alignment_policy_sha256": inputs.alignment_policy_sha256,
            "acceptance_policy_sha256": inputs.acceptance_policy_sha256,
            "calibration_corpus_set_sha256": inputs.calibration_corpus_set_sha256,
            "measurement_request_sha256": request.request_hash,
            "native_port_identity_sha256": inputs.native_port_identity_sha256,
            "native_response_blobs": [
                {
                    "corpus_member_reference_sha256": member.corpus_member_reference_sha256,
                    "native_response_blob": _blob_mapping(blob),
                }
                for member, (_, blob) in zip(request.corpus_members, results, strict=True)
            ],
            "registry_snapshot_sha256": inputs.registry_snapshot_sha256,
            "schema_version": "shadow-calibration-measurement-manifest-v1",
            "shadow_profile_source_sha256": inputs.profile_source_sha256,
            "vad_merge_policy_sha256": inputs.vad_merge_policy_sha256,
            "word_gap_policy_sha256": inputs.word_gap_policy_sha256,
        }
        manifest = _artifact(
            request.artifact_scope,
            "calibration_measurement_manifest",
            "measurement-manifest",
            manifest_payload,
        )
        results_payload = {
            "measurement_manifest_sha256": manifest.content_hash,
            "members": [
                {
                    "asr_observation": [_match_mapping(match) for match in result.asr_matches],
                    "corpus_member_reference_sha256": member.corpus_member_reference_sha256,
                    "expected_anchor_reference_sha256": member.expected_anchor_reference_sha256,
                    "native_response_blob": _blob_mapping(blob),
                    "reported_native_identity_sha256": result.reported_native_identity_sha256,
                    "vad_observation": [_match_mapping(match) for match in result.vad_matches],
                }
                for member, (result, blob) in zip(request.corpus_members, results, strict=True)
            ],
            "per_producer_measurements": {
                "asr": _measurement_mapping(summary.asr),
                "vad": _measurement_mapping(summary.vad),
            },
            "schema_version": "shadow-calibration-measurement-results-v1",
        }
        results_artifact = _artifact(
            request.artifact_scope,
            "calibration_measurement_results",
            "measurement-results",
            results_payload,
        )
        return (manifest, results_artifact)

    def _reject(
        self,
        claimed: CommandOutcome,
        code: str,
        *,
        outcome: Literal["denied", "failed"] = "denied",
    ) -> CommandOutcome:
        return self._store.commit_command_rejection(
            CommandRejection(
                claimed.command_slot_id,
                code,
                _json({"reason": _safe_failure_detail(code), "stage": "shadow_calibration"}),
                outcome,
            )
        )


def _anchor_mapping(anchor: CalibrationAnchor) -> dict[str, object]:
    return {
        "anchor_id": anchor.anchor_id,
        "clock_id": anchor.clock_id,
        "expected_range": _range_mapping(anchor.expected_range.start_pts, anchor.expected_range.end_pts),
        "producer": anchor.producer.value,
        "producer_id": anchor.producer_id,
        "time_base": _time_base_mapping(anchor.time_base),
    }


def _match_mapping(match: CalibrationAnchorMatch) -> dict[str, object]:
    return {
        "absolute_tick": match.absolute_tick,
        "anchor": _anchor_mapping(match.anchor),
        "early_tick": match.early_tick,
        "late_tick": match.late_tick,
        "observation": {
            "inference_kind": match.observation.inference_kind,
            "observation_id": match.observation.observation_id,
            "observed_range": _range_mapping(
                match.observation.observed_range.start_pts,
                match.observation.observed_range.end_pts,
            ),
        },
    }


def _measurement_mapping(measurement: ProducerCalibrationMeasurement) -> dict[str, object]:
    return {
        "absolute_maximum_tick": measurement.absolute_maximum_tick,
        "accepted_bound_tick": measurement.accepted_bound_tick,
        "clock_id": measurement.clock_id,
        "early_maximum_tick": measurement.early_maximum_tick,
        "inference_kind": measurement.inference_kind,
        "late_maximum_tick": measurement.late_maximum_tick,
        "matched_anchor_count": len(measurement.matches),
        "producer": measurement.producer.value,
        "producer_id": measurement.producer_id,
        "time_base": _time_base_mapping(measurement.time_base),
    }


def _range_mapping(start_pts: int, end_pts: int) -> dict[str, int]:
    return {"end_pts": end_pts, "start_pts": start_pts}


def _time_base_mapping(time_base: TimeBase) -> dict[str, int]:
    return {"denominator": time_base.denominator, "numerator": time_base.numerator}


def _blob_mapping(blob: BlobRef) -> dict[str, object]:
    return {
        "byte_length": blob.byte_length,
        "content_hash": blob.content_hash,
        "media_type": blob.media_type,
        "object_id": str(blob.object_id),
    }


def _artifact(
    scope: ArtifactScope,
    artifact_type: str,
    logical_id: str,
    payload: Mapping[str, object],
) -> ArtifactMember:
    payload_json = _json(payload)
    return ArtifactMember(
        artifact_type,
        logical_id,
        1,
        scope,
        canonical_payload_hash(payload_json),
        payload_json,
    )


def _artifact_set_hash(artifacts: tuple[ArtifactMember, ...]) -> str:
    members = [
        {
            "artifact_type": item.artifact_type,
            "content_hash": item.content_hash,
            "logical_id": item.logical_id,
            "payload_json": json.loads(item.payload_json),
            "revision": item.revision,
            "scope": {
                "key": item.scope.key,
                "kind": item.scope.kind,
                "namespace": item.scope.namespace,
            },
        }
        for item in artifacts
    ]
    return "sha256:" + hashlib.sha256(_json(members).encode()).hexdigest()


def _json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sha256_bytes(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _safe_failure_detail(code: str) -> str:
    details = {
        "SHADOW_CALIBRATION_INVALID": "shadow calibration evidence was rejected",
        "SHADOW_CALIBRATION_MEASUREMENT_FAILED": "shadow calibration measurement failed",
        ShadowCalibrationProducerFailureCode.REJECTED.value: "native calibration measurement was rejected",
        ShadowCalibrationProducerFailureCode.UNAVAILABLE.value: "native calibration measurement was unavailable",
    }
    try:
        return details[code]
    except KeyError as error:
        raise ShadowCalibrationCommandError("shadow calibration failure code is not allowlisted") from error
