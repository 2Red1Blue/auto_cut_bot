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
from typing import Protocol, cast
from uuid import UUID

from autocut_kernel.media import (
    AudioBoundaryMethod,
    AudioSampleBoundary,
    AudioSampleBoundarySet,
    AudioSourceOutcome,
    Coverage,
    CoverageOutcome,
    EvidenceContext,
    FramePtsIndexSet,
    MediaKind,
    PTSIndex,
    TickRange,
    TimeBase,
)
from autocut_kernel.media.ffprobe_port import ProbeResult
from autocut_kernel.media.types import ToolEvidence, VideoStreamEvidence, canonical_sha256
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
from autocut_kernel.vlm.window import ProxyTimelineMap, ProxyTimelineSegment

from .census import snapshot_series_sources
from .models import AuthorizedSeriesSourceRoot, SeriesCensusError, SeriesSourceCensus
from .probe import FFprobeSourceMediaPort, SourceMediaProbe

_COMMAND_NAME = "PrepareWholeSeriesSourcesCommand"
_SHOWINFO_PTS = re.compile(rb"\bpts:\s*(-?(?:0|[1-9][0-9]*))\b")
IDENTITY_FRAME_GENERATION_POLICY_SHA256 = canonical_sha256(
    {
        "endpoint_rule": "complete-decoded-frame-pts-membership-v1",
        "operation": "identity",
        "producer": "identity-source-window-v2",
    }
)


class FrameSampleEvidenceError(SeriesCensusError):
    """The sampled image does not bind the requested decoded frame PTS."""

    code = "FRAME_SAMPLE_CORRESPONDENCE_INVALID"


class FrameSampleToolError(RuntimeError):
    """FFmpeg could not produce a deterministic frame sample."""

    code = "FRAME_SAMPLE_TOOL_FAILED"


class SourceManifestDecodeError(SeriesCensusError):
    """A committed source manifest cannot be reconstructed exactly."""

    code = "SOURCE_MANIFEST_INVALID"


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
                raise SourceManifestDecodeError(
                    f"persisted source {field_name} must be a UUID"
                )
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
        return PreparedSourceEpisode(probe, blob, manifest, manifest_set)

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

    def execute(
        self, request: PrepareWholeSeriesSourcesRequest
    ) -> PrepareWholeSeriesSourcesResult:
        """Execute or safely resume one exact local, immutable preparation claim."""
        return self._execute_or_resume(request)

    def resume(
        self, request: PrepareWholeSeriesSourcesRequest
    ) -> PrepareWholeSeriesSourcesResult:
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
        census = prepared.census
        if (
            census.authorization_id != request.source_root.authorization_id
            or census.series_id != request.source_root.series_id
            or len(census.sources) != request.source_root.expected_source_count
        ):
            raise SourceManifestDecodeError(
                "replayed source manifest does not match the authorized series identity"
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
    """Compatibility reader returning only the strictly decoded prepared value."""

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
        raise SourceManifestDecodeError(
            "succeeded source preparation lost its ArtifactSet binding"
        )
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
    payload_json = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
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
    stream = probe.video_probe.video_stream
    clock_id = f"video-stream-{stream.stream_index}"
    context = EvidenceContext(
        probe.source.source_id,
        probe.source.content_sha256,
        MediaKind.VIDEO,
        clock_id,
        stream.time_base,
        probe.video_range.start_pts,
        probe.video_range.duration_pts,
        "identity-source-window-v2",
        policy,
    )
    coverage = Coverage(
        probe.source.source_id,
        probe.source.content_sha256,
        clock_id,
        stream.time_base,
        probe.video_range.start_pts,
        probe.video_range.end_pts,
        CoverageOutcome.COMPLETE,
    )
    return FramePtsIndexSet(
        "identity-source-frame-pts-v2",
        context,
        coverage,
        probe.video_probe.pts_index,
        canonical_sha256(list(probe.video_probe.pts_index.ticks)),
    )


def _decode_prepared_sources(
    persisted: PersistedWholeSeriesSourceManifest,
) -> PreparedSeriesSources:
    try:
        raw: object = json.loads(
            persisted.payload_json,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"invalid JSON constant {value}")
            ),
        )
        root = _closed_mapping(
            raw,
            {"census", "census_sha256", "completion_policy", "episodes"},
            "source manifest",
        )
        census = _decode_census(root["census"])
        if (
            _text_value(root["census_sha256"], "census_sha256")
            != census.canonical_hash
            or root["completion_policy"] != census.completion_policy
        ):
            raise ValueError("source manifest census certificate is invalid")
        episodes_raw = _array(root["episodes"], "episodes")
        if len(episodes_raw) != len(census.sources) or len(episodes_raw) != len(
            persisted.proxy_blobs
        ):
            raise ValueError("source manifest episode count is inconsistent")
        episodes = tuple(
            _decode_episode(raw_episode, source, blob)
            for raw_episode, source, blob in zip(
                episodes_raw,
                census.sources,
                persisted.proxy_blobs,
                strict=True,
            )
        )
        prepared = PreparedSeriesSources(census, episodes)
        if prepared.to_mapping() != root:
            raise ValueError("source manifest is not the canonical prepared mapping")
        return prepared
    except SourceManifestDecodeError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise SourceManifestDecodeError(
            "committed source manifest failed strict decoding"
        ) from error


