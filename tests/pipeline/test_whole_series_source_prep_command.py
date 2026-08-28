from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from autocut_kernel.media import MediaKind, PTSIndex, TimeBase
from autocut_kernel.media.audio_stream_facts import decode_audio_stream_facts
from autocut_kernel.media.ffprobe_port import FFprobePort, ProbeResult
from autocut_kernel.media.types import (
    MediaValidationError,
    ToolEvidence,
    VideoStreamEvidence,
    canonical_sha256,
)
from autocut_kernel.pipeline import GenerateVlmEvidenceRequest
from autocut_kernel.store import (
    ArtifactScope,
    BlobIntegrityError,
    BlobRef,
    CommandClaim,
    CommandOutcome,
    CommandRejection,
    CommandSuccess,
    IdempotencyConflictError,
    Job,
    PersistedWholeSeriesSourceManifest,
    PostgresRuntimeStore,
    WholeSeriesSourceManifestReference,
)
from autocut_kernel.store.models import canonical_payload_hash
from autocut_kernel.vlm import (
    GENERATION_RETRY_STRATEGY_VERSION,
    GenerationRetryPolicy,
    VlmParsePolicy,
    WindowFrameSample,
)

from auto_cut_bot.pipeline.runtime import (
    DurablePipelineRunService,
    FullStageVlmRecomputeBinder,
    PipelineExecutionProfile,
    PipelineRunRequest,
    PipelineStageContext,
    PipelineStageResult,
    PostgresPipelineRunStore,
    SourcePrepPipelineStage,
    VlmFullStageRecomputeRequest,
)
from auto_cut_bot.pipeline.runtime.source_prep_stage import source_prep_kernel_idempotency_key
from auto_cut_bot.pipeline.source_prep import (
    DECODED_AUDIO_BOUNDARY_GENERATION_POLICY_SHA256,
    IDENTITY_FRAME_GENERATION_POLICY_SHA256,
    AuthorizedSeriesSourceRoot,
    BindWholeSeriesSourcesCommand,
    BindWholeSeriesSourcesRequest,
    FFprobeSourceMediaPort,
    IdentitySourceWindowBuilder,
    PrepareWholeSeriesSourcesCommand,
    PrepareWholeSeriesSourcesRequest,
    PrepareWholeSeriesSourcesResult,
    SourceOperationPolicy,
    SourceOperationPurpose,
    SourcePurposeDeniedError,
)
from auto_cut_bot.pipeline.source_prep import probe as source_probe
from auto_cut_bot.pipeline.source_prep.command import (
    FrameSampleEvidenceError,
    SourceManifestDecodeError,
    _identity_frame_index,
    _identity_policy,
)
from auto_cut_bot.pipeline.source_prep.models import SeriesCensusError, SeriesSource
from auto_cut_bot.pipeline.source_prep.probe import SourceMediaEvidenceError
from auto_cut_bot.pipeline.vlm import DoubaoVlmRequestPolicy

try:
    import psycopg
except ModuleNotFoundError:
    psycopg = None


VERIFY_POSTGRES_DSN = "postgresql://ac_user:ac_password_2026@127.0.0.1:5433/ac_autocut_verify"


class Store:
    def __init__(self) -> None:
        self.claims: dict[tuple[str, str], tuple[CommandClaim, CommandOutcome]] = {}
        self.blobs: dict[str, bytes] = {}
        self.blob_refs: dict[str, BlobRef] = {}
        self.successes: list[CommandSuccess] = []
        self.rejections: list[CommandRejection] = []
        self.manifests: dict[UUID, PersistedWholeSeriesSourceManifest] = {}

    def read_outcome(self, job: Job, idempotency_key: str) -> CommandOutcome | None:
        existing = self.claims.get((job.job_key, idempotency_key))
        return None if existing is None else existing[1]

    def claim_command(self, claim: CommandClaim) -> CommandOutcome:
        key = (claim.job.job_key, claim.idempotency_key)
        existing = self.claims.get(key)
        if existing is not None:
            if existing[0].request_hash != claim.request_hash:
                raise IdempotencyConflictError(
                    "idempotency key was already claimed by a different request hash"
                )
            return existing[1]
        outcome = CommandOutcome(uuid4(), "running", is_fresh_claim=True)
        self.claims[key] = (claim, outcome)
        return outcome

    def put_immutable_blob(
        self,
        _job: Job,
        *,
        content: bytes,
        content_hash: str,
        media_type: str,
    ) -> BlobRef:
        assert content_hash == "sha256:" + hashlib.sha256(content).hexdigest()
        self.blobs[content_hash] = content
        reference = BlobRef(uuid4(), content_hash, len(content), media_type)
        self.blob_refs[content_hash] = reference
        return reference

    def commit_command_success(self, success: CommandSuccess) -> CommandOutcome:
        self.successes.append(success)
        outcome = CommandOutcome(
            success.command_slot_id,
            "succeeded",
            receipt_id=uuid4(),
            artifact_set_id=uuid4(),
        )
        member = success.artifacts[0]
        payload = json.loads(member.payload_json)
        proxy_blobs = tuple(
            self.blob_refs[episode["proxy_blob"]["content_hash"]] for episode in payload["episodes"]
        )
        assert outcome.artifact_set_id is not None
        assert outcome.receipt_id is not None
        self.manifests[outcome.artifact_set_id] = PersistedWholeSeriesSourceManifest(
            WholeSeriesSourceManifestReference(
                member.scope,
                member.logical_id,
                member.revision,
                member.content_hash,
            ),
            member.payload_json,
            proxy_blobs,
            uuid4(),
            outcome.receipt_id,
            outcome.artifact_set_id,
            outcome.command_slot_id,
        )
        self._replace(success.command_slot_id, outcome)
        return outcome

    def commit_source_reuse_success(self, success: CommandSuccess, *, binding) -> CommandOutcome:
        assert len(success.artifacts) == 2
        assert success.artifacts[0].payload_json == binding.origin.payload_json
        outcome = CommandOutcome(
            success.command_slot_id,
            "succeeded",
            receipt_id=uuid4(),
            artifact_set_id=uuid4(),
        )
        assert outcome.artifact_set_id is not None
        assert outcome.receipt_id is not None
        source = success.artifacts[0]
        self.successes.append(success)
        self.manifests[outcome.artifact_set_id] = PersistedWholeSeriesSourceManifest(
            WholeSeriesSourceManifestReference(
                source.scope,
                source.logical_id,
                source.revision,
                source.content_hash,
            ),
            source.payload_json,
            binding.origin.proxy_blobs,
            uuid4(),
            outcome.receipt_id,
            outcome.artifact_set_id,
            outcome.command_slot_id,
        )
        self._replace(success.command_slot_id, outcome)
        return outcome

    def commit_command_rejection(self, rejection: CommandRejection) -> CommandOutcome:
        self.rejections.append(rejection)
        outcome = CommandOutcome(
            rejection.command_slot_id,
            rejection.outcome,
            receipt_id=uuid4(),
            failure_code=rejection.failure_code,
            failure_detail_json=rejection.failure_detail_json,
        )
        self._replace(rejection.command_slot_id, outcome)
        return outcome

    def _replace(self, slot_id: UUID, outcome: CommandOutcome) -> None:
        for key, (claim, current) in self.claims.items():
            if current.command_slot_id == slot_id:
                self.claims[key] = (claim, outcome)
                return
        raise AssertionError("unknown slot")

    def read_whole_series_source_manifest(
        self,
        job: Job,
        artifact_set_id: UUID,
    ) -> PersistedWholeSeriesSourceManifest:
        persisted = self.manifests[artifact_set_id]
        claim = next(
            claim
            for claim, outcome in self.claims.values()
            if outcome.artifact_set_id == artifact_set_id
        )
        assert claim.job == job
        return persisted


