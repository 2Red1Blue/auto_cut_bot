"""Physical-only prelude values; no speech, Store authority or edit admission."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol, cast

from ..media.physical_root import PhysicalRootMediaEvidence
from ..media.root_evidence_codec import decode_media_evidence_json
from ..media.timed_evidence import CalibrationBinding
from ..media.types import canonical_sha256, sha256_prefixed
from ..store.models import BlobRef, Job, VerifiedMaterializedBlob
from .prepare_timed_media_evidence_command import (
    PrepareTimedMediaEvidenceRequest,
    ResolvedPrepareTimedMediaEvidenceRequest,
)

PHYSICAL_EVIDENCE_STRATEGY_VERSION = "physical-prelude-v1"
PHYSICAL_PROVENANCE_SCHEMA = "local-physical-producer-provenance-v1"
PREPARE_PHYSICAL_MEDIA_EVIDENCE_COMMAND = "PreparePhysicalMediaEvidence@2.1.3"
PHYSICAL_KIND_ORDER = ("frame", "audio", "shot", "scene", "visual", "subtitle")
PHYSICAL_ROOT_MEDIA_TYPE = "application/vnd.autocut.physical-root-media-evidence+json"


class PhysicalMediaEvidenceCommandError(ValueError):
    """A physical prelude request or measured result fails closed validation."""


def physical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)


def positive_limit(value: object, name: str) -> int:
    if type(value) is not int or not 0 < value <= 2**53 - 1:
        raise PhysicalMediaEvidenceCommandError(f"{name} must be a positive safe integer")
    return value


def physical_object(value: object, fields: tuple[str, ...] | None = None) -> dict[str, object]:
    if type(value) is not dict or (fields is not None and set(cast(dict[object, object], value)) != set(fields)):
        raise PhysicalMediaEvidenceCommandError("physical object has missing or unknown fields")
    return cast(dict[str, object], value)


def physical_array(value: object) -> list[object]:
    if type(value) is not list:
        raise PhysicalMediaEvidenceCommandError("physical collection must be an array")
    return cast(list[object], value)


def _text(value: object) -> None:
    if type(value) is not str or not value.strip():
        raise PhysicalMediaEvidenceCommandError("physical identifier must be nonempty text")
    value.encode("utf-8")


def _hash(value: object) -> None:
    if type(value) is not str or value == "sha256:" + "0" * 64:
        raise PhysicalMediaEvidenceCommandError("physical hash must be text")
    sha256_prefixed(value, "physical identity")


def canonical_physical_mapping(raw: str) -> dict[str, object]:
    if type(raw) is not str or not raw:
        raise PhysicalMediaEvidenceCommandError("physical metadata must be nonempty canonical JSON")
    encoded = raw.encode("utf-8")
    value = physical_object(decode_media_evidence_json(encoded, max_bytes=len(encoded)))
    if physical_json(value) != raw:
        raise PhysicalMediaEvidenceCommandError("physical metadata must be canonical JSON")
    return value


@dataclass(frozen=True, slots=True)
class PreparePhysicalMediaEvidenceRequest:
    parent: PrepareTimedMediaEvidenceRequest
    physical_policy_sha256: str
    max_evidence_bytes: int
    max_metadata_bytes: int

    def __post_init__(self) -> None:
        if type(self.parent) is not PrepareTimedMediaEvidenceRequest:
            raise PhysicalMediaEvidenceCommandError("physical prelude requires exact Source/VLM handles")
        _hash(self.physical_policy_sha256)
        positive_limit(self.max_evidence_bytes, "max_evidence_bytes")
        positive_limit(self.max_metadata_bytes, "max_metadata_bytes")

    def canonical_payload(self) -> dict[str, object]:
        return {"strategy_version": PHYSICAL_EVIDENCE_STRATEGY_VERSION,
                "parent": self.parent.canonical_payload(),
                "physical_policy_sha256": self.physical_policy_sha256,
                "max_evidence_bytes": self.max_evidence_bytes, "max_metadata_bytes": self.max_metadata_bytes}

    @property
    def request_hash(self) -> str:
        return canonical_sha256(self.canonical_payload())


@dataclass(frozen=True, slots=True)
class ResolvedPreparePhysicalMediaEvidenceRequest:
    request: PreparePhysicalMediaEvidenceRequest
    source: ResolvedPrepareTimedMediaEvidenceRequest

    def __post_init__(self) -> None:
        if (type(self.request) is not PreparePhysicalMediaEvidenceRequest
                or type(self.source) is not ResolvedPrepareTimedMediaEvidenceRequest
                or self.source.request != self.request.parent):
            raise PhysicalMediaEvidenceCommandError("resolved physical Source differs from exact parent")

    def canonical_payload(self) -> dict[str, object]:
        return {"domain": PHYSICAL_EVIDENCE_STRATEGY_VERSION,
                "request": self.request.canonical_payload(), "source": self.source.canonical_payload()}

    @property
    def request_hash(self) -> str:
        return canonical_sha256(self.canonical_payload())

    @property
    def idempotency_key(self) -> str:
        return f"physical-prelude:{self.request_hash}"

    @property
    def root_input_manifest_sha256(self) -> str:
        return canonical_sha256({"domain": "physical-prelude-v1/root-input", "request_sha256": self.request_hash})

    @property
    def physical_root_id(self) -> str:
        return f"physical-root:{self.request_hash[7:]}"

    @property
    def job(self) -> Job:
        return self.source.job

    @property
    def source_blob(self) -> BlobRef:
        return self.source.source_blob

    @property
    def source_manifest_sha256(self) -> str:
        return self.source.source_manifest_sha256

    @property
    def source_provenance_sha256(self) -> str:
        return self.source.source_provenance_sha256

    @property
    def physical_policy_sha256(self) -> str:
        return self.request.physical_policy_sha256

    @property
    def max_evidence_bytes(self) -> int:
        return self.request.max_evidence_bytes

    @property
    def max_metadata_bytes(self) -> int:
        return self.request.max_metadata_bytes


@dataclass(frozen=True, slots=True)
class ProducedPhysicalMediaEvidence:
    physical_root: PhysicalRootMediaEvidence
    calibration_bindings: tuple[CalibrationBinding, ...]
    producer_policy_json: str
    producer_provenance_json: str

    def __post_init__(self) -> None:
        if type(self.physical_root) is not PhysicalRootMediaEvidence:
            raise PhysicalMediaEvidenceCommandError("physical root must be exact")
        if (type(self.calibration_bindings) is not tuple or len(self.calibration_bindings) != 6
                or any(type(item) is not CalibrationBinding for item in self.calibration_bindings)):
            raise PhysicalMediaEvidenceCommandError("physical output requires exactly six typed calibrations")
        canonical_physical_mapping(self.producer_policy_json)
        provenance = physical_object(canonical_physical_mapping(self.producer_provenance_json), (
            "schema_version", "source_provenance_sha256", "producer_identities", "tool_invocations", "tool_trace_sha256",
        ))
        if provenance["schema_version"] != PHYSICAL_PROVENANCE_SCHEMA:
            raise PhysicalMediaEvidenceCommandError("physical provenance schema differs")
        _hash(provenance["source_provenance_sha256"])
        _hash(provenance["tool_trace_sha256"])
        identities = physical_array(provenance["producer_identities"])
        if len(identities) != 6:
            raise PhysicalMediaEvidenceCommandError("physical identities must contain exactly six roles")
        for kind, value in zip(PHYSICAL_KIND_ORDER, identities, strict=True):
            identity = physical_object(value, (
                "producer_kind", "producer_id", "producer_version", "producer_policy_sha256",
                "detector_sha256", "calibration_policy_sha256", "calibration_record_sha256",
                "timing_error_bound_tick", "adapter_sha256",
            ))
            if identity["producer_kind"] != kind:
                raise PhysicalMediaEvidenceCommandError("physical identity role order differs")
            _text(identity["producer_id"])
            _text(identity["producer_version"])
            positive_limit(identity["timing_error_bound_tick"], "physical timing bound")
            for key in ("producer_policy_sha256", "detector_sha256", "calibration_policy_sha256", "calibration_record_sha256"):
                _hash(identity[key])
            if identity["adapter_sha256"] is not None:
                _hash(identity["adapter_sha256"])
        invocations = physical_array(provenance["tool_invocations"])
        if not invocations or canonical_sha256(invocations) != provenance["tool_trace_sha256"]:
            raise PhysicalMediaEvidenceCommandError("physical tool trace hash differs or is empty")
        for value in invocations:
            trace = physical_object(value, ("producer_kind", "executable", "executable_sha256",
                                           "version_evidence_sha256", "argv_sha256", "stdout_sha256", "stderr_sha256"))
            _text(trace["producer_kind"])
            _text(trace["executable"])
            for key in ("executable_sha256", "version_evidence_sha256", "argv_sha256", "stdout_sha256", "stderr_sha256"):
                _hash(trace[key])

    @property
    def producer_policy_sha256(self) -> str:
        return canonical_sha256(self.policy_mapping())

    @property
    def producer_provenance_sha256(self) -> str:
        return canonical_sha256(canonical_physical_mapping(self.producer_provenance_json))

    def policy_mapping(self) -> dict[str, object]:
        return canonical_physical_mapping(self.producer_policy_json)


class PhysicalMediaEvidenceProducerPort(Protocol):
    def prepare(self, request: ResolvedPreparePhysicalMediaEvidenceRequest,
                source: VerifiedMaterializedBlob) -> ProducedPhysicalMediaEvidence: ...