def _decode_census(value: object) -> SeriesSourceCensus:
    from .models import SeriesSource

    raw = _closed_mapping(
        value,
        {"authorization_id", "completion_policy", "series_id", "sources"},
        "census",
    )
    sources = tuple(
        SeriesSource(
            _text_value(item["relative_path"], "source.relative_path"),
            _text_value(item["source_id"], "source.source_id"),
            _text_value(item["content_sha256"], "source.content_sha256"),
            _int_value(item["byte_size"], "source.byte_size"),
        )
        for item in (
            _closed_mapping(
                entry,
                {"relative_path", "source_id", "content_sha256", "byte_size"},
                "source",
            )
            for entry in _array(raw["sources"], "census.sources")
        )
    )
    census = SeriesSourceCensus(
        _text_value(raw["authorization_id"], "census.authorization_id"),
        _text_value(raw["series_id"], "census.series_id"),
        _text_value(raw["completion_policy"], "census.completion_policy"),
        sources,
    )
    if census.to_mapping() != raw:
        raise ValueError("census is not canonical")
    return census


def _decode_episode(
    value: object,
    source: object,
    durable_blob: BlobRef,
) -> PreparedSourceEpisode:
    from .models import SeriesSource

    if type(source) is not SeriesSource:  # noqa: E721
        raise ValueError("episode source is invalid")
    raw = _closed_mapping(
        value,
        {"media_probe", "proxy_blob", "window_manifest", "window_manifest_set"},
        "episode",
    )
    declared_blob = _decode_blob(raw["proxy_blob"])
    if declared_blob != durable_blob:
        raise ValueError("episode proxy BlobRef does not match durable storage")
    probe = _decode_probe(raw["media_probe"], source)
    manifest = _decode_manifest(raw["window_manifest"], probe, durable_blob)
    manifest_set = _decode_manifest_set(raw["window_manifest_set"], manifest)
    return PreparedSourceEpisode(probe, durable_blob, manifest, manifest_set)


