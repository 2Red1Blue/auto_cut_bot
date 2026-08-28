"""Durable, deterministic projection of an external narrative snapshot.

This is deliberately a separate command from VLM generation.  It snapshots
the HTTP result once, writes raw bytes as a Blob, then commits only normalized
and bounded artifacts.  Downstream VLM requests read the committed PackSet;
they never fetch the external service themselves.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol, cast
from uuid import UUID

from autocut_kernel.context_pack import (
    ContextSelectionPolicy,
    EpisodeContextBinding,
    EpisodeContextBindingSet,
    OwnerEpisodeMapSet,
    WindowContextPack,
    build_window_context_pack,
    normalize_narrative_context,
    video_only_window_context_pack,
)
from autocut_kernel.store import (
    ArtifactMember,
    ArtifactScope,
    BlobRef,
    CommandClaim,
    CommandOutcome,
    CommandRejection,
    CommandSuccess,
    Job,
)
from autocut_kernel.store.models import (
    PersistedCommittedArtifactSet,
    artifact_set_hash,
    canonical_payload_hash,
)

from auto_cut_bot.pipeline.source_prep import PersistedPreparedSources

from .api import ExternalNarrativeApiClient

COMMAND_NAME = "PrepareWindowContextCommand"
CONTEXT_PACK_SET_ARTIFACT_TYPE = "window_context_pack_set"
CONTEXT_PACK_SET_LOGICAL_ID = "window_context_pack_set"
_JSON_MEDIA_TYPE = "application/json"


class ContextPrepareStore(Protocol):
    def read_outcome(self, job: Job, idempotency_key: str) -> CommandOutcome | None: ...

    def claim_command(self, claim: CommandClaim) -> CommandOutcome: ...

    def put_immutable_blob(
        self, job: Job, *, content: bytes, content_hash: str, media_type: str,
    ) -> BlobRef: ...

    def commit_command_success(self, success: CommandSuccess) -> CommandOutcome: ...

    def commit_command_rejection(self, rejection: CommandRejection) -> CommandOutcome: ...

    def read_committed_artifact_set(
        self,
        job: Job,
        *,
        command_slot_id: UUID,
        receipt_id: UUID,
        artifact_set_id: UUID,
        expected_request_hash: str,
        expected_command_name: str,
        expected_execution_kind: str,
    ) -> PersistedCommittedArtifactSet: ...


@dataclass(frozen=True, slots=True)
class PrepareWindowContextRequest:
    job: Job
    idempotency_key: str
    artifact_scope: ArtifactScope
    artifact_revision: int
    source_bundle: PersistedPreparedSources
    owner_maps: OwnerEpisodeMapSet
    selection_policy: ContextSelectionPolicy

    def __post_init__(self) -> None:
        if type(self.job) is not Job or type(self.artifact_scope) is not ArtifactScope:  # noqa: E721
            raise TypeError("context request requires exact Kernel job and scope")
        if type(self.idempotency_key) is not str or not self.idempotency_key.strip():  # noqa: E721
            raise ValueError("context idempotency key must be non-empty")
        if type(self.artifact_revision) is not int or self.artifact_revision < 1:  # noqa: E721
            raise ValueError("context artifact revision must be positive")
        if type(self.source_bundle) is not PersistedPreparedSources:  # noqa: E721
            raise TypeError("context request requires exact persisted sources")
        if type(self.owner_maps) is not OwnerEpisodeMapSet:  # noqa: E721
            raise TypeError("context request requires exact owner episode maps")
        if type(self.selection_policy) is not ContextSelectionPolicy:  # noqa: E721
            raise TypeError("context request requires exact selection policy")

    @property
    def request_hash(self) -> str:
        from autocut_kernel.media.types import canonical_sha256

        return canonical_sha256({
            "artifact_revision": self.artifact_revision,
            "artifact_scope": _scope_mapping(self.artifact_scope),
            "command": COMMAND_NAME,
            "job": {"job_key": self.job.job_key, "profile": self.job.profile},
            "owner_maps_sha256": self.owner_maps.canonical_hash,
            "selection_policy_sha256": self.selection_policy.canonical_hash,
            "source_provenance_sha256": self.source_bundle.canonical_hash,
        })


@dataclass(frozen=True, slots=True)
class PreparedWindowContextSet:
    """Pack for every committed source episode; absent maps become video-only."""

    packs: tuple[WindowContextPack, ...]
    source_provenance_sha256: str
    owner_maps_sha256: str
    snapshot: dict[str, object] | None
    raw_snapshot_blob: BlobRef | None
    normalized_context: dict[str, object] | None
    binding_set: EpisodeContextBindingSet | None
    diagnostic_code: str | None

    def __post_init__(self) -> None:
        if not self.packs or any(type(pack) is not WindowContextPack for pack in self.packs):  # noqa: E721
            raise ValueError("context pack set must contain exact packs")
        if self.raw_snapshot_blob is not None and self.snapshot is None:
            raise ValueError("raw snapshot blob requires snapshot identity")
        if self.binding_set is not None and self.normalized_context is None:
            raise ValueError("binding set requires normalized context")

    def pack_for_episode(self, episode_index: int) -> WindowContextPack:
        if type(episode_index) is not int or not 0 <= episode_index < len(self.packs):  # noqa: E721
            raise ValueError("context pack episode index is out of range")
        return self.packs[episode_index]

    def to_mapping(self) -> dict[str, object]:
        return {
            "binding_set": None if self.binding_set is None else self.binding_set.to_mapping(),
            "diagnostic_code": self.diagnostic_code,
            "normalized_context": self.normalized_context,
            "owner_maps_sha256": self.owner_maps_sha256,
            "packs": [pack.to_mapping() for pack in self.packs],
            "raw_snapshot_blob": None if self.raw_snapshot_blob is None else _blob_mapping(self.raw_snapshot_blob),
            "snapshot": self.snapshot,
            "source_provenance_sha256": self.source_provenance_sha256,
        }


@dataclass(frozen=True, slots=True)
class PrepareWindowContextResult:
    outcome: CommandOutcome
    prepared: PreparedWindowContextSet | None = None


def read_committed_window_context_packs(
    store: ContextPrepareStore,
    request: PrepareWindowContextRequest,
    outcome: CommandOutcome,
) -> tuple[WindowContextPack, ...]:
    """Read exact PackSet replay input; never refetch or consult a head."""
    if (
        type(outcome) is not CommandOutcome  # noqa: E721
        or outcome.state != "succeeded"
        or outcome.receipt_id is None
        or outcome.artifact_set_id is None
    ):
        raise ValueError("context pack reader requires an exact succeeded receipt")
    record = store.read_committed_artifact_set(
        request.job,
        command_slot_id=outcome.command_slot_id,
        receipt_id=outcome.receipt_id,
        artifact_set_id=outcome.artifact_set_id,
        expected_request_hash=request.request_hash,
        expected_command_name=COMMAND_NAME,
        expected_execution_kind="deterministic",
    )
    members = record.artifacts
    matching = tuple(
        member for member in members
        if (
            member.artifact_type == CONTEXT_PACK_SET_ARTIFACT_TYPE
            and member.logical_id == CONTEXT_PACK_SET_LOGICAL_ID
            and member.scope == request.artifact_scope
            and member.revision == request.artifact_revision
        )
    )
    if len(matching) != 1:
        raise ValueError("committed context pack set member is missing or ambiguous")
    try:
        decoded = json.loads(matching[0].payload_json)
    except (TypeError, ValueError) as error:
        raise ValueError("committed context pack set JSON is invalid") from error
    if type(decoded) is not dict:  # noqa: E721
        raise ValueError("committed context pack set must be an object")
    payload = cast(dict[str, object], decoded)
    if type(payload) is not dict or payload.get("source_provenance_sha256") != request.source_bundle.canonical_hash:
        raise ValueError("committed context pack provenance does not match source preparation")
    if payload.get("owner_maps_sha256") != request.owner_maps.canonical_hash:
        raise ValueError("committed context pack owner map differs")
    values = payload.get("packs")
    if type(values) is not list:  # noqa: E721
        raise ValueError("committed context pack membership differs from source episodes")
    pack_values = cast(list[object], values)
    if len(pack_values) != len(request.source_bundle.prepared.episodes):
        raise ValueError("committed context pack membership differs from source episodes")
    packs = tuple(WindowContextPack.from_mapping(item) for item in pack_values)
    return packs


class PrepareWindowContextCommand:
    def __init__(self, store: ContextPrepareStore, client: ExternalNarrativeApiClient) -> None:
        self._store = store
        self._client = client

    def execute(self, request: PrepareWindowContextRequest) -> PrepareWindowContextResult:
        existing = self._store.read_outcome(request.job, request.idempotency_key)
        if existing is not None and existing.state not in ("pending", "running"):
            # A generic replay reader will be used by downstream consumers.  A
            # new fetch here would violate the snapshot boundary.
            return PrepareWindowContextResult(existing)
        claimed = self._store.claim_command(CommandClaim(
            request.job, request.idempotency_key, COMMAND_NAME, request.request_hash,
            execution_kind="deterministic",
        ))
        if claimed.state not in ("pending", "running"):
            return PrepareWindowContextResult(claimed)
        try:
            fetched = self._client.fetch(request.owner_maps.series_external_id)
            raw_blob = self._store.put_immutable_blob(
                request.job,
                content=fetched.raw_payload,
                content_hash=fetched.snapshot.raw_payload_sha256,
                media_type=_JSON_MEDIA_TYPE,
            )
            normalized = normalize_narrative_context(
                fetched.snapshot,
                asset_response=fetched.asset_response,
                episode_response=fetched.episode_response,
            )
            maps_by_local = {
                (item.local_relative_path, item.local_episode_index): item
                for item in request.owner_maps.mappings
            }
            bindings: list[EpisodeContextBinding] = []
            packs: list[WindowContextPack] = []
            for episode_index, episode in enumerate(request.source_bundle.prepared.episodes):
                manifest = episode.manifest
                source = request.source_bundle.prepared.census.sources[episode_index]
                if (
                    manifest.source_id != source.source_id
                    or manifest.source_sha256 != source.content_sha256
                ):
                    raise ValueError("committed source episode does not match source census")
                owner_map = maps_by_local.get((source.relative_path, episode_index))
                if owner_map is None:
                    packs.append(video_only_window_context_pack(
                        request.selection_policy, "EXTERNAL_EPISODE_BINDING_MISSING"
                    ))
                    continue
                binding = owner_map.bind(
                    local_source_id=manifest.source_id,
                    local_source_sha256=manifest.source_sha256,
                )
                bindings.append(binding)
                packs.append(build_window_context_pack(
                    normalized,
                    binding,
                    local_source_id=manifest.source_id,
                    local_source_sha256=manifest.source_sha256,
                    local_episode_index=episode_index,
                    policy=request.selection_policy,
                ))
            binding_set = EpisodeContextBindingSet(
                request.owner_maps.series_external_id,
                tuple(sorted(bindings, key=lambda item: item.canonical_hash)),
            ) if bindings else None
            prepared = PreparedWindowContextSet(
                packs=tuple(packs),
                source_provenance_sha256=request.source_bundle.canonical_hash,
                owner_maps_sha256=request.owner_maps.canonical_hash,
                snapshot=fetched.snapshot.to_mapping(),
                raw_snapshot_blob=raw_blob,
                normalized_context=normalized.to_mapping(),
                binding_set=binding_set,
                diagnostic_code=None,
            )
        except Exception as error:
            prepared = PreparedWindowContextSet(
                packs=tuple(video_only_window_context_pack(
                    request.selection_policy, "EXTERNAL_CONTEXT_UNAVAILABLE"
                ) for _episode in request.source_bundle.prepared.episodes),
                source_provenance_sha256=request.source_bundle.canonical_hash,
                owner_maps_sha256=request.owner_maps.canonical_hash,
                snapshot=None,
                raw_snapshot_blob=None,
                normalized_context=None,
                binding_set=None,
                diagnostic_code=_diagnostic_code(error),
            )
        artifacts = (_artifact(request, prepared),)
        outcome = self._store.commit_command_success(CommandSuccess(
            claimed.command_slot_id, artifact_set_hash(artifacts), artifacts,
        ))
        return PrepareWindowContextResult(outcome, prepared)


def _artifact(request: PrepareWindowContextRequest, prepared: PreparedWindowContextSet) -> ArtifactMember:
    payload_json = json.dumps(prepared.to_mapping(), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return ArtifactMember(
        CONTEXT_PACK_SET_ARTIFACT_TYPE,
        CONTEXT_PACK_SET_LOGICAL_ID,
        request.artifact_revision,
        request.artifact_scope,
        canonical_payload_hash(payload_json),
        payload_json,
    )


def _scope_mapping(scope: ArtifactScope) -> dict[str, str]:
    return {"namespace": scope.namespace, "kind": scope.kind, "key": scope.key}


def _blob_mapping(blob: BlobRef) -> dict[str, object]:
    return {
        "object_id": str(blob.object_id),
        "content_hash": blob.content_hash,
        "byte_length": blob.byte_length,
        "media_type": blob.media_type,
    }


def _diagnostic_code(error: Exception) -> str:
    # Do not persist remote response text, Authorization, or accidental URLs in
    # an Artifact.  The raw response is either the immutable snapshot Blob or
    # unavailable; this code is all downstream policy needs.
    if isinstance(error, (ValueError, TypeError)):
        return "EXTERNAL_CONTEXT_INVALID"
    return "EXTERNAL_CONTEXT_FETCH_FAILED"


__all__ = [
    "COMMAND_NAME",
    "CONTEXT_PACK_SET_ARTIFACT_TYPE",
    "CONTEXT_PACK_SET_LOGICAL_ID",
    "ContextPrepareStore",
    "PrepareWindowContextCommand",
    "PrepareWindowContextRequest",
    "PrepareWindowContextResult",
    "PreparedWindowContextSet",
    "read_committed_window_context_packs",
]