class CountingProbe(FFprobeSourceMediaPort):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def probe(self, source_path: Path, source: object):
        self.calls += 1
        return super().probe(source_path, source)  # type: ignore[arg-type]


class SimulatedProcessCrash(BaseException):
    pass


class CrashAfterPartialBlobBuilder:
    def build(self, *, store, job, source_path, source):
        del source_path, source
        content = b"partial-source-prep-checkpoint"
        store.put_immutable_blob(
            job,
            content=content,
            content_hash="sha256:" + hashlib.sha256(content).hexdigest(),
            media_type="application/octet-stream",
        )
        raise SimulatedProcessCrash


class MustNotBuild:
    def build(self, **_kwargs):
        raise AssertionError("changed request hash must fail before rebuilding")


def _source_policy(
    authorization_id: str = "authority",
    series_id: str = "series",
    expected_source_count: int = 1,
    authorized_purposes: tuple[SourceOperationPurpose, ...] = (
        "semantic_analysis",
        "render_source",
    ),
) -> SourceOperationPolicy:
    return SourceOperationPolicy(
        authorization_id,
        series_id,
        expected_source_count,
        authorized_purposes,
    )


def _source_root(
    root: Path,
    authorization_id: str = "authority",
    series_id: str = "series",
    expected_source_count: int = 1,
    authorized_purposes: tuple[SourceOperationPurpose, ...] = (
        "semantic_analysis",
        "render_source",
    ),
) -> AuthorizedSeriesSourceRoot:
    return AuthorizedSeriesSourceRoot(
        root,
        _source_policy(
            authorization_id,
            series_id,
            expected_source_count,
            authorized_purposes,
        ),
    )


def test_source_operation_policy_is_canonical_hash_bound_and_default_deny() -> None:
    policy = _source_policy(authorized_purposes=("render_source", "semantic_analysis"))

    assert policy.authorized_purposes == ("semantic_analysis", "render_source")
    assert policy.policy_sha256 == canonical_sha256(policy.to_mapping())
    policy.require_purpose("semantic_analysis")
    policy.require_purpose("render_source")

    render_only = _source_policy(authorized_purposes=("render_source",))
    with pytest.raises(SourcePurposeDeniedError, match="semantic_analysis"):
        render_only.require_purpose("semantic_analysis")


@pytest.mark.parametrize(
    "authorized_purposes",
    [
        (),
        ("semantic_analysis", "semantic_analysis"),
        ("unknown",),
    ],
)
def test_source_operation_policy_rejects_invalid_purpose_sets(
    authorized_purposes: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError):
        _source_policy(
            authorized_purposes=authorized_purposes,  # type: ignore[arg-type]
        )


def _make_nonzero_media(path: Path) -> None:
    completed = subprocess.run(
        [
            shutil.which("ffmpeg") or "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=96x64:rate=7:duration=1.2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=1000:sample_rate=48000:duration=1.2",
            "-vf",
            "select=not(eq(mod(n\\,3)\\,2)),setpts=PTS+2/TB",
            "-af",
            "asetpts=PTS+2/TB",
            "-copyts",
            "-fps_mode",
            "vfr",
            "-c:v",
            "mpeg4",
            "-q:v",
            "5",
            "-c:a",
            "aac",
            str(path),
        ],
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg and ffprobe are required",
)
def test_real_probe_identity_window_persistence_and_replay(tmp_path: Path) -> None:
    root = tmp_path / "videos"
    root.mkdir()
    source = root / "episode.mp4"
    _make_nonzero_media(source)
    probe = CountingProbe()
    store = Store()
    job = Job("whole-series-source-prep", "test")
    scope = ArtifactScope("pipeline", "job", job.job_key)
    request = PrepareWholeSeriesSourcesRequest(
        job,
        "prepare-v1",
        scope,
        1,
        _source_root(root.resolve(), "fixture-authority", "fixture-series", 1),
    )
    command = PrepareWholeSeriesSourcesCommand(
        store,
        builder=IdentitySourceWindowBuilder(probe_port=probe, sample_count=3),
    )

    first = command.execute(request)
    replay = command.execute(request)

    assert first.outcome.state == replay.outcome.state == "succeeded"
    assert probe.calls == 1
    assert len(store.successes) == 1
    assert first.prepared is not None
    assert replay.prepared == first.prepared
    assert replay.prepared is not None
    assert replay.prepared.census.policy == request.source_root.policy
    replay.prepared.census.require_purpose("semantic_analysis")
    persisted_mapping = json.loads(first.artifacts[0].payload_json)
    assert str(root.resolve()) not in first.artifacts[0].payload_json
    assert persisted_mapping["census"]["authorization_policy_sha256"] == (
        request.source_root.policy.policy_sha256
    )
    episode = replay.prepared.episodes[0]
    assert episode.media_probe.video_range.start_pts > 0
    frame_ticks = episode.media_probe.video_probe.pts_index.ticks
    assert len({right - left for left, right in zip(frame_ticks, frame_ticks[1:])}) > 1
    assert episode.manifest.source_range == episode.media_probe.video_range
    assert episode.manifest.timeline_map.certificate_kind == "translation_certificate"
    assert episode.manifest_set.manifests == (episode.manifest,)
    assert (
        episode.manifest.frame_pts_index_set.context.generation_policy_sha256
        == IDENTITY_FRAME_GENERATION_POLICY_SHA256
    )
    assert (
        episode.media_probe.audio_sample_boundaries.context.generation_policy_sha256
        == DECODED_AUDIO_BOUNDARY_GENERATION_POLICY_SHA256
    )
    assert episode.media_probe.audio_sample_boundaries.points[0].tick == (
        episode.media_probe.audio_sample_boundaries.context.origin_tick
    )
    assert episode.media_probe.audio_sample_boundaries.points[-1].tick == (
        episode.media_probe.audio_sample_boundaries.context.end_tick
    )
    presentation = episode.media_probe.presentation_timeline_probe
    assert presentation is not None
    assert presentation.source_sha256 == episode.proxy_blob.content_hash
    assert presentation.video.origin_tick == episode.media_probe.video_range.start_pts
    assert presentation.video.end_tick == episode.media_probe.video_range.end_pts
    assert presentation.audio.origin_tick == (
        episode.media_probe.audio_sample_boundaries.context.origin_tick
    )
    assert presentation.audio.end_tick == (
        episode.media_probe.audio_sample_boundaries.context.end_tick
    )
    assert presentation.video.index_sha256 == episode.manifest.frame_pts_index_set.canonical_hash
    assert (
        presentation.audio.index_sha256
        == episode.media_probe.audio_sample_boundaries.canonical_hash
    )
    assert (
        presentation.source_proxy_timeline_map_sha256
        == episode.manifest.timeline_map.canonical_hash
    )
    assert presentation.window_manifest_sha256 == episode.manifest.canonical_hash
    assert all(
        episode.manifest.frame_pts_index_set.pts_index.contains(sample.source_pts)
        for sample in episode.manifest.frame_samples
    )
    vlm_request = GenerateVlmEvidenceRequest(
        job,
        "vlm-v1",
        scope,
        1,
        episode.manifest,
        episode.manifest_set,
        episode.proxy_blob,
        "prompt",
        "prompt-v1",
        '{"type":"object"}',
        "{}",
        "model",
        "provider",
        VlmParsePolicy(
            max_response_bytes=100_000,
            max_entities=10,
            max_facts=100,
            max_events=100,
            max_candidate_hypotheses=10,
            max_temporal_segments=10,
            max_measurements=100,
            max_text_characters=1_000,
            max_total_text_characters=10_000,
        ),
        GenerationRetryPolicy(GENERATION_RETRY_STRATEGY_VERSION, 1, ()),
    )
    assert vlm_request.proxy_blob == episode.proxy_blob