def _decode_probe(value: object, source: object) -> SourceMediaProbe:
    from .models import SeriesSource

    if type(source) is not SeriesSource:  # noqa: E721
        raise ValueError("probe source is invalid")
    raw = _closed_mapping(
        value,
        {
            "audio_sample_boundaries",
            "decoded_video_frame_pts",
            "ffprobe",
            "source",
            "video_stream",
        },
        "media_probe",
    )
    if raw["source"] != source.to_mapping():
        raise ValueError("media probe source identity is inconsistent")
    video = _closed_mapping(
        raw["video_stream"],
        {
            "codec_name",
            "duration_tick",
            "end_tick",
            "height",
            "index",
            "start_tick",
            "time_base",
            "width",
        },
        "video_stream",
    )
    time_base = _decode_time_base(video["time_base"], "video.time_base")
    video_range = TickRange(
        _int_value(video["start_tick"], "video.start_tick"),
        _int_value(video["end_tick"], "video.end_tick"),
    )
    if _int_value(video["duration_tick"], "video.duration_tick") != video_range.duration_pts:
        raise ValueError("video duration is inconsistent")
    tool_raw = _closed_mapping(
        raw["ffprobe"], {"executable", "stderr_sha256", "version"}, "ffprobe"
    )
    result = ProbeResult(
        VideoStreamEvidence(
            _int_value(video["index"], "video.index"),
            _text_value(video["codec_name"], "video.codec_name"),
            _int_value(video["width"], "video.width"),
            _int_value(video["height"], "video.height"),
            time_base,
        ),
        PTSIndex(
            tuple(
                _int_value(item, "decoded_video_frame_pts")
                for item in _array(
                    raw["decoded_video_frame_pts"], "decoded_video_frame_pts"
                )
            )
        ),
        ToolEvidence(
            _text_value(tool_raw["executable"], "ffprobe.executable"),
            _text_value(tool_raw["version"], "ffprobe.version"),
            _text_value(tool_raw["stderr_sha256"], "ffprobe.stderr_sha256"),
        ),
    )
    if any(
        not video_range.start_pts <= tick < video_range.end_pts
        for tick in result.pts_index.ticks
    ):
        raise ValueError("decoded video PTS is outside the half-open stream range")
    audio = _decode_audio_boundaries(raw["audio_sample_boundaries"])
    audio_context = audio.context
    audio_coverage = audio.coverage
    if (
        audio_context.source_id != source.source_id
        or audio_context.source_sha256 != source.content_sha256
        or audio_coverage.source_id != source.source_id
        or audio_coverage.source_sha256 != source.content_sha256
        or audio_coverage.clock_id != audio_context.clock_id
        or audio_coverage.time_base != audio_context.time_base
        or audio_coverage.in_tick != audio_context.origin_tick
        or audio_coverage.out_tick != audio_context.end_tick
        or any(
            point.source_id != source.source_id
            or point.source_sha256 != source.content_sha256
            or point.clock_id != audio_context.clock_id
            or point.time_base != audio_context.time_base
            or not audio_context.origin_tick <= point.tick <= audio_context.end_tick
            for point in audio.points
        )
    ):
        raise ValueError("audio evidence is not bound to the episode source clock")
    probe = SourceMediaProbe(
        source,
        result,
        video_range,
        audio,
    )
    if probe.to_mapping() != raw:
        raise ValueError("media probe is not canonical")
    return probe


