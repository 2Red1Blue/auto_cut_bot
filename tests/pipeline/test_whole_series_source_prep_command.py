from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from autocut_kernel.media import PTSIndex
from autocut_kernel.pipeline import GenerateVlmEvidenceRequest
from autocut_kernel.store import (
    ArtifactScope,
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
)

from auto_cut_bot.pipeline.source_prep import (
    AuthorizedSeriesSourceRoot,
    FFprobeSourceMediaPort,
    IdentitySourceWindowBuilder,
    PrepareWholeSeriesSourcesCommand,
    PrepareWholeSeriesSourcesRequest,
    PrepareWholeSeriesSourcesResult,
)
from auto_cut_bot.pipeline.source_prep.command import (
    FrameSampleEvidenceError,
    SourceManifestDecodeError,
    _identity_frame_index,
    _identity_policy,
)
from auto_cut_bot.pipeline.source_prep.models import SeriesCensusError, SeriesSource
from auto_cut_bot.pipeline.source_prep.probe import SourceMediaEvidenceError


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
            self.blob_refs[episode["proxy_blob"]["content_hash"]]
            for episode in payload["episodes"]
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
        AuthorizedSeriesSourceRoot(root.resolve(), "fixture-authority", "fixture-series", 1),
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
    episode = replay.prepared.episodes[0]
    assert episode.media_probe.video_range.start_pts > 0
    frame_ticks = episode.media_probe.video_probe.pts_index.ticks
    assert len({right - left for left, right in zip(frame_ticks, frame_ticks[1:])}) > 1
    assert episode.manifest.source_range == episode.media_probe.video_range
    assert episode.manifest.timeline_map.certificate_kind == "translation_certificate"
    assert episode.manifest_set.manifests == (episode.manifest,)
    assert episode.media_probe.audio_sample_boundaries.points[0].tick == (
        episode.media_probe.audio_sample_boundaries.context.origin_tick
    )
    assert episode.media_probe.audio_sample_boundaries.points[-1].tick == (
        episode.media_probe.audio_sample_boundaries.context.end_tick
    )
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
        VlmParsePolicy(Decimal("0.5"), 100_000, 10, 100, 1_000),
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
            AuthorizedSeriesSourceRoot(root.resolve(), "authority", "series", 1),
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
        AuthorizedSeriesSourceRoot(root.resolve(), "authority", "series", 1),
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
        AuthorizedSeriesSourceRoot(root.resolve(), "authority", "series", 1),
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
    not os.environ.get("AUTOCUT_TEST_POSTGRES_DSN")
    or shutil.which("ffmpeg") is None
    or shutil.which("ffprobe") is None,
    reason="disposable PostgreSQL, ffmpeg and ffprobe are required",
)
def test_crash_after_partial_blob_concurrent_exact_replays_converge(
    tmp_path: Path,
) -> None:
    import psycopg

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
        AuthorizedSeriesSourceRoot(root.resolve(), "authority", "series", 1),
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
    not os.environ.get("AUTOCUT_TEST_POSTGRES_DSN"),
    reason="set AUTOCUT_TEST_POSTGRES_DSN for disposable PostgreSQL integration",
)
def test_real_episode_persists_receipt_artifact_set_blob_and_replays(tmp_path: Path) -> None:
    import psycopg

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
        AuthorizedSeriesSourceRoot(root.resolve(), "authority", "series", 1),
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
def test_audio_boundaries_reject_empty_or_discontinuous_frames(
    frames: list[dict[str, object]],
) -> None:
    source = SeriesSource("episode.mp4", "source-a", "sha256:" + "1" * 64, 1)
    with pytest.raises(SourceMediaEvidenceError):
        AudioFramesProbe(frames)._audio_boundaries(
            Path("unused.mp4"),
            source,
            {"index": 1, "time_base": "1/48000", "start_pts": 0, "duration_ts": 20},
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
        AuthorizedSeriesSourceRoot(root.resolve(), "authority", "series", 1),
    )
    command = PrepareWholeSeriesSourcesCommand(
        store,
        builder=IdentitySourceWindowBuilder(probe_port=probe, sample_count=1),
    )
    first = command.execute(request)
    assert first.outcome.artifact_set_id is not None
    persisted = store.manifests[first.outcome.artifact_set_id]
    payload = json.loads(persisted.payload_json)
    payload["episodes"][0]["window_manifest_set"]["manifest_hashes"] = [
        "sha256:" + "0" * 64
    ]
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
        AuthorizedSeriesSourceRoot(root.resolve(), "authority", "series", 1),
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
    tampered_probe = replace(
        episode.media_probe,
        audio_sample_boundaries=tampered_audio,
    )
    tampered_episode = replace(episode, media_probe=tampered_probe)
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
        AuthorizedSeriesSourceRoot(root.resolve(), "authority", "series", 1),
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
    policy = _identity_policy(tampered_probe)
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
            AuthorizedSeriesSourceRoot(root.resolve(), "authority", "series", 1),
        )
    )

    assert result.outcome.state == state
    assert result.outcome.failure_code == code
    assert str(root) not in (result.outcome.failure_detail_json or "")