class TamperingStore(Store):
    def put_immutable_blob(
        self,
        job: Job,
        *,
        content: bytes,
        content_hash: str,
        media_type: str,
    ) -> BlobRef:
        reference = super().put_immutable_blob(
            job, content=content, content_hash=content_hash, media_type=media_type
        )
        return BlobRef(reference.object_id, "sha256:" + "0" * 64, reference.byte_length, media_type)


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg and ffprobe are required",
)
def test_proxy_blob_tamper_fails_closed_without_artifact_set(tmp_path: Path) -> None:
    root = tmp_path / "videos"
    root.mkdir()
    _make_nonzero_media(root / "episode.mp4")
    store = TamperingStore()
    job = Job("tamper", "test")
    result = PrepareWholeSeriesSourcesCommand(
        store,
        builder=IdentitySourceWindowBuilder(sample_count=1),
    ).execute(
        PrepareWholeSeriesSourcesRequest(
            job,
            "prepare-v1",
            ArtifactScope("pipeline", "job", job.job_key),
            1,
            _source_root(root.resolve()),
        )
    )

    assert result.outcome.state == "denied"
    assert result.prepared is None
    assert store.successes == []
    assert len(store.rejections) == 1


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg and ffprobe are required",
)
def test_terminal_replay_uses_committed_snapshot_after_source_mutation(tmp_path: Path) -> None:
    root = tmp_path / "videos"
    root.mkdir()
    source = root / "episode.mp4"
    _make_nonzero_media(source)
    probe = CountingProbe()
    store = Store()
    job = Job("mutation", "test")
    request = PrepareWholeSeriesSourcesRequest(
        job,
        "prepare-v1",
        ArtifactScope("pipeline", "job", job.job_key),
        1,
        _source_root(root.resolve()),
    )
    command = PrepareWholeSeriesSourcesCommand(
        store,
        builder=IdentitySourceWindowBuilder(probe_port=probe, sample_count=1),
    )
    first = command.execute(request)
    assert first.outcome.state == "succeeded"
    source.write_bytes(source.read_bytes() + b"mutated")

    replay = command.execute(request)

    assert replay.outcome.state == "succeeded"
    assert replay.prepared == first.prepared
    assert probe.calls == 1


def test_running_claim_recomputes_census_and_rejects_changed_request_hash(
    tmp_path: Path,
) -> None:
    root = tmp_path / "videos"
    root.mkdir()
    source = root / "episode.mp4"
    source.write_bytes(b"first immutable source")
    store = Store()
    job = Job("source-prep-changed-census", "test")
    request = PrepareWholeSeriesSourcesRequest(
        job,
        "prepare-v1",
        ArtifactScope("pipeline", "job", job.job_key),
        1,
        _source_root(root.resolve()),
    )
    with pytest.raises(SimulatedProcessCrash):
        PrepareWholeSeriesSourcesCommand(
            store,
            builder=CrashAfterPartialBlobBuilder(),  # type: ignore[arg-type]
        ).execute(request)
    running = store.read_outcome(job, request.idempotency_key)
    assert running is not None and running.state == "running"
    source.write_bytes(b"changed immutable source")

    with pytest.raises(IdempotencyConflictError, match="different request hash"):
        PrepareWholeSeriesSourcesCommand(
            store,
            builder=MustNotBuild(),  # type: ignore[arg-type]
        ).resume(request)


@pytest.mark.skipif(
    psycopg is None
    or os.environ.get("AUTOCUT_TEST_POSTGRES_DSN") != VERIFY_POSTGRES_DSN
    or shutil.which("ffmpeg") is None
    or shutil.which("ffprobe") is None,
    reason="disposable PostgreSQL, ffmpeg and ffprobe are required",
)
def test_crash_after_partial_blob_concurrent_exact_replays_converge(
    tmp_path: Path,
) -> None:
    dsn = os.environ["AUTOCUT_TEST_POSTGRES_DSN"]
    with psycopg.connect(dsn, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA IF EXISTS runtime CASCADE")
            cursor.execute("DROP SCHEMA IF EXISTS storage CASCADE")
            migrations = Path("packages/autocut-kernel/migrations")
            for name in (
                "0001_runtime_core.sql",
                "0002_runtime_core_constraints.sql",
                "0003_vlm_generation_and_run_finalization.sql",
                "0004_provider_media_objects.sql",
            ):
                cursor.execute((migrations / name).read_text())
    root = tmp_path / "videos"
    root.mkdir()
    _make_nonzero_media(root / "episode.mp4")
    job = Job("source-prep-concurrent-resume", "test")
    request = PrepareWholeSeriesSourcesRequest(
        job,
        "prepare-v1",
        ArtifactScope("pipeline", "job", job.job_key),
        1,
        _source_root(root.resolve()),
    )
    store = PostgresRuntimeStore(lambda: psycopg.connect(dsn))
    with pytest.raises(SimulatedProcessCrash):
        PrepareWholeSeriesSourcesCommand(
            store,
            builder=CrashAfterPartialBlobBuilder(),  # type: ignore[arg-type]
        ).execute(request)
    running = store.read_outcome(job, request.idempotency_key)
    assert running is not None and running.state == "running"

    def resume() -> PrepareWholeSeriesSourcesResult:
        return PrepareWholeSeriesSourcesCommand(
            store,
            builder=IdentitySourceWindowBuilder(sample_count=1),
        ).resume(request)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _index: resume(), range(2)))

    assert all(result.outcome.state == "succeeded" for result in results)
    assert results[0].outcome.receipt_id == results[1].outcome.receipt_id
    assert results[0].outcome.artifact_set_id == results[1].outcome.artifact_set_id
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM runtime.command_receipts")
            assert cursor.fetchone() == (1,)
            cursor.execute("SELECT count(*) FROM runtime.artifact_sets")
            assert cursor.fetchone() == (1,)