def _decode_audio_boundaries(value: object) -> AudioSampleBoundarySet:
    raw = _closed_mapping(
        value,
        {
            "audio_sample_boundary_set_id",
            "context",
            "coverage",
            "points",
            "source_outcome",
        },
        "audio boundaries",
    )
    context_raw = _closed_mapping(
        raw["context"],
        {
            "clock_id",
            "duration_tick",
            "generation_policy_sha256",
            "media_kind",
            "origin_tick",
            "producer_id",
            "source_id",
            "source_sha256",
            "time_base",
        },
        "audio context",
    )
    context = EvidenceContext(
        _text_value(context_raw["source_id"], "audio.context.source_id"),
        _text_value(context_raw["source_sha256"], "audio.context.source_sha256"),
        MediaKind(_text_value(context_raw["media_kind"], "audio.context.media_kind")),
        _text_value(context_raw["clock_id"], "audio.context.clock_id"),
        _decode_time_base(context_raw["time_base"], "audio.context.time_base"),
        _int_value(context_raw["origin_tick"], "audio.context.origin_tick"),
        _int_value(context_raw["duration_tick"], "audio.context.duration_tick"),
        _text_value(context_raw["producer_id"], "audio.context.producer_id"),
        _text_value(
            context_raw["generation_policy_sha256"],
            "audio.context.generation_policy_sha256",
        ),
    )
    coverage_raw = _closed_mapping(
        raw["coverage"],
        {
            "clock_id",
            "diagnostics",
            "in_tick",
            "out_tick",
            "outcome",
            "source_id",
            "source_sha256",
            "time_base",
        },
        "audio coverage",
    )
    if _array(coverage_raw["diagnostics"], "audio.coverage.diagnostics"):
        raise ValueError("complete source audio coverage cannot contain diagnostics")
    coverage = Coverage(
        _text_value(coverage_raw["source_id"], "audio.coverage.source_id"),
        _text_value(
            coverage_raw["source_sha256"], "audio.coverage.source_sha256"
        ),
        _text_value(coverage_raw["clock_id"], "audio.coverage.clock_id"),
        _decode_time_base(coverage_raw["time_base"], "audio.coverage.time_base"),
        _int_value(coverage_raw["in_tick"], "audio.coverage.in_tick"),
        _int_value(coverage_raw["out_tick"], "audio.coverage.out_tick"),
        CoverageOutcome(
            _text_value(coverage_raw["outcome"], "audio.coverage.outcome")
        ),
    )
    points = tuple(
        AudioSampleBoundary(
            _text_value(item["boundary_id"], "audio.boundary_id"),
            _text_value(item["source_id"], "audio.source_id"),
            _text_value(item["source_sha256"], "audio.source_sha256"),
            _text_value(item["clock_id"], "audio.clock_id"),
            _decode_time_base(item["time_base"], "audio.time_base"),
            _int_value(item["tick"], "audio.tick"),
            AudioBoundaryMethod(_text_value(item["method"], "audio.method")),
        )
        for item in (
            _closed_mapping(
                entry,
                {
                    "boundary_id",
                    "clock_id",
                    "method",
                    "source_id",
                    "source_sha256",
                    "tick",
                    "time_base",
                },
                "audio boundary",
            )
            for entry in _array(raw["points"], "audio.points")
        )
    )
    result = AudioSampleBoundarySet(
        _text_value(
            raw["audio_sample_boundary_set_id"], "audio_sample_boundary_set_id"
        ),
        context,
        coverage,
        AudioSourceOutcome(
            _text_value(raw["source_outcome"], "audio.source_outcome")
        ),
        points,
    )
    if result.to_mapping() != raw:
        raise ValueError("audio boundaries are not canonical")
    return result


def _decode_manifest(
    value: object,
    probe: SourceMediaProbe,
    blob: BlobRef,
) -> WindowManifest:
    raw = _closed_mapping(
        value,
        {
            "core_range",
            "frame_pts_index_set_sha256",
            "frame_samples",
            "preprocess_policy_sha256",
            "proxy_blob_ref",
            "source_clock_id",
            "source_id",
            "source_range",
            "source_sha256",
            "source_time_base",
            "stream_index",
            "timeline_map",
            "window_sampling_policy_sha256",
        },
        "window manifest",
    )
    frame_index = _identity_frame_index(probe, _identity_policy())
    proxy = WindowProxyBlobRef(
        str(blob.object_id), blob.content_hash, blob.byte_length, blob.media_type
    )
    if raw["proxy_blob_ref"] != proxy.to_mapping():
        raise ValueError("window proxy BlobRef does not match durable storage")
    samples = tuple(
        WindowFrameSample(
            _int_value(item["source_pts"], "frame_sample.source_pts"),
            _int_value(item["proxy_pts"], "frame_sample.proxy_pts"),
            _text_value(item["frame_sha256"], "frame_sample.frame_sha256"),
        )
        for item in (
            _closed_mapping(
                entry,
                {"frame_sha256", "proxy_pts", "source_pts"},
                "frame sample",
            )
            for entry in _array(raw["frame_samples"], "frame_samples")
        )
    )
    result = WindowManifest(
        _text_value(raw["source_id"], "window.source_id"),
        _text_value(raw["source_clock_id"], "window.source_clock_id"),
        _text_value(raw["source_sha256"], "window.source_sha256"),
        _int_value(raw["stream_index"], "window.stream_index"),
        _decode_time_base(raw["source_time_base"], "window.source_time_base"),
        _decode_range(raw["source_range"], "window.source_range"),
        _decode_range(raw["core_range"], "window.core_range"),
        frame_index,
        proxy,
        _text_value(
            raw["preprocess_policy_sha256"], "window.preprocess_policy_sha256"
        ),
        _text_value(
            raw["window_sampling_policy_sha256"],
            "window.window_sampling_policy_sha256",
        ),
        _decode_timeline_map(raw["timeline_map"]),
        samples,
    )
    if result.to_mapping() != raw:
        raise ValueError("window manifest is not canonical")
    return result


