"""Identity source windows and durable whole-series preparation command."""

# pyright: reportMissingTypeStubs=false

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID

from autocut_kernel.media import (
    FramePtsIndexSet,
)
from autocut_kernel.media.types import canonical_sha256
from autocut_kernel.source_manifest import (
    IDENTITY_FRAME_GENERATION_POLICY_SHA256,
    SourceManifestDecodeError,
    decode_source_manifest,
    identity_frame_index,
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
    PersistedWholeSeriesSourceManifest,
    RuntimeStoreError,
    WholeSeriesSourceManifestReference,
)
from autocut_kernel.store.models import canonical_payload_hash
from autocut_kernel.vlm import (
    WindowFrameSample,
    WindowManifest,
    WindowManifestSet,
    WindowProxyBlobRef,
)
from autocut_kernel.vlm.window import ProxyTimelineMap

from .census import snapshot_series_sources
from .models import AuthorizedSeriesSourceRoot, SeriesCensusError, SeriesSourceCensus
from .probe import (
    FFprobeSourceMediaPort,
    SourceMediaProbe,
)

_COMMAND_NAME = "PrepareWholeSeriesSourcesCommand"
_SHOWINFO_PTS = re.compile(rb"\bpts:\s*(-?(?:0|[1-9][0-9]*))\b")


class FrameSampleEvidenceError(SeriesCensusError):
    """The sampled image does not bind the requested decoded frame PTS."""

    code = "FRAME_SAMPLE_CORRESPONDENCE_INVALID"


class FrameSampleToolError(RuntimeError):
    """FFmpeg could not produce a deterministic frame sample."""

    code = "FRAME_SAMPLE_TOOL_FAILED"


class SourceProbePort(Protocol):
    def probe(self, source_path: Path, source: object) -> SourceMediaProbe: ...


class SourcePrepStore(Protocol):
    def read_outcome(self, job: Job, idempotency_key: str) -> CommandOutcome | None: ...

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

    def read_whole_series_source_manifest(
        self,
        job: Job,
        artifact_set_id: UUID,
    ) -> PersistedWholeSeriesSourceManifest: ...


@dataclass(frozen=True, slots=True)
class PreparedSourceEpisode:
    media_probe: SourceMediaProbe
    proxy_blob: BlobRef
    manifest: WindowManifest
    manifest_set: WindowManifestSet

    def to_mapping(self) -> dict[str, object]:
        return {
            "media_probe": self.media_probe.to_mapping(),
            "proxy_blob": _blob_mapping(self.proxy_blob),
            "window_manifest": self.manifest.to_mapping(),
            "window_manifest_set": self.manifest_set.to_mapping(),
        }


@dataclass(frozen=True, slots=True)
class PreparedSeriesSources:
    census: SeriesSourceCensus
    episodes: tuple[PreparedSourceEpisode, ...]

    def to_mapping(self) -> dict[str, object]:
        return {
            "census": self.census.to_mapping(),
            "census_sha256": self.census.canonical_hash,
            "completion_policy": "all_or_nothing",
            "episodes": [episode.to_mapping() for episode in self.episodes],
        }