@pytest.mark.skipif(
    psycopg is None or os.environ.get("AUTOCUT_TEST_POSTGRES_DSN") != VERIFY_POSTGRES_DSN,
    reason="set AUTOCUT_TEST_POSTGRES_DSN to the dedicated ac_autocut_verify database",
)
def test_real_episode_persists_receipt_artifact_set_blob_and_replays(tmp_path: Path) -> None:
    dsn = os.environ["AUTOCUT_TEST_POSTGRES_DSN"]
    with psycopg.connect(dsn, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA IF EXISTS runtime CASCADE")
            cursor.execute("DROP SCHEMA IF EXISTS storage CASCADE")
            migrations = Path("packages/autocut-kernel/migrations")
            # Source preparation uses only the established runtime/storage baseline;
            # the separate HTTP run-control migration is intentionally out of scope.
            for name in (
                "0001_runtime_core.sql",
                "0002_runtime_core_constraints.sql",
                "0003_vlm_generation_and_run_finalization.sql",
                "0004_provider_media_objects.sql",
            ):
                cursor.execute((migrations / name).read_text())
    root = tmp_path / "videos"
    root.mkdir()
    _make_nonzero_media(root / "episode.mp4")
    job = Job("source-prep-postgres", "test")
    request = PrepareWholeSeriesSourcesRequest(
        job,
        "prepare-v1",
        ArtifactScope("pipeline", "job", job.job_key),
        1,
        _source_root(root.resolve()),
    )
    probe = CountingProbe()
    store = PostgresRuntimeStore(lambda: psycopg.connect(dsn))
    command = PrepareWholeSeriesSourcesCommand(
        store,
        builder=IdentitySourceWindowBuilder(probe_port=probe, sample_count=3),
    )

    first = command.execute(request)
    replay = PrepareWholeSeriesSourcesCommand(
        store,
        builder=IdentitySourceWindowBuilder(probe_port=probe, sample_count=3),
    ).execute(request)

    assert first.outcome.state == replay.outcome.state == "succeeded"
    assert first.outcome.receipt_id == replay.outcome.receipt_id
    assert first.outcome.artifact_set_id == replay.outcome.artifact_set_id
    assert probe.calls == 1
    assert replay.prepared == first.prepared
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM runtime.command_receipts")
            assert cursor.fetchone() == (1,)
            cursor.execute("SELECT count(*) FROM runtime.artifact_sets")
            assert cursor.fetchone() == (1,)
            cursor.execute("SELECT count(*) FROM storage.blob_objects")
            assert cursor.fetchone() == (1,)


class AudioFramesProbe(FFprobeSourceMediaPort):
    def __init__(self, frames: list[dict[str, object]]) -> None:
        super().__init__(executable="/usr/bin/true")
        self.frames = frames

    def _json(self, command: list[str]):
        del command
        return {"frames": self.frames}


@pytest.mark.parametrize(
    "frames",
    [
        [],
        [
            {
                "media_type": "audio",
                "stream_index": 1,
                "best_effort_timestamp": 0,
                "duration": 8,
            },
            {
                "media_type": "audio",
                "stream_index": 1,
                "best_effort_timestamp": 10,
                "duration": 10,
            },
        ],
    ],
)
def test_audio_boundaries_reject_empty_frames_and_preserve_discontinuous_frames(
    frames: list[dict[str, object]],
) -> None:
    source = SeriesSource("episode.mp4", "source-a", "sha256:" + "1" * 64, 1)
    port = AudioFramesProbe(frames)
    if not frames:
        with pytest.raises(SourceMediaEvidenceError):
            port._audio_boundaries(
                Path("unused.mp4"),
                source,
                {"index": 1, "time_base": "1/48000", "start_pts": 0, "duration_ts": 20},
            )
        return
    boundaries = port._audio_boundaries(
        Path("unused.mp4"),
        source,
        {"index": 1, "time_base": "1/48000", "start_pts": 0, "duration_ts": 20},
    )
    assert tuple(point.tick for point in boundaries.points) == (0, 8, 10, 20)


def test_presentation_track_preserves_nonzero_vfr_tails_and_declared_gap() -> None:
    frames = (
        source_probe.DecodedFrame(1_000, 1_010),
        source_probe.DecodedFrame(1_010, 1_050),
        source_probe.DecodedFrame(1_080, 1_087),
    )
    track = source_probe._presentation_track(
        MediaKind.VIDEO,
        0,
        TimeBase(1, 1_000),
        frames,
        "sha256:" + "a" * 64,
    )

    assert track.origin_tick == 1_000
    assert track.end_tick == 1_087
    assert [
        (item.stream_tick_range.start_pts, item.stream_tick_range.end_pts)
        for item in track.segments
    ] == [
        (1_000, 1_050),
        (1_050, 1_080),
        (1_080, 1_087),
    ]
    assert track.segments[1].continuity.value == "declared_gap"
    assert track.segments[2].stream_tick_range.end_pts == 1_087
    audio = source_probe._presentation_track(
        MediaKind.AUDIO,
        1,
        TimeBase(1, 1_000),
        (
            source_probe.DecodedFrame(1_000, 1_040),
            source_probe.DecodedFrame(1_040, 1_095),
        ),
        "sha256:" + "b" * 64,
    )
    assert audio.origin_tick == track.origin_tick == 1_000
    assert audio.end_tick == 1_095
    assert audio.end_tick != track.end_tick


def test_decoded_frame_probe_rejects_unproved_final_frame_end() -> None:
    port = FFprobeSourceMediaPort(executable="/usr/bin/true")
    port._json = lambda _command: {  # type: ignore[method-assign]
        "frames": [
            {
                "media_type": "video",
                "stream_index": 0,
                "best_effort_timestamp": 10,
            }
        ]
    }

    with pytest.raises(SourceMediaEvidenceError, match="end boundary"):
        port._decoded_frames(
            Path("unused.mp4"),
            selector="v:0",
            expected_media_kind="video",
            stream_index=0,
            field_name="video",
        )


def test_frame_sample_rejects_ffmpeg_pts_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=b"png",
        stderr=b"[Parsed_showinfo_1] n:0 pts:11 pts_time:0.1",
    )
    monkeypatch.setattr(
        "auto_cut_bot.pipeline.source_prep.command.subprocess.run",
        lambda *args, **kwargs: completed,
    )
    builder = IdentitySourceWindowBuilder(ffmpeg_executable="/usr/bin/true")
    with pytest.raises(FrameSampleEvidenceError):
        builder._frame_sample(Path("snapshot.mp4"), 3, 10)


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg and ffprobe are required",
)
def test_terminal_replay_rejects_tampered_manifest_certificate(tmp_path: Path) -> None:
    root = tmp_path / "videos"
    root.mkdir()
    _make_nonzero_media(root / "episode.mp4")
    store = Store()
    probe = CountingProbe()
    job = Job("manifest-tamper", "test")
    request = PrepareWholeSeriesSourcesRequest(
        job,
        "prepare-v1",
        ArtifactScope("pipeline", "job", job.job_key),
        1,
        _source_root(root.resolve()),
    )
    command = PrepareWholeSeriesSourcesCommand(
        store,
        builder=IdentitySourceWindowBuilder(probe_port=probe, sample_count=1),
    )
    first = command.execute(request)
    assert first.outcome.artifact_set_id is not None
    persisted = store.manifests[first.outcome.artifact_set_id]
    payload = json.loads(persisted.payload_json)
    payload["episodes"][0]["window_manifest_set"]["manifest_hashes"] = ["sha256:" + "0" * 64]
    serialized = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    store.manifests[first.outcome.artifact_set_id] = replace(
        persisted,
        reference=replace(
            persisted.reference,
            content_hash=canonical_payload_hash(serialized),
        ),
        payload_json=serialized,
    )

    with pytest.raises(SourceManifestDecodeError):
        command.execute(request)
    assert probe.calls == 1


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg and ffprobe are required",
)
def test_terminal_replay_rejects_policy_and_source_tamper_with_recomputed_hashes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "videos"
    root.mkdir()
    _make_nonzero_media(root / "episode.mp4")
    store = Store()
    job = Job("authorization-purpose-tamper", "test")
    request = PrepareWholeSeriesSourcesRequest(
        job,
        "prepare-v1",
        ArtifactScope("pipeline", "job", job.job_key),
        1,
        _source_root(root.resolve()),
    )
    command = PrepareWholeSeriesSourcesCommand(
        store,
        builder=IdentitySourceWindowBuilder(sample_count=1),
    )
    first = command.execute(request)
    assert first.outcome.artifact_set_id is not None
    persisted = store.manifests[first.outcome.artifact_set_id]
    payload = json.loads(persisted.payload_json)
    original_payload = json.loads(persisted.payload_json)
    census = payload["census"]
    census["authorized_purposes"] = ["semantic_analysis"]

    _install_recomputed_manifest_payload(store, first.outcome.artifact_set_id, payload)
    with pytest.raises(SourceManifestDecodeError):
        command.execute(request)

    source_tamper = original_payload
    source_tamper["census"]["sources"][0]["content_sha256"] = "sha256:" + "f" * 64
    source_tamper["census_sha256"] = canonical_sha256(source_tamper["census"])
    _install_recomputed_manifest_payload(
        store,
        first.outcome.artifact_set_id,
        source_tamper,
    )

    with pytest.raises(SourceManifestDecodeError):
        command.execute(request)

    policy_mapping = {
        "authorization_id": census["authorization_id"],
        "authorized_purposes": census["authorized_purposes"],
        "expected_source_count": census["expected_source_count"],
        "schema_version": census["authorization_policy_schema_version"],
        "series_id": census["series_id"],
    }
    census["authorization_policy_sha256"] = canonical_sha256(policy_mapping)
    payload["census_sha256"] = canonical_sha256(census)
    _install_recomputed_manifest_payload(store, first.outcome.artifact_set_id, payload)

    with pytest.raises(SourceManifestDecodeError):
        command.execute(request)