def _decode_manifest_set(value: object, manifest: WindowManifest) -> WindowManifestSet:
    raw = _closed_mapping(
        value,
        {
            "declared_source_range",
            "frame_pts_index_set_sha256",
            "manifest_hashes",
            "source_clock_id",
            "source_id",
            "source_sha256",
            "source_time_base",
            "stream_index",
        },
        "window manifest set",
    )
    result = WindowManifestSet(
        _text_value(raw["source_id"], "window_set.source_id"),
        _text_value(raw["source_clock_id"], "window_set.source_clock_id"),
        _text_value(raw["source_sha256"], "window_set.source_sha256"),
        _int_value(raw["stream_index"], "window_set.stream_index"),
        _decode_time_base(raw["source_time_base"], "window_set.source_time_base"),
        _decode_range(raw["declared_source_range"], "window_set.declared_source_range"),
        (manifest,),
    )
    if result.to_mapping() != raw:
        raise ValueError("window manifest set is not canonical")
    return result


def _decode_timeline_map(value: object) -> ProxyTimelineMap:
    raw = _closed_mapping(
        value,
        {"certificate_kind", "proxy_time_base", "segments", "source_time_base"},
        "timeline map",
    )
    segments = tuple(
        ProxyTimelineSegment(
            _decode_range(item["proxy_range"], "timeline.proxy_range"),
            _decode_range(item["source_range"], "timeline.source_range"),
            _int_value(
                item["max_source_error_pts"], "timeline.max_source_error_pts"
            ),
        )
        for item in (
            _closed_mapping(
                entry,
                {"max_source_error_pts", "proxy_range", "source_range"},
                "timeline segment",
            )
            for entry in _array(raw["segments"], "timeline.segments")
        )
    )
    result = ProxyTimelineMap(
        _decode_time_base(raw["proxy_time_base"], "timeline.proxy_time_base"),
        _decode_time_base(raw["source_time_base"], "timeline.source_time_base"),
        segments,
        _text_value(raw["certificate_kind"], "timeline.certificate_kind"),
    )
    if result.to_mapping() != raw:
        raise ValueError("timeline map is not canonical")
    return result


def _decode_blob(value: object) -> BlobRef:
    raw = _closed_mapping(
        value,
        {"byte_length", "content_hash", "media_type", "object_id"},
        "proxy blob",
    )
    return BlobRef(
        UUID(_text_value(raw["object_id"], "proxy_blob.object_id")),
        _text_value(raw["content_hash"], "proxy_blob.content_hash"),
        _int_value(raw["byte_length"], "proxy_blob.byte_length"),
        _text_value(raw["media_type"], "proxy_blob.media_type"),
    )


def _decode_time_base(value: object, field_name: str) -> TimeBase:
    raw = _closed_mapping(value, {"denominator", "numerator"}, field_name)
    return TimeBase(
        _int_value(raw["numerator"], f"{field_name}.numerator"),
        _int_value(raw["denominator"], f"{field_name}.denominator"),
    )


def _decode_range(value: object, field_name: str) -> TickRange:
    raw = _closed_mapping(value, {"end_pts", "start_pts"}, field_name)
    return TickRange(
        _int_value(raw["start_pts"], f"{field_name}.start_pts"),
        _int_value(raw["end_pts"], f"{field_name}.end_pts"),
    )


def _closed_mapping(
    value: object,
    keys: set[str],
    field_name: str,
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be a closed object")
    mapping = cast(dict[object, object], value)
    if set(mapping) != keys:
        raise ValueError(f"{field_name} must be a closed object")
    return {str(key): item for key, item in mapping.items()}


def _array(value: object, field_name: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be an array")
    return list(cast(list[object], value))


def _text_value(value: object, field_name: str) -> str:
    if type(value) is not str or not value:  # noqa: E721
        raise ValueError(f"{field_name} must be text")
    return value


def _int_value(value: object, field_name: str) -> int:
    if type(value) is not int:  # noqa: E721
        raise ValueError(f"{field_name} must be an integer")
    return value


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
