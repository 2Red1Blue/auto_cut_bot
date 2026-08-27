"""Exact reusable timed-media evidence identities.

This module deliberately separates equivalence from ownership.  A requirement
contains only inputs capable of changing evidence.  An index entry then binds
that requirement to one exact immutable, succeeded child closure.  Neither
value searches a logical head or mutates historical Artifact state.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from ..media.types import canonical_sha256, sha256_prefixed
from ..store.models import CommittedArtifactMemberReference, Job, canonical_recipe_scope

EVIDENCE_REQUIREMENT_SCHEMA = "whole-episode-evidence-requirement-v1"
EVIDENCE_INDEX_ENTRY_SCHEMA = "whole-episode-evidence-index-entry-v1"
EVIDENCE_INDEX_SCHEMA = "whole-episode-evidence-index-v1"

_ZERO_SHA256 = "sha256:" + "0" * 64
_TIMED_MEDIA_MEMBER_LAYOUT = (
    ("root_media_evidence_bundle", "root_media_evidence"),
    ("candidate_timed_evidence_index", "candidate_timed_evidence"),
    ("timed_speech_profile_admission", "timed_speech_profile_admission"),
    ("presentation_timeline_probe", "presentation_timeline_probe"),
    ("committed_video_to_audio_clock_map_certificate", "video_to_audio_clock_map"),
)


class EvidenceIndexError(ValueError):
    """A reusable evidence requirement or exact child closure is invalid."""


def _sha(value: object, field_name: str) -> str:
    try:
        digest = sha256_prefixed(value, field_name)
    except ValueError as error:
        raise EvidenceIndexError(f"{field_name} must be a SHA-256 identity") from error
    if digest == _ZERO_SHA256:
        raise EvidenceIndexError(f"{field_name} must not be zero")
    return digest


def _text(value: object, field_name: str) -> str:
    if type(value) is not str or not value or value != value.strip():  # noqa: E721
        raise EvidenceIndexError(f"{field_name} must be canonical non-empty text")
    return value


@dataclass(frozen=True, slots=True)
class EvidenceRequirement:
    """Canonical semantic inputs for exactly one episode's timed evidence.

    Operational ownership (Job, Receipt, Blob object ID, staging limits and
    idempotency key) is deliberately absent.  It is carried by
    :class:`EvidenceIndexEntry`, where it can be independently reread.
    """

    episode_index: int
    source_content_sha256: str
    window_manifest_sha256: str
    proxy_timeline_map_sha256: str
    semantic_pack_sha256: str
    physical_timeline_inputs_sha256: str
    physical_detector_policy_sha256: str
    adaptive_plan_policy_sha256: str
    timed_speech_profile_sha256: str
    runtime_calibration_capability_sha256: str
    authority_registry_snapshot_sha256: str
    strategy_version: str

    def __post_init__(self) -> None:
        if type(self.episode_index) is not int or self.episode_index < 0:  # noqa: E721
            raise EvidenceIndexError("episode_index must be a non-negative integer")
        for field_name in (
            "source_content_sha256",
            "window_manifest_sha256",
            "proxy_timeline_map_sha256",
            "semantic_pack_sha256",
            "physical_timeline_inputs_sha256",
            "physical_detector_policy_sha256",
            "adaptive_plan_policy_sha256",
            "timed_speech_profile_sha256",
            "runtime_calibration_capability_sha256",
            "authority_registry_snapshot_sha256",
        ):
            _sha(getattr(self, field_name), field_name)
        _text(self.strategy_version, "strategy_version")

    def to_mapping(self) -> dict[str, object]:
        return {
            "adaptive_plan_policy_sha256": self.adaptive_plan_policy_sha256,
            "authority_registry_snapshot_sha256": self.authority_registry_snapshot_sha256,
            "episode_index": self.episode_index,
            "physical_detector_policy_sha256": self.physical_detector_policy_sha256,
            "physical_timeline_inputs_sha256": self.physical_timeline_inputs_sha256,
            "proxy_timeline_map_sha256": self.proxy_timeline_map_sha256,
            "runtime_calibration_capability_sha256": self.runtime_calibration_capability_sha256,
            "schema_version": EVIDENCE_REQUIREMENT_SCHEMA,
            "semantic_pack_sha256": self.semantic_pack_sha256,
            "source_content_sha256": self.source_content_sha256,
            "strategy_version": self.strategy_version,
            "timed_speech_profile_sha256": self.timed_speech_profile_sha256,
            "window_manifest_sha256": self.window_manifest_sha256,
        }

    @property
    def fingerprint_sha256(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class EvidenceIndexEntry:
    """One exact succeeded five-member evidence child selected for reuse."""

    requirement: EvidenceRequirement
    origin_job: Job
    command_slot_id: UUID
    receipt_id: UUID
    artifact_set_id: UUID
    request_hash: str
    set_hash: str
    members: tuple[CommittedArtifactMemberReference, ...]

    def __post_init__(self) -> None:
        if type(self.requirement) is not EvidenceRequirement:  # noqa: E721
            raise EvidenceIndexError("entry requires an exact EvidenceRequirement")
        if type(self.origin_job) is not Job:  # noqa: E721
            raise EvidenceIndexError("entry origin_job must be exact")
        if any(type(value) is not UUID for value in (
            self.command_slot_id, self.receipt_id, self.artifact_set_id,
        )):
            raise EvidenceIndexError("entry command, Receipt and ArtifactSet IDs must be UUIDs")
        _sha(self.request_hash, "request_hash")
        _sha(self.set_hash, "set_hash")
        if (
            type(self.members) is not tuple
            or len(self.members) != len(_TIMED_MEDIA_MEMBER_LAYOUT)
            or any(type(member) is not CommittedArtifactMemberReference for member in self.members)
        ):
            raise EvidenceIndexError("entry requires the exact five-member timed-media closure")
        expected_scope = canonical_recipe_scope(self.origin_job)
        episode = self.requirement.episode_index
        for ordinal, (member, (artifact_type, prefix)) in enumerate(
            zip(self.members, _TIMED_MEDIA_MEMBER_LAYOUT, strict=True)
        ):
            if (
                member.receipt_id != self.receipt_id
                or member.artifact_set_id != self.artifact_set_id
                or member.member_ordinal != ordinal
                or member.scope != expected_scope
                or member.artifact_type != artifact_type
                or member.logical_id != f"{prefix}_episode_{episode:04d}"
            ):
                raise EvidenceIndexError("entry member does not match its exact origin child closure")

    def to_mapping(self) -> dict[str, object]:
        return {
            "artifact_set_id": str(self.artifact_set_id),
            "command_slot_id": str(self.command_slot_id),
            "members": [item.to_mapping() for item in self.members],
            "origin_job": {"job_key": self.origin_job.job_key, "profile": self.origin_job.profile},
            "receipt_id": str(self.receipt_id),
            "requirement": self.requirement.to_mapping(),
            "requirement_fingerprint_sha256": self.requirement.fingerprint_sha256,
            "request_hash": self.request_hash,
            "schema_version": EVIDENCE_INDEX_ENTRY_SCHEMA,
            "set_hash": self.set_hash,
        }


@dataclass(frozen=True, slots=True)
class EvidenceIndex:
    """One complete, ordered target selection; no omission or alternatives."""

    entries: tuple[EvidenceIndexEntry, ...]

    def __post_init__(self) -> None:
        if type(self.entries) is not tuple or not self.entries:  # noqa: E721
            raise EvidenceIndexError("evidence index entries must be a non-empty tuple")
        if any(type(entry) is not EvidenceIndexEntry for entry in self.entries):
            raise EvidenceIndexError("evidence index entries must be exact")
        indexes = tuple(entry.requirement.episode_index for entry in self.entries)
        if indexes != tuple(range(len(self.entries))):
            raise EvidenceIndexError("evidence index must cover target episode indexes in exact order")
        handles = tuple(
            (entry.origin_job, entry.command_slot_id, entry.receipt_id, entry.artifact_set_id)
            for entry in self.entries
        )
        if len(handles) != len(set(handles)):
            raise EvidenceIndexError("evidence index cannot select the same child closure twice")

    def to_mapping(self) -> dict[str, object]:
        return {
            "entries": [entry.to_mapping() for entry in self.entries],
            "schema_version": EVIDENCE_INDEX_SCHEMA,
        }

    @property
    def content_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


__all__ = (
    "EVIDENCE_INDEX_ENTRY_SCHEMA",
    "EVIDENCE_INDEX_SCHEMA",
    "EVIDENCE_REQUIREMENT_SCHEMA",
    "EvidenceIndex",
    "EvidenceIndexEntry",
    "EvidenceIndexError",
    "EvidenceRequirement",
)