def _install_recomputed_manifest_payload(
    store: Store,
    artifact_set_id: UUID,
    payload: dict[str, object],
) -> None:
    persisted = store.manifests[artifact_set_id]
    serialized = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    store.manifests[artifact_set_id] = replace(
        persisted,
        reference=replace(
            persisted.reference,
            content_hash=canonical_payload_hash(serialized),
        ),
        payload_json=serialized,
    )


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg and ffprobe are required",
)
def test_terminal_replay_rejects_rehashed_audio_bound_to_foreign_source(
    tmp_path: Path,
) -> None:
    root = tmp_path / "videos"
    root.mkdir()
    _make_nonzero_media(root / "episode.mp4")
    store = Store()
    job = Job("audio-source-binding-tamper", "test")
    request = PrepareWholeSeriesSourcesRequest(
        job,
        "prepare-v1",
        ArtifactScope("pipeline", "job", job.job_key),
        1,
        _source_root(root.resolve()),
    )
    command = PrepareWholeSeriesSourcesCommand(
        store,
        builder=IdentitySourceWindowBuilder(sample_count=1),
    )
    first = command.execute(request)
    assert first.prepared is not None
    assert first.outcome.artifact_set_id is not None
    episode = first.prepared.episodes[0]
    audio = episode.media_probe.audio_sample_boundaries
    foreign_id = "source-foreign"
    foreign_sha256 = "sha256:" + "f" * 64
    tampered_audio = replace(
        audio,
        context=replace(
            audio.context,
            source_id=foreign_id,
            source_sha256=foreign_sha256,
        ),
        coverage=replace(
            audio.coverage,
            source_id=foreign_id,
            source_sha256=foreign_sha256,
        ),
        points=tuple(
            replace(point, source_id=foreign_id, source_sha256=foreign_sha256)
            for point in audio.points
        ),
    )
    # Mutate wire directly: the new writer also closes audio facts, so an
    # inconsistent typed producer projection must no longer serialize.
    tampered_payload = first.prepared.to_mapping()
    tampered_payload["episodes"][0]["media_probe"]["audio_sample_boundaries"] = (
        tampered_audio.to_mapping()
    )
    _install_recomputed_manifest_payload(
        store,
        first.outcome.artifact_set_id,
        tampered_payload,
    )

    with pytest.raises(SourceManifestDecodeError):
        command.execute(request)


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg and ffprobe are required",
)
def test_terminal_replay_rejects_rehashed_video_pts_equal_to_stream_end(
    tmp_path: Path,
) -> None:
    root = tmp_path / "videos"
    root.mkdir()
    _make_nonzero_media(root / "episode.mp4")
    store = Store()
    job = Job("video-end-pts-tamper", "test")
    request = PrepareWholeSeriesSourcesRequest(
        job,
        "prepare-v1",
        ArtifactScope("pipeline", "job", job.job_key),
        1,
        _source_root(root.resolve()),
    )
    command = PrepareWholeSeriesSourcesCommand(
        store,
        builder=IdentitySourceWindowBuilder(sample_count=1),
    )
    first = command.execute(request)
    assert first.prepared is not None
    assert first.outcome.artifact_set_id is not None
    episode = first.prepared.episodes[0]
    tampered_video_probe = replace(
        episode.media_probe.video_probe,
        pts_index=PTSIndex(
            (
                *episode.media_probe.video_probe.pts_index.ticks,
                episode.media_probe.video_range.end_pts,
            )
        ),
    )
    tampered_probe = replace(
        episode.media_probe,
        video_probe=tampered_video_probe,
    )
    policy = _identity_policy()
    tampered_manifest = replace(
        episode.manifest,
        frame_pts_index_set=_identity_frame_index(tampered_probe, policy),
        preprocess_policy_sha256=policy,
    )
    tampered_episode = replace(
        episode,
        media_probe=tampered_probe,
        manifest=tampered_manifest,
        manifest_set=replace(episode.manifest_set, manifests=(tampered_manifest,)),
    )
    tampered_prepared = replace(first.prepared, episodes=(tampered_episode,))
    _install_recomputed_manifest_payload(
        store,
        first.outcome.artifact_set_id,
        tampered_prepared.to_mapping(),
    )

    with pytest.raises(SourceManifestDecodeError):
        command.execute(request)


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg and ffprobe are required",
)
def test_terminal_replay_rejects_rehashed_presentation_frame_boundary_tamper(
    tmp_path: Path,
) -> None:
    root = tmp_path / "videos"
    root.mkdir()
    _make_nonzero_media(root / "episode.mp4")
    store = Store()
    job = Job("presentation-boundary-tamper", "test")
    request = PrepareWholeSeriesSourcesRequest(
        job,
        "prepare-v1",
        ArtifactScope("pipeline", "job", job.job_key),
        1,
        _source_root(root.resolve()),
    )
    command = PrepareWholeSeriesSourcesCommand(
        store,
        builder=IdentitySourceWindowBuilder(sample_count=1),
    )
    first = command.execute(request)
    assert first.outcome.artifact_set_id is not None
    payload = json.loads(store.manifests[first.outcome.artifact_set_id].payload_json)
    boundary = payload["episodes"][0]["media_probe"]["decoded_video_frame_boundaries"][0]
    boundary["end_tick"] += 1
    _install_recomputed_manifest_payload(store, first.outcome.artifact_set_id, payload)

    with pytest.raises(SourceManifestDecodeError):
        command.execute(request)