@dataclass(frozen=True, slots=True)
class PersistedPreparedSources:
    """Strict source-prep value plus its exact committed Kernel provenance."""

    prepared: PreparedSeriesSources
    source_job: Job
    kernel_job_id: UUID
    receipt_id: UUID
    artifact_set_id: UUID
    command_slot_id: UUID
    artifact_reference: WholeSeriesSourceManifestReference

    def __post_init__(self) -> None:
        if type(self.prepared) is not PreparedSeriesSources:  # noqa: E721
            raise SourceManifestDecodeError("persisted prepared source value is invalid")
        if type(self.source_job) is not Job:  # noqa: E721
            raise SourceManifestDecodeError("persisted source Job is invalid")
        for field_name in (
            "kernel_job_id",
            "receipt_id",
            "artifact_set_id",
            "command_slot_id",
        ):
            if not isinstance(getattr(self, field_name), UUID):
                raise SourceManifestDecodeError(f"persisted source {field_name} must be a UUID")
        if type(self.artifact_reference) is not WholeSeriesSourceManifestReference:  # noqa: E721
            raise SourceManifestDecodeError("persisted source artifact reference is invalid")
        if self.artifact_reference.logical_id != "whole_series_source_manifest":
            raise SourceManifestDecodeError("persisted source artifact logical_id is invalid")
        if self.artifact_reference.scope != ArtifactScope(
            "pipeline", "job", self.source_job.job_key
        ):
            raise SourceManifestDecodeError(
                "persisted source artifact scope does not match its source Job"
            )
        payload_json = json.dumps(
            self.prepared.to_mapping(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if canonical_payload_hash(payload_json) != self.artifact_reference.content_hash:
            raise SourceManifestDecodeError(
                "persisted prepared sources do not match the artifact content hash"
            )

    def provenance_mapping(self) -> dict[str, object]:
        reference = self.artifact_reference
        return {
            "artifact_reference": {
                "artifact_type": reference.artifact_type,
                "content_hash": reference.content_hash,
                "logical_id": reference.logical_id,
                "revision": reference.revision,
                "scope": _scope_mapping(reference.scope),
            },
            "artifact_set_id": str(self.artifact_set_id),
            "command_slot_id": str(self.command_slot_id),
            "kernel_job_id": str(self.kernel_job_id),
            "receipt_id": str(self.receipt_id),
            "source_job": {
                "job_key": self.source_job.job_key,
                "profile": self.source_job.profile,
            },
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.provenance_mapping())


class IdentitySourceWindowBuilder:
    """Probe and build one exact identity window from one immutable snapshot."""

    def __init__(
        self,
        *,
        probe_port: FFprobeSourceMediaPort | None = None,
        ffmpeg_executable: str | None = None,
        sample_count: int = 9,
        timeout_seconds: float = 120.0,
    ) -> None:
        if type(sample_count) is not int or sample_count < 1:  # noqa: E721
            raise ValueError("sample_count must be positive")
        executable = ffmpeg_executable or shutil.which("ffmpeg")
        if not executable:
            raise SeriesCensusError("ffmpeg executable is unavailable")
        self._probe_port = probe_port or FFprobeSourceMediaPort(timeout_seconds=timeout_seconds)
        self._ffmpeg = executable
        self._sample_count = sample_count
        self._timeout_seconds = timeout_seconds

    def build(
        self,
        *,
        store: SourcePrepStore,
        job: Job,
        source_path: Path,
        source: object,
    ) -> PreparedSourceEpisode:
        from .models import SeriesSource

        if type(source) is not SeriesSource:  # noqa: E721
            raise TypeError("source must be a SeriesSource")
        source_bytes = source_path.read_bytes()
        if _sha256_bytes(source_bytes) != source.content_sha256:
            raise SeriesCensusError("snapshot bytes do not match the source census")
        probe = self._probe_port.probe(source_path, source)
        video = probe.video_probe
        stream = video.video_stream
        policy = _identity_policy()
        clock_id = f"video-stream-{stream.stream_index}"
        frame_index = _identity_frame_index(probe, policy)
        selected = _sample_indices(len(video.pts_index.ticks), self._sample_count)
        samples = tuple(
            self._frame_sample(source_path, index, video.pts_index.ticks[index])
            for index in selected
        )
        if _sha256_bytes(source_path.read_bytes()) != source.content_sha256:
            raise SeriesCensusError("snapshot changed while media evidence was produced")
        blob = store.put_immutable_blob(
            job,
            content=source_bytes,
            content_hash=source.content_sha256,
            media_type="video/mp4",
        )
        if (
            blob.content_hash != source.content_sha256
            or blob.byte_length != len(source_bytes)
            or blob.media_type != "video/mp4"
        ):
            raise SeriesCensusError("immutable proxy BlobRef does not match the identity bytes")
        proxy_ref = WindowProxyBlobRef(
            str(blob.object_id), blob.content_hash, blob.byte_length, blob.media_type
        )
        manifest = WindowManifest(
            source_id=source.source_id,
            source_clock_id=clock_id,
            source_sha256=source.content_sha256,
            stream_index=stream.stream_index,
            source_time_base=stream.time_base,
            source_range=probe.video_range,
            core_range=probe.video_range,
            frame_pts_index_set=frame_index,
            proxy_blob_ref=proxy_ref,
            preprocess_policy_sha256=policy,
            window_sampling_policy_sha256=canonical_sha256(
                {
                    "algorithm": "uniform-decoded-frame-index-v1",
                    "correspondence_certificate": (
                        "ffmpeg-copyts-showinfo-pts-equals-ffprobe-index-v1"
                    ),
                    "frame_encoding": "png-image2pipe-v1",
                    "sample_count": self._sample_count,
                    "selected_indices": list(selected),
                }
            ),
            timeline_map=ProxyTimelineMap.translation(
                time_base=stream.time_base,
                proxy_range=probe.video_range,
                source_start_pts=probe.video_range.start_pts,
            ),
            frame_samples=samples,
        )
        manifest_set = WindowManifestSet(
            source.source_id,
            clock_id,
            source.content_sha256,
            stream.stream_index,
            stream.time_base,
            probe.video_range,
            (manifest,),
        )
        bound_probe = probe.bind_presentation_timeline(
            source_blob=blob,
            frame_pts_index_set_sha256=frame_index.canonical_hash,
            source_proxy_timeline_map_sha256=manifest.timeline_map.canonical_hash,
            window_manifest_sha256=manifest.canonical_hash,
        )
        return PreparedSourceEpisode(bound_probe, blob, manifest, manifest_set)

    def _frame_sample(
        self,
        source_path: Path,
        frame_index: int,
        expected_pts: int,
    ) -> WindowFrameSample:
        try:
            completed = subprocess.run(
                [
                    self._ffmpeg,
                    "-y",
                    "-hide_banner",
                    "-nostdin",
                    "-loglevel",
                    "info",
                    "-copyts",
                    "-i",
                    str(source_path),
                    "-map",
                    "0:v:0",
                    "-vf",
                    f"select=eq(n\\,{frame_index}),showinfo",
                    "-frames:v",
                    "1",
                    "-f",
                    "image2pipe",
                    "-vcodec",
                    "png",
                    "pipe:1",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=self._timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise FrameSampleToolError("ffmpeg frame sampling failed") from error
        if completed.returncode != 0 or not completed.stdout:
            raise FrameSampleToolError("ffmpeg frame sampling failed")
        observed = tuple(int(value) for value in _SHOWINFO_PTS.findall(completed.stderr))
        if observed != (expected_pts,):
            raise FrameSampleEvidenceError(
                "sampled frame PTS does not match the decoded frame index"
            )
        return WindowFrameSample(
            expected_pts,
            expected_pts,
            _sha256_bytes(completed.stdout),
        )


@dataclass(frozen=True, slots=True)
class PrepareWholeSeriesSourcesRequest:
    job: Job
    idempotency_key: str
    artifact_scope: ArtifactScope
    artifact_revision: int
    source_root: AuthorizedSeriesSourceRoot

    def __post_init__(self) -> None:
        if self.artifact_scope != ArtifactScope("pipeline", "job", self.job.job_key):
            raise ValueError("artifact_scope must be the canonical Job scope")
        if type(self.artifact_revision) is not int or self.artifact_revision < 1:  # noqa: E721
            raise ValueError("artifact_revision must be positive")


@dataclass(frozen=True, slots=True)
class PrepareWholeSeriesSourcesResult:
    outcome: CommandOutcome
    prepared: PreparedSeriesSources | None = None
    artifacts: tuple[ArtifactMember, ...] = ()


class PrepareWholeSeriesSourcesCommand:
    def __init__(
        self,
        store: SourcePrepStore,
        *,
        builder: IdentitySourceWindowBuilder | None = None,
    ) -> None:
        self._store = store
        self._builder = builder or IdentitySourceWindowBuilder()

    def execute(self, request: PrepareWholeSeriesSourcesRequest) -> PrepareWholeSeriesSourcesResult:
        """Execute or safely resume one exact local, immutable preparation claim."""
        return self._execute_or_resume(request)

    def resume(self, request: PrepareWholeSeriesSourcesRequest) -> PrepareWholeSeriesSourcesResult:
        """Rebuild a running claim only after its recomputed request hash matches."""
        return self._execute_or_resume(request)

    def _execute_or_resume(
        self, request: PrepareWholeSeriesSourcesRequest
    ) -> PrepareWholeSeriesSourcesResult:
        existing = self._store.read_outcome(request.job, request.idempotency_key)
        if existing is not None and existing.state not in ("pending", "running"):
            # A terminal Receipt is authoritative. Never census or probe again.
            return self._replay(request, existing)
        with snapshot_series_sources(request.source_root) as snapshot:
            request_hash = canonical_sha256(
                {
                    "artifact_revision": request.artifact_revision,
                    "artifact_scope": _scope_mapping(request.artifact_scope),
                    "census_sha256": snapshot.census.canonical_hash,
                    "command": _COMMAND_NAME,
                    "job": {
                        "job_key": request.job.job_key,
                        "profile": request.job.profile,
                    },
                }
            )
            claimed = self._store.claim_command(
                CommandClaim(
                    request.job,
                    request.idempotency_key,
                    _COMMAND_NAME,
                    request_hash,
                )
            )
            if claimed.state not in ("pending", "running"):
                return self._replay(request, claimed)
            # A pre-existing running claim can only reach here after claim_command
            # has compared the recomputed census-bound request hash. All remaining
            # operations are local immutable writes, so rebuilding is safe and
            # concurrent exact-hash replays converge through Store CAS.
            try:
                episodes = tuple(
                    self._builder.build(
                        store=self._store,
                        job=request.job,
                        source_path=snapshot.root / source.relative_path,
                        source=source,
                    )
                    for source in snapshot.census.sources
                )
                prepared = PreparedSeriesSources(snapshot.census, episodes)
                artifacts = (_artifact(request, prepared.to_mapping()),)
                success = CommandSuccess(
                    claimed.command_slot_id,
                    _set_hash(artifacts),
                    artifacts,
                )
                outcome = self._store.commit_command_success(success)
                return PrepareWholeSeriesSourcesResult(outcome, prepared, artifacts)
            except SeriesCensusError as error:
                outcome = self._store.commit_command_rejection(
                    CommandRejection(
                        claimed.command_slot_id,
                        _diagnostic_code(error, "INVALID_SOURCE_EVIDENCE"),
                        _failure_detail(error, classification="denied"),
                        outcome="denied",
                    )
                )
                return PrepareWholeSeriesSourcesResult(outcome)
            except Exception as error:
                outcome = self._store.commit_command_rejection(
                    CommandRejection(
                        claimed.command_slot_id,
                        _diagnostic_code(error, "SOURCE_PREPARATION_FAILED"),
                        _failure_detail(error, classification="failed"),
                        outcome="failed",
                    )
                )
                return PrepareWholeSeriesSourcesResult(outcome)

    def _replay(
        self,
        request: PrepareWholeSeriesSourcesRequest,
        outcome: CommandOutcome,
    ) -> PrepareWholeSeriesSourcesResult:
        if outcome.state != "succeeded":
            return PrepareWholeSeriesSourcesResult(outcome)
        prepared = read_persisted_prepared_sources(
            self._store,
            job=request.job,
            outcome=outcome,
            artifact_scope=request.artifact_scope,
            artifact_revision=request.artifact_revision,
        )
        if prepared.census.policy != request.source_root.policy:
            raise SourceManifestDecodeError(
                "replayed source manifest does not match the exact operation policy"
            )
        artifact = _artifact(request, prepared.to_mapping())
        return PrepareWholeSeriesSourcesResult(outcome, prepared, (artifact,))


def read_persisted_prepared_sources(
    store: SourcePrepStore,
    *,
    job: Job,
    outcome: CommandOutcome,
    artifact_scope: ArtifactScope,
    artifact_revision: int,
) -> PreparedSeriesSources:
    """Return the source-prep projection from the Kernel-owned strict decoder."""

    return read_persisted_prepared_sources_bundle(
        store,
        job=job,
        outcome=outcome,
        artifact_scope=artifact_scope,
        artifact_revision=artifact_revision,
    ).prepared


def read_persisted_prepared_sources_bundle(
    store: SourcePrepStore,
    *,
    job: Job,
    outcome: CommandOutcome,
    artifact_scope: ArtifactScope,
    artifact_revision: int,
) -> PersistedPreparedSources:
    """Decode one committed source projection while retaining exact provenance."""

    if outcome.state != "succeeded" or outcome.receipt_id is None:
        raise SourceManifestDecodeError(
            "prepared sources require an exact succeeded Kernel Receipt"
        )
    if outcome.artifact_set_id is None:
        raise SourceManifestDecodeError("succeeded source preparation lost its ArtifactSet binding")
    persisted = store.read_whole_series_source_manifest(job, outcome.artifact_set_id)
    if (
        persisted.artifact_set_id != outcome.artifact_set_id
        or persisted.command_slot_id != outcome.command_slot_id
        or persisted.receipt_id != outcome.receipt_id
        or persisted.reference.scope != artifact_scope
        or persisted.reference.revision != artifact_revision
    ):
        raise SourceManifestDecodeError(
            "persisted source manifest provenance does not match its Receipt"
        )
    prepared = _decode_prepared_sources(persisted)
    return PersistedPreparedSources(
        prepared=prepared,
        source_job=job,
        kernel_job_id=persisted.job_id,
        receipt_id=persisted.receipt_id,
        artifact_set_id=persisted.artifact_set_id,
        command_slot_id=persisted.command_slot_id,
        artifact_reference=persisted.reference,
    )


def _artifact(
    request: PrepareWholeSeriesSourcesRequest,
    payload: object,
) -> ArtifactMember:
    payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return ArtifactMember(
        "whole_series_source_manifest",
        "whole_series_source_manifest",
        request.artifact_revision,
        request.artifact_scope,
        canonical_payload_hash(payload_json),
        payload_json,
    )


def _set_hash(artifacts: tuple[ArtifactMember, ...]) -> str:
    payload = [
        {
            "artifact_type": item.artifact_type,
            "content_hash": item.content_hash,
            "logical_id": item.logical_id,
            "payload_json": json.loads(item.payload_json),
            "revision": item.revision,
            "scope": _scope_mapping(item.scope),
        }
        for item in artifacts
    ]
    return canonical_sha256(payload)


def _scope_mapping(scope: ArtifactScope) -> dict[str, str]:
    return {"key": scope.key, "kind": scope.kind, "namespace": scope.namespace}


def _blob_mapping(blob: BlobRef) -> dict[str, object]:
    return {
        "byte_length": blob.byte_length,
        "content_hash": blob.content_hash,
        "media_type": blob.media_type,
        "object_id": str(blob.object_id),
    }


def _sample_indices(frame_count: int, sample_count: int) -> tuple[int, ...]:
    if frame_count < 1:
        raise SeriesCensusError("decoded frame PTS must not be empty")
    count = min(frame_count, sample_count)
    if count == 1:
        return (0,)
    return tuple(position * (frame_count - 1) // (count - 1) for position in range(count))


def _sha256_bytes(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _identity_policy() -> str:
    """Return the stable frame-generation strategy, never per-source input identity."""

    return IDENTITY_FRAME_GENERATION_POLICY_SHA256


def _identity_frame_index(
    probe: SourceMediaProbe,
    policy: str,
) -> FramePtsIndexSet:
    if policy != IDENTITY_FRAME_GENERATION_POLICY_SHA256:
        raise ValueError("identity frame policy is not canonical")
    return identity_frame_index(probe)


def _decode_prepared_sources(
    persisted: PersistedWholeSeriesSourceManifest,
) -> PreparedSeriesSources:
    """Adapt the Kernel-owned closed decoder into source-prep domain values."""

    from .models import SeriesSource

    decoded = decode_source_manifest(persisted.payload_json, persisted.proxy_blobs)
    sources = tuple(
        SeriesSource(
            source.relative_path,
            source.source_id,
            source.content_sha256,
            source.byte_size,
        )
        for source in decoded.census.sources
    )
    census = SeriesSourceCensus(
        decoded.census.policy,
        decoded.census.completion_policy,
        sources,
    )
    episodes = tuple(
        PreparedSourceEpisode(
            SourceMediaProbe(
                source,
                episode.media_probe.video_probe,
                episode.media_probe.video_range,
                episode.media_probe.audio_sample_boundaries,
                episode.media_probe.frame_detector_sha256,
                episode.media_probe.audio_detector_sha256,
                episode.media_probe.presentation_timeline_probe,
            ),
            proxy_blob,
            episode.manifest,
            episode.manifest_set,
        )
        for source, episode, proxy_blob in zip(
            sources,
            decoded.episodes,
            persisted.proxy_blobs,
            strict=True,
        )
    )
    prepared = PreparedSeriesSources(census, episodes)
    if prepared.to_mapping() != decoded.to_mapping():
        raise SourceManifestDecodeError(
            "source-prep projection changed the canonical Kernel manifest"
        )
    return prepared


def _diagnostic_code(error: Exception, default: str) -> str:
    if isinstance(error, RuntimeStoreError):
        return "SOURCE_PREP_PERSISTENCE_FAILED"
    value = getattr(error, "code", default)
    if type(value) is not str or re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", value) is None:  # noqa: E721
        return default
    return value


def _failure_detail(error: Exception, *, classification: str) -> str:
    return json.dumps(
        {
            "classification": classification,
            "diagnostic_code": _diagnostic_code(
                error,
                "SOURCE_PREPARATION_FAILED",
            ),
            "stage": _COMMAND_NAME,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


__all__ = [
    "IdentitySourceWindowBuilder",
    "PrepareWholeSeriesSourcesCommand",
    "PrepareWholeSeriesSourcesRequest",
    "PrepareWholeSeriesSourcesResult",
    "PreparedSeriesSources",
    "PreparedSourceEpisode",
    "PersistedPreparedSources",
    "SourcePrepStore",
    "read_persisted_prepared_sources",
    "read_persisted_prepared_sources_bundle",
]