class RaisingBuilder:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def build(self, **kwargs):
        del kwargs
        raise self.error


@pytest.mark.parametrize(
    ("error", "state", "code"),
    [
        (SeriesCensusError("bad evidence"), "denied", "SERIES_SOURCE_CENSUS_DENIED"),
        (RuntimeError("tool/storage failure"), "failed", "SOURCE_PREPARATION_FAILED"),
    ],
)
def test_command_classifies_denied_and_failed_without_path_details(
    tmp_path: Path,
    error: Exception,
    state: str,
    code: str,
) -> None:
    root = tmp_path / "videos"
    root.mkdir()
    (root / "episode.mp4").write_bytes(b"not-empty")
    store = Store()
    job = Job(f"classification-{state}", "test")
    command = PrepareWholeSeriesSourcesCommand(store, builder=RaisingBuilder(error))  # type: ignore[arg-type]
    result = command.execute(
        PrepareWholeSeriesSourcesRequest(
            job,
            "prepare-v1",
            ArtifactScope("pipeline", "job", job.job_key),
            1,
            _source_root(root.resolve()),
        )
    )

    assert result.outcome.state == state
    assert result.outcome.failure_code == code
    assert str(root) not in (result.outcome.failure_detail_json or "")


class SyntheticVideoPort(FFprobePort):
    """Only native I/O is synthetic; the Source command and codecs are real."""

    def __init__(self):
        super().__init__(executable="synthetic-never-executed")

    def probe(self, source_path):
        del source_path
        return ProbeResult(
            VideoStreamEvidence(0, "h264", 96, 64, TimeBase(1, 1000)),
            PTSIndex((-10, 0, 10)), ToolEvidence("ffprobe", "synthetic-v1", "sha256:" + "a" * 64),
        )


class SyntheticLayoutProbe(FFprobeSourceMediaPort):
    def __init__(self, **audio_changes):
        super().__init__(executable="synthetic-never-executed", video_port=SyntheticVideoPort())
        self.audio_metadata = {
            "codec_type": "audio", "codec_name": "aac", "index": 1,
            "time_base": "1/1000", "start_pts": -10, "sample_rate": "48000", "channels": 2,
            **audio_changes,
        }
        self.calls = 0

    def _tool_identity(self):
        return "sha256:" + "a" * 64, "sha256:" + "b" * 64

    def _json(self, command):
        self.calls += 1
        if "-show_streams" in command:
            return {"streams": [
                {"codec_type": "video", "codec_name": "h264", "index": 0,
                 "time_base": "1/1000", "start_pts": -10},
                self.audio_metadata,
            ]}
        audio = command[command.index("-select_streams") + 1] == "a:0"
        return {"frames": [
            {"media_type": "audio" if audio else "video", "stream_index": 1 if audio else 0,
             "best_effort_timestamp": start, "duration": 10}
            for start in (-10, 0, 10)
        ]}


class SyntheticSampleBuilder(IdentitySourceWindowBuilder):
    def __init__(self, probe):
        super().__init__(probe_port=probe, ffmpeg_executable="synthetic-never-executed", sample_count=2)

    def _frame_sample(self, source_path, frame_index, expected_pts):
        del source_path, frame_index
        return WindowFrameSample(expected_pts, expected_pts, "sha256:" + "c" * 64)


def _synthetic_layout_command(tmp_path, **audio_changes):
    root = tmp_path / "synthetic-source"
    root.mkdir()
    (root / "episode.mp4").write_bytes(b"synthetic bytes, not an encoded media file")
    store = Store()
    job = Job("native-layout-synthetic", "test")
    request = PrepareWholeSeriesSourcesRequest(
        job, "prepare", ArtifactScope("pipeline", "job", job.job_key), 1,
        _source_root(root.resolve()),
    )
    probe = SyntheticLayoutProbe(**audio_changes)
    command = PrepareWholeSeriesSourcesCommand(store, builder=SyntheticSampleBuilder(probe))
    return store, probe, request, command


def test_synthetic_native_audio_layout_survives_exact_command_replay(tmp_path):
    store, probe, request, command = _synthetic_layout_command(tmp_path)
    first = command.execute(request)
    assert first.outcome.state == "succeeded"
    first_calls = probe.calls
    replay = command.resume(request)
    assert replay.prepared == first.prepared and probe.calls == first_calls
    facts = replay.prepared.episodes[0].media_probe.audio_stream_facts
    assert facts is not None
    assert facts.sample_rate == 48000 and facts.channels == 2
    assert facts.time_base == TimeBase(1, 1000) and facts.origin_tick == -10
    assert len(store.successes) == 1


def test_source_reuse_binds_persisted_manifest_without_origin_source_io(tmp_path):
    store, probe, request, command = _synthetic_layout_command(tmp_path)
    origin = command.execute(request)
    assert origin.outcome.state == "succeeded"
    source_calls = probe.calls
    target = Job("native-layout-synthetic-recompute", "test")
    bind_request = BindWholeSeriesSourcesRequest(
        target, "bind-source-v1", ArtifactScope("pipeline", "job", target.job_key), 1,
        request.job, origin.outcome, request.source_root.policy,
    )

    shutil.rmtree(request.source_root.root)
    first = BindWholeSeriesSourcesCommand(store).execute(bind_request)
    replay = BindWholeSeriesSourcesCommand(store).execute(bind_request)

    assert first.outcome.state == replay.outcome.state == "succeeded"
    assert first.outcome.receipt_id == replay.outcome.receipt_id
    assert first.sources is not None and replay.sources == first.sources
    assert first.sources.prepared == origin.prepared
    assert first.sources.source_job == target
    assert probe.calls == source_calls
    assert len(store.successes) == 2


def test_source_reuse_rejects_changed_policy_without_probe(tmp_path):
    store, probe, request, command = _synthetic_layout_command(tmp_path)
    origin = command.execute(request)
    assert origin.outcome.state == "succeeded"
    source_calls = probe.calls
    target = Job("native-layout-synthetic-policy-denial", "test")
    mismatched = _source_policy(
        authorization_id="other-authority", series_id=request.source_root.policy.series_id,
    )

    result = BindWholeSeriesSourcesCommand(store).execute(BindWholeSeriesSourcesRequest(
        target, "bind-source-v1", ArtifactScope("pipeline", "job", target.job_key), 1,
        request.job, origin.outcome, mismatched,
    ))

    assert result.outcome.state == "denied"
    assert result.outcome.failure_code == "SOURCE_REUSE_POLICY_MISMATCH"
    assert probe.calls == source_calls


@pytest.mark.skipif(
    psycopg is None
    or not os.environ.get("AUTOCUT_TEST_POSTGRES_DSN", "").endswith(
        "/autocut_test_source_reuse"
    ),
    reason="set AUTOCUT_TEST_POSTGRES_DSN to the dedicated autocut_test_source_reuse database",
)
def test_postgres_source_reuse_is_atomic_and_survives_origin_path_removal(tmp_path):
    dsn = os.environ["AUTOCUT_TEST_POSTGRES_DSN"]
    with psycopg.connect(dsn, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute("DROP SCHEMA IF EXISTS runtime CASCADE")
        cursor.execute("DROP SCHEMA IF EXISTS storage CASCADE")
        migrations = Path("packages/autocut-kernel/migrations")
        for migration in sorted(migrations.glob("*.sql")):
            cursor.execute(migration.read_text())

    root = tmp_path / "source-preparation-host"
    root.mkdir()
    source_path = root / "episode.mp4"
    source_bytes = b"immutable synthetic source for source-reuse postgres test"
    source_path.write_bytes(source_bytes)
    origin_job = Job("source-reuse-origin", "test")
    origin_request = PrepareWholeSeriesSourcesRequest(
        origin_job, "source-prep-v1", ArtifactScope("pipeline", "job", origin_job.job_key),
        1, _source_root(root.resolve(), "source-reuse-authority", "source-reuse-series"),
    )
    store = PostgresRuntimeStore(lambda: psycopg.connect(dsn))
    origin = PrepareWholeSeriesSourcesCommand(
        store, builder=SyntheticSampleBuilder(SyntheticLayoutProbe())
    ).execute(origin_request)
    assert origin.outcome.state == "succeeded"
    assert origin.prepared is not None
    target_job = Job("source-reuse-target", "test")
    bind_request = BindWholeSeriesSourcesRequest(
        target_job, "bind-source-v1", ArtifactScope("pipeline", "job", target_job.job_key),
        1, origin_job, origin.outcome, origin_request.source_root.policy,
    )

    shutil.rmtree(root)
    origin_proxy = origin.prepared.episodes[0].proxy_blob
    store.claim_command(
        CommandClaim(
            target_job,
            "pre-binding-read-check",
            "TestTargetJobInitialization",
            canonical_sha256({"target_job": target_job.job_key}),
            execution_kind="deterministic",
        )
    )
    with pytest.raises(BlobIntegrityError):
        store.read_immutable_blob(target_job, origin_proxy)
    first = BindWholeSeriesSourcesCommand(store).execute(bind_request)
    replay = BindWholeSeriesSourcesCommand(store).execute(bind_request)

    assert first.outcome.state == replay.outcome.state == "succeeded"
    assert first.outcome.receipt_id == replay.outcome.receipt_id
    assert first.sources is not None
    proxy = first.sources.prepared.episodes[0].proxy_blob
    assert store.read_immutable_blob(target_job, proxy) == source_bytes
    restarted = PostgresRuntimeStore(lambda: psycopg.connect(dsn))
    restored = BindWholeSeriesSourcesCommand(restarted).execute(bind_request)
    assert restored.sources == first.sources
    with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM runtime.command_receipts")
        assert cursor.fetchone() == (2,)
        cursor.execute("SELECT count(*) FROM storage.blob_claims WHERE object_id = %s", (proxy.object_id,))
        assert cursor.fetchone() == (2,)


@pytest.mark.skipif(
    psycopg is None
    or not os.environ.get("AUTOCUT_TEST_POSTGRES_DSN", "").endswith(
        "/autocut_test_source_reuse"
    ),
    reason="set AUTOCUT_TEST_POSTGRES_DSN to the dedicated autocut_test_source_reuse database",
)
@pytest.mark.asyncio
async def test_full_vlm_recompute_reuses_bound_sources_after_origin_host_removal(tmp_path):
    dsn = os.environ["AUTOCUT_TEST_POSTGRES_DSN"]
    with psycopg.connect(dsn, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute("DROP SCHEMA IF EXISTS runtime CASCADE")
        cursor.execute("DROP SCHEMA IF EXISTS storage CASCADE")
        migrations = Path("packages/autocut-kernel/migrations")
        for migration in sorted(migrations.glob("*.sql")):
            cursor.execute(migration.read_text())

    class Scheduler:
        def __init__(self) -> None:
            self.run_ids: list[str] = []

        async def enqueue(self, run_id: str) -> None:
            self.run_ids.append(run_id)

    class NoFilesystemAuthorization:
        def allows(self, _request) -> bool:
            raise AssertionError("recompute must not reauthorize the unavailable origin filesystem")

    class RejectingRootResolver:
        def resolve(self, _context):
            raise AssertionError("bound target SourcePrep must not resolve the removed origin root")

    source_root = tmp_path / "origin-host"
    source_root.mkdir()
    (source_root / "episode.mp4").write_bytes(b"cross-host full recompute source")
    policy = _source_policy(
        authorization_id="recompute-authority", series_id="recompute-series"
    )
    retry_policy = GenerationRetryPolicy(GENERATION_RETRY_STRATEGY_VERSION, 3, (1, 2))
    profile = PipelineExecutionProfile.from_semantic_policies(
        DoubaoVlmRequestPolicy("doubao-seed-2-1-pro-260628"), retry_policy=retry_policy
    )
    base_run_id = "pipeline_run_" + "a" * 32
    request = PipelineRunRequest("test", source_root=str(source_root))
    control = PostgresPipelineRunStore(lambda: psycopg.connect(dsn))
    base_claim = control._claim_run_sync(
        base_run_id, "base-run", request, request.request_hash, profile
    )
    kernel = PostgresRuntimeStore(lambda: psycopg.connect(dsn))
    origin = PrepareWholeSeriesSourcesCommand(
        kernel, builder=SyntheticSampleBuilder(SyntheticLayoutProbe())
    ).execute(
        PrepareWholeSeriesSourcesRequest(
            Job(base_run_id, "test"),
            source_prep_kernel_idempotency_key(base_run_id),
            ArtifactScope("pipeline", "job", base_run_id),
            1,
            AuthorizedSeriesSourceRoot(source_root, policy),
        )
    )
    assert origin.outcome.state == "succeeded" and origin.outcome.receipt_id is not None
    _complete_control_run_for_recompute(control, base_claim.snapshot, origin.outcome.receipt_id)
    base = control._read_run_sync(base_run_id)
    assert base is not None and base.status == "succeeded"

    shutil.rmtree(source_root)
    scheduler = Scheduler()
    service = DurablePipelineRunService(
        control,
        scheduler,
        NoFilesystemAuthorization(),
        execution_profile=profile,
        full_stage_vlm_recompute_binder=FullStageVlmRecomputeBinder(kernel),
    )
    recompute = await service.recompute_full_vlm_stage(
        VlmFullStageRecomputeRequest(base_run_id, base.version), "full-recompute-1"
    )
    assert recompute.replayed is False
    assert scheduler.run_ids == [recompute.snapshot.run_id]
    assert recompute.snapshot.request.source_root == str(source_root)

    source_command = next(
        command for command in recompute.snapshot.commands if command.stage == "source_prep"
    )
    target_context = PipelineStageContext(
        recompute.snapshot.run_id,
        recompute.snapshot.request,
        source_command,
        recompute.snapshot.execution_profile,
    )
    projected = await SourcePrepPipelineStage(kernel, RejectingRootResolver()).execute(target_context)
    assert projected.outcome == "succeeded"
    assert projected.receipt_id is not None
    target_outcome = kernel.read_outcome(
        Job(recompute.snapshot.run_id, "test"),
        source_prep_kernel_idempotency_key(recompute.snapshot.run_id),
    )
    assert target_outcome is not None
    assert kernel.is_source_reuse_binding(Job(recompute.snapshot.run_id, "test"), target_outcome)


def _complete_control_run_for_recompute(
    store: PostgresPipelineRunStore,
    snapshot,
    source_receipt_id: UUID,
) -> None:
    """Drive the control projection only; VLM data is not faked as Kernel output."""

    current = snapshot
    for position, command in enumerate(snapshot.commands):
        claimed = store._claim_next_pending_sync(
            snapshot.run_id,
            command.version,
            f"control-{position}",
        )
        assert claimed is not None and claimed.stage == command.stage
        receipt = source_receipt_id if command.stage == "source_prep" else uuid4()
        store._record_result_sync(
            snapshot.run_id,
            PipelineStageResult(claimed.command_id, "succeeded", receipt),
            claimed.version,
            f"control-{position}",
        )
        current = store._read_run_sync(snapshot.run_id)
        assert current is not None


def test_synthetic_old_absent_audio_leaf_keeps_original_payload_and_hash(tmp_path):
    store, probe, request, command = _synthetic_layout_command(tmp_path)
    result = command.execute(request)
    set_id = result.outcome.artifact_set_id
    payload = json.loads(store.manifests[set_id].payload_json)
    del payload["episodes"][0]["media_probe"]["audio_stream_facts"]
    _install_recomputed_manifest_payload(store, set_id, payload)
    persisted = store.manifests[set_id]
    calls = probe.calls
    replay = command.resume(request)
    assert replay.prepared.episodes[0].media_probe.audio_stream_facts is None
    assert replay.prepared.to_mapping() == payload
    assert canonical_sha256(replay.prepared.to_mapping()) == persisted.reference.content_hash
    assert probe.calls == calls
    assert "audio_stream_facts" not in replay.artifacts[0].payload_json


@pytest.mark.parametrize("field,value", [
    ("sample_rate", None), ("sample_rate", True), ("sample_rate", 48000),
    ("sample_rate", 48000.0), ("sample_rate", "0"), ("sample_rate", "-1"),
    ("sample_rate", "048000"), ("sample_rate", " 48000"), ("sample_rate", "48e3"),
    ("channels", None), ("channels", True), ("channels", 2.0),
    ("channels", "2"), ("channels", 0), ("channels", -1),
])
def test_synthetic_native_layout_is_required_no_guesses(tmp_path, field, value):
    store, _, request, command = _synthetic_layout_command(tmp_path, **{field: value})
    result = command.execute(request)
    assert result.outcome.state == "denied"
    assert not store.successes and not store.blobs


@pytest.mark.parametrize("field,value", [
    ("source_id", "foreign"), ("source_sha256", "sha256:" + "d" * 64),
    ("probe_execution_sha256", "sha256:" + "d" * 64),
    ("audio_sample_boundary_set_sha256", "sha256:" + "d" * 64),
    ("clock_id", "audio-stream-2"), ("end_tick", 21),
    ("selected_audio_metadata_sha256", "sha256:" + "d" * 64),
])
def test_synthetic_replay_rejects_rehashed_audio_fact_substitution(tmp_path, field, value):
    store, probe, request, command = _synthetic_layout_command(tmp_path)
    result = command.execute(request)
    set_id = result.outcome.artifact_set_id
    payload = json.loads(store.manifests[set_id].payload_json)
    payload["episodes"][0]["media_probe"]["audio_stream_facts"][field] = value
    _install_recomputed_manifest_payload(store, set_id, payload)
    calls = probe.calls
    with pytest.raises(SourceManifestDecodeError):
        command.resume(request)
    assert probe.calls == calls and len(store.successes) == 1


def test_synthetic_explicit_null_audio_leaf_is_not_historical_absence(tmp_path):
    store, _, request, command = _synthetic_layout_command(tmp_path)
    result = command.execute(request)
    set_id = result.outcome.artifact_set_id
    payload = json.loads(store.manifests[set_id].payload_json)
    payload["episodes"][0]["media_probe"]["audio_stream_facts"] = None
    _install_recomputed_manifest_payload(store, set_id, payload)
    with pytest.raises(SourceManifestDecodeError):
        command.resume(request)


@pytest.mark.parametrize("field,value", [
    ("stream_index", 2), ("time_base", {"numerator": 1, "denominator": 2000}),
    ("origin_tick", -20),
])
def test_synthetic_coherent_rehashed_metadata_still_requires_exact_probe(tmp_path, field, value):
    store, _, request, command = _synthetic_layout_command(tmp_path)
    result = command.execute(request)
    set_id = result.outcome.artifact_set_id
    payload = json.loads(store.manifests[set_id].payload_json)
    facts = payload["episodes"][0]["media_probe"]["audio_stream_facts"]
    facts[field] = value
    metadata_field = "declared_start_tick" if field == "origin_tick" else field
    facts["selected_audio_metadata"][metadata_field] = value
    if field == "stream_index":
        facts["clock_id"] = "audio-stream-2"
    facts["selected_audio_metadata_sha256"] = canonical_sha256(facts["selected_audio_metadata"])
    # Internally coherent leaf, so rejection must come from the outer probe join.
    decoded_facts = decode_audio_stream_facts(facts)
    assert decoded_facts.to_mapping() == facts
    original_probe = result.prepared.episodes[0].media_probe
    with pytest.raises(MediaValidationError, match="exact probe"):
        decoded_facts.assert_matches(
            original_probe.presentation_timeline_probe, original_probe.audio_sample_boundaries,
        )
    _install_recomputed_manifest_payload(store, set_id, payload)
    with pytest.raises(SourceManifestDecodeError):
        command.resume(request)


@pytest.mark.parametrize("field,value", [("sample_rate", "44100"), ("channels", 1)])
def test_synthetic_native_layout_changes_only_its_new_hash_not_old_presentation(tmp_path, field, value):
    store, _, request, command = _synthetic_layout_command(tmp_path)
    first = command.execute(request).prepared.episodes[0].media_probe
    changed_probe = SyntheticLayoutProbe(**{field: value}).probe(
        request.source_root.root / "episode.mp4", first.source,
    )
    old_facts, new_facts = first.audio_stream_facts, changed_probe.audio_stream_facts
    assert old_facts.selected_audio_metadata_sha256 != new_facts.selected_audio_metadata_sha256
    assert old_facts.canonical_hash != new_facts.canonical_hash
    assert old_facts.probe_execution_sha256 == new_facts.probe_execution_sha256
    assert first.audio_sample_boundaries == changed_probe.audio_sample_boundaries
    assert len(store.successes) == 1


@pytest.mark.parametrize("field", ["sample_rate", "channels"])
def test_synthetic_missing_native_layout_fields_deny_before_blob_write(tmp_path, field):
    store, probe, request, command = _synthetic_layout_command(tmp_path)
    del probe.audio_metadata[field]
    result = command.execute(request)
    assert result.outcome.state == "denied"
    assert not store.blobs and not store.successes
