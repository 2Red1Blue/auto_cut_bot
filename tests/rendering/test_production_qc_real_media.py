"""Real-tool compatibility tests for the production QC collector boundary."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from autocut_kernel.rendering.production_process import (
    ProductionExecutableIdentity,
    copy_pin_executable,
    create_process_pipe,
    probe_executable_version,
    resolve_executable,
    run_streaming_process,
)
from autocut_kernel.rendering.production_qc_collectors import (
    PRODUCTION_QC_COLLECTORS,
    DetectorIntervalReducer,
    MetadataPrintReducer,
    ProgressReducer,
    bind_collector_argv,
    parse_topology_json,
)
from autocut_kernel.rendering.production_qc_runner import (
    ProductionRenderQcCollectorProfile,
    ProductionRenderQcExecutionLimits,
    ProductionRenderQcPlanProjection,
    run_production_render_qc,
)
from autocut_kernel.store.models import (
    PRODUCTION_RENDER_QC_CHECK_SET_VERSION,
    BlobRef,
    Job,
    MaterializationLimits,
    ProductionRenderQcAttempt,
    ProductionRenderQcEvidenceReport,
    ProductionRenderQcLease,
)


def _sha256(value: bytes) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(value).hexdigest()


class _MaterializedFixture:
    def __init__(self, reference: BlobRef, path: Path) -> None:
        self.reference = reference
        self.path = path

    def close(self) -> None:
        self.path.unlink(missing_ok=True)


class _RealMediaStore:
    """Narrow in-memory Store port; real tools operate only on copied bytes."""

    def __init__(self, source: Path, attempt: ProductionRenderQcAttempt, root: Path) -> None:
        self.source = source
        self.attempt = attempt
        self.root = root
        self.lease: ProductionRenderQcLease | None = None
        self.evidence: list[bytes] = []
        self.report: ProductionRenderQcEvidenceReport | None = None

    def materialize_immutable_blob(
        self, job: Job, reference: BlobRef, limits: MaterializationLimits
    ) -> _MaterializedFixture:
        del job, limits
        target = (self.root / "materialized.mp4").resolve()
        target.write_bytes(self.source.read_bytes())
        return _MaterializedFixture(reference, target)

    def put_immutable_blob(
        self, job: Job, *, content: bytes, content_hash: str, media_type: str
    ) -> BlobRef:
        del job
        assert media_type == "application/json"
        assert content_hash == _sha256(content)
        self.evidence.append(content)
        return BlobRef(uuid4(), content_hash, len(content), media_type)

    def renew_production_render_qc_lease(
        self, lease: ProductionRenderQcLease, *, lease_seconds: int
    ) -> ProductionRenderQcLease:
        self.lease = replace(
            lease,
            version=lease.version + 1,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=lease_seconds),
        )
        return self.lease

    def record_production_render_qc_evidence(
        self, lease: ProductionRenderQcLease, report: ProductionRenderQcEvidenceReport
    ) -> ProductionRenderQcAttempt:
        assert self.lease == lease
        self.report = report
        return replace(
            self.attempt,
            state="evidence_ready",
            version=lease.version + 1,
            lease_expires_at=None,
            evidence_report=report,
            evidence_report_sha256=report.canonical_hash,
            evidence_ready_at=datetime.now(timezone.utc),
        )


def _media_tools() -> tuple[str, str]:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.skip("ffmpeg and ffprobe are required")
    return ffmpeg, ffprobe


def _generate_black_av_fixture(path: Path, ffmpeg: str) -> None:
    result = subprocess.run(  # noqa: S603 - exact executable and argv fixture.
        (
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=black:size=160x120:rate=12:duration=1",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=mono:d=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ),
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")


def _generate_terminal_freeze_and_silence_fixture(path: Path, ffmpeg: str) -> None:
    """Generate moving/tone media that enters both states exactly at the tail.

    The terminal states deliberately have no subsequent non-free/non-silent
    sample.  A collector may prove their observed start, but cannot invent an
    end timestamp merely because the file reached EOF.
    """

    result = subprocess.run(  # noqa: S603 - exact executable and argv fixture.
        (
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x120:rate=12:duration=0.5",
            "-f",
            "lavfi",
            "-i",
            "color=blue:size=160x120:rate=12:duration=0.75",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=0.5",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=mono:d=0.75",
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1:a=0[v];[2:a][3:a]concat=n=2:v=0:a=1[a]",
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ),
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")


def _generate_video_only_fixture(path: Path, ffmpeg: str) -> None:
    result = subprocess.run(  # noqa: S603 - exact executable and argv fixture.
        (
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x120:rate=12:duration=1",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ),
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")


def _generate_audio_only_fixture(path: Path, ffmpeg: str) -> None:
    result = subprocess.run(  # noqa: S603 - exact executable and argv fixture.
        (
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=1",
            "-vn",
            "-c:a",
            "aac",
            str(path),
        ),
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")


def test_real_ffprobe_topology_matches_closed_parser(tmp_path: Path) -> None:
    ffmpeg, ffprobe = _media_tools()
    media = (tmp_path / "fixture.mp4").absolute()
    _generate_black_av_fixture(media, ffmpeg)
    spec = PRODUCTION_QC_COLLECTORS[1]
    bound = bind_collector_argv(spec, exact_output=str(media))
    result = subprocess.run(  # noqa: S603 - exact executable and closed registry argv.
        (ffprobe, *bound[1:]),
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    topology = parse_topology_json(result.stdout)
    assert topology.indexes("video") == (0,)
    assert topology.indexes("audio") == (1,)
    assert topology.streams[0].time_base is not None
    assert topology.streams[1].sample_rate == 48_000


def test_real_ffmpeg_detector_has_metadata_eof_and_terminal_progress(
    tmp_path: Path,
) -> None:
    ffmpeg, _ffprobe = _media_tools()
    media = (tmp_path / "fixture.mp4").absolute()
    _generate_black_av_fixture(media, ffmpeg)
    detector = DetectorIntervalReducer("black")
    metadata = MetadataPrintReducer(detector)
    progress = ProgressReducer()
    progress_pipe = create_process_pipe()
    spec = PRODUCTION_QC_COLLECTORS[6]
    bound = bind_collector_argv(
        spec,
        exact_output=str(media),
        stream_index=0,
        metadata_fd=1,
        progress_fd=progress_pipe.write_fd,
    )

    result = run_streaming_process(
        (ffmpeg, *bound[1:]),
        timeout_milliseconds=30_000,
        stdout_diagnostic_max_bytes=0,
        stderr_diagnostic_max_bytes=64 * 1024,
        progress_diagnostic_max_bytes=0,
        stdout_sink=metadata.feed,
        progress_sink=progress.feed,
        progress_pipe=progress_pipe,
    )
    assert result.returncode == 0, result.stderr.diagnostic_prefix.decode(
        "utf-8", "replace"
    )
    metadata.complete()
    progress.complete()
    assert detector.channel_intervals == ((0, "0/1", None),)
    assert detector.right_censored_count == 1


def _real_profile(tmp_path: Path, ffmpeg: str, ffprobe: str) -> ProductionRenderQcCollectorProfile:
    identities: list[ProductionExecutableIdentity] = []
    for executable, name in ((ffmpeg, "ffmpeg"), (ffprobe, "ffprobe")):
        source = resolve_executable(executable, default_name=name)
        pinned = copy_pin_executable(source, (tmp_path / f"profile-{name}").resolve())
        version = probe_executable_version(pinned, timeout_milliseconds=10_000, max_bytes=64 * 1024)
        identities.append(
            ProductionExecutableIdentity(
                pinned.sha256,
                pinned.byte_length,
                version.output_sha256,
            )
        )
    return ProductionRenderQcCollectorProfile("real_ffmpeg_fixture_v1", *identities)


def _run_real_media_qc(
    tmp_path: Path,
    media: Path,
    ffmpeg: str,
    ffprobe: str,
    *,
    junction_timeline_ticks: tuple[int, ...] = (450_000,),
) -> tuple[ProductionRenderQcAttempt, _RealMediaStore]:
    content = media.read_bytes()
    profile = _real_profile(tmp_path, ffmpeg, ffprobe)
    now = datetime.now(timezone.utc)
    output = BlobRef(uuid4(), _sha256(content), len(content), "video/mp4")
    job_id = uuid4()
    slot_id = uuid4()
    render_attempt_id = uuid4()
    attempt = ProductionRenderQcAttempt(
        qc_attempt_id=uuid4(),
        render_attempt_id=render_attempt_id,
        job_id=job_id,
        command_slot_id=slot_id,
        rendered_version=2,
        output_blob=output,
        render_facts_sha256=_sha256(b"real-render-facts"),
        qc_policy_sha256=_sha256(b"real-qc-policy"),
        required_check_set_version=PRODUCTION_RENDER_QC_CHECK_SET_VERSION,
        qc_runner_identity_sha256=profile.qc_runner_identity_sha256,
        state="scanning",
        version=1,
        reserved_at=now,
        lease_expires_at=now + timedelta(minutes=10),
    )
    lease = ProductionRenderQcLease(
        attempt.qc_attempt_id,
        render_attempt_id,
        job_id,
        slot_id,
        uuid4(),
        now + timedelta(minutes=10),
        1,
    )
    store = _RealMediaStore(media, attempt, tmp_path)
    store.lease = lease
    result = run_production_render_qc(
        store,
        job=Job(f"real-qc-{uuid4()}", "production"),
        attempt=attempt,
        lease=lease,
        plan=ProductionRenderQcPlanProjection(
            attempt.render_facts_sha256, junction_timeline_ticks
        ),
        materialization_limits=MaterializationLimits(1024 * 1024, 16 * 1024 * 1024, 64, 4096),
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        profile=profile,
        execution_limits=ProductionRenderQcExecutionLimits(
            process_timeout_milliseconds=30_000,
            tool_probe_timeout_milliseconds=10_000,
            tool_version_max_bytes=64 * 1024,
            diagnostic_max_bytes=64 * 1024,
            topology_max_bytes=1024 * 1024,
            evidence_max_bytes=2 * 1024 * 1024,
            aggregate_evidence_max_bytes=16 * 1024 * 1024,
            lease_seconds=60,
        ),
    )
    return result, store


def test_real_media_runner_collects_and_attaches_the_closed_evidence_set(tmp_path: Path) -> None:
    ffmpeg, ffprobe = _media_tools()
    media = (tmp_path / "fixture.mp4").absolute()
    _generate_black_av_fixture(media, ffmpeg)
    content = media.read_bytes()
    profile = _real_profile(tmp_path, ffmpeg, ffprobe)
    now = datetime.now(timezone.utc)
    output = BlobRef(uuid4(), _sha256(content), len(content), "video/mp4")
    job_id = uuid4()
    slot_id = uuid4()
    render_attempt_id = uuid4()
    attempt = ProductionRenderQcAttempt(
        qc_attempt_id=uuid4(),
        render_attempt_id=render_attempt_id,
        job_id=job_id,
        command_slot_id=slot_id,
        rendered_version=2,
        output_blob=output,
        render_facts_sha256=_sha256(b"real-render-facts"),
        qc_policy_sha256=_sha256(b"real-qc-policy"),
        required_check_set_version=PRODUCTION_RENDER_QC_CHECK_SET_VERSION,
        qc_runner_identity_sha256=profile.qc_runner_identity_sha256,
        state="scanning",
        version=1,
        reserved_at=now,
        lease_expires_at=now + timedelta(minutes=10),
    )
    lease = ProductionRenderQcLease(
        attempt.qc_attempt_id,
        render_attempt_id,
        job_id,
        slot_id,
        uuid4(),
        now + timedelta(minutes=10),
        1,
    )
    store = _RealMediaStore(media, attempt, tmp_path)
    store.lease = lease

    result = run_production_render_qc(
        store,
        job=Job(f"real-qc-{uuid4()}", "production"),
        attempt=attempt,
        lease=lease,
        plan=ProductionRenderQcPlanProjection(attempt.render_facts_sha256, (450_000,)),
        materialization_limits=MaterializationLimits(1024 * 1024, 16 * 1024 * 1024, 64, 4096),
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        profile=profile,
        execution_limits=ProductionRenderQcExecutionLimits(
            process_timeout_milliseconds=30_000,
            tool_probe_timeout_milliseconds=10_000,
            tool_version_max_bytes=64 * 1024,
            diagnostic_max_bytes=64 * 1024,
            topology_max_bytes=1024 * 1024,
            evidence_max_bytes=2 * 1024 * 1024,
            aggregate_evidence_max_bytes=16 * 1024 * 1024,
            lease_seconds=60,
        ),
    )

    assert result.state == "evidence_ready"
    assert store.report is not None
    assert len(store.report.checks) == len(PRODUCTION_QC_COLLECTORS)
    assert len(store.evidence) == len(PRODUCTION_QC_COLLECTORS)
    assert [
        (check.check_id, check.collection_status, check.diagnostic_code)
        for check in store.report.checks
    ] == [(spec.check_id, "completed", None) for spec in PRODUCTION_QC_COLLECTORS]
    evidence = [json.loads(item) for item in store.evidence]
    assert all(item["progress_stream_byte_length"] > 0 for item in evidence[4:10])
    assert all(item["progress_stream_sha256"] != _sha256(b"") for item in evidence[4:10])
    # The black fixture reaches EOF with a right-censored interval rather than
    # silently inventing an end timestamp; its bounded example is audit data.
    assert evidence[6]["examples"] == ["black:0:0/1:right_censored"]


def test_real_media_runner_preserves_terminal_freeze_and_silence_as_censored(
    tmp_path: Path,
) -> None:
    """EOF must preserve tail states as open intervals, never close them by guesswork."""

    ffmpeg, ffprobe = _media_tools()
    media = (tmp_path / "terminal-freeze-and-silence.mp4").absolute()
    _generate_terminal_freeze_and_silence_fixture(media, ffmpeg)

    result, store = _run_real_media_qc(tmp_path, media, ffmpeg, ffprobe)

    assert result.state == "evidence_ready"
    assert store.report is not None
    evidence_by_check = {
        item["check_id"]: item for item in (json.loads(content) for content in store.evidence)
    }
    for check_id, expected_example in (
        ("video_freeze_intervals", "freeze:0:1/2:right_censored"),
        ("audio_silence_intervals", "silence:1:1/2:right_censored"),
    ):
        evidence = evidence_by_check[check_id]
        values = {item["name"]: item["value"] for item in evidence["measurements"]}
        assert values["interval_count"] == "1"
        assert values["right_censored_interval_count"] == "1"
        assert evidence["examples"] == [expected_example]


def test_real_media_runner_projects_two_junctions_from_the_exact_plan(tmp_path: Path) -> None:
    """Junction collection is a plan projection over full decoded A/V evidence."""

    ffmpeg, ffprobe = _media_tools()
    media = (tmp_path / "two-junction-av.mp4").absolute()
    _generate_black_av_fixture(media, ffmpeg)

    result, store = _run_real_media_qc(
        tmp_path,
        media,
        ffmpeg,
        ffprobe,
        junction_timeline_ticks=(150_000, 750_000),
    )

    assert result.state == "evidence_ready"
    assert store.report is not None
    checks = {check.check_id: check for check in store.report.checks}
    evidence_by_check = {
        item["check_id"]: item for item in (json.loads(content) for content in store.evidence)
    }
    decoded = checks["decoded_frame_timeline"]
    junction = checks["edit_junction_continuity"]
    assert (decoded.collection_status, decoded.coverage) == ("completed", "full_file")
    assert (junction.collection_status, junction.coverage) == ("completed", "full_file")
    assert {item.name: item.value for item in junction.measurements} == {
        "junction_count": "2",
        "observation_count": str(evidence_by_check["decoded_frame_timeline"]["record_count"]),
    }


@pytest.mark.parametrize(
    ("media_kind", "not_applicable"),
    (
        (
            "video_only",
            {
                "full_audio_decode",
                "audio_silence_intervals",
                "audio_sample_health",
                "av_presentation_envelope",
            },
        ),
        (
            "audio_only",
            {
                "full_video_decode",
                "video_black_intervals",
                "video_freeze_intervals",
                "av_presentation_envelope",
            },
        ),
    ),
)
def test_real_media_runner_marks_absent_media_family_not_applicable(
    tmp_path: Path,
    media_kind: str,
    not_applicable: set[str],
) -> None:
    """A valid one-family container must not be mislabeled as an incomplete scan."""

    ffmpeg, ffprobe = _media_tools()
    media = (tmp_path / f"{media_kind}.mp4").absolute()
    if media_kind == "video_only":
        _generate_video_only_fixture(media, ffmpeg)
    else:
        _generate_audio_only_fixture(media, ffmpeg)

    result, store = _run_real_media_qc(tmp_path, media, ffmpeg, ffprobe)

    assert result.state == "evidence_ready"
    assert store.report is not None
    statuses = {
        check.check_id: (check.collection_status, check.coverage)
        for check in store.report.checks
    }
    assert {check_id for check_id, status in statuses.items() if status == ("not_applicable", "not_applicable")} == not_applicable
    assert {
        check_id for check_id, status in statuses.items() if status == ("completed", "full_file")
    } == {spec.check_id for spec in PRODUCTION_QC_COLLECTORS} - not_applicable


def test_real_media_runner_persists_corrupt_tail_as_incomplete_evidence(tmp_path: Path) -> None:
    """A corrupted MP4 tail must yield denyable evidence, never a completed topology scan."""

    ffmpeg, ffprobe = _media_tools()
    complete = (tmp_path / "complete.mp4").absolute()
    _generate_black_av_fixture(complete, ffmpeg)
    damaged = (tmp_path / "damaged-tail.mp4").absolute()
    payload = complete.read_bytes()
    assert len(payload) > 512
    damaged.write_bytes(payload[:-256])

    result, store = _run_real_media_qc(tmp_path, damaged, ffmpeg, ffprobe)

    assert result.state == "evidence_ready"
    assert store.report is not None
    first = store.report.checks[1]
    assert first.check_id == "container_stream_topology"
    assert (first.collection_status, first.coverage) == ("incomplete", "partial")
    assert first.diagnostic_code in {"process_exit_nonzero", "parser_error"}
    assert all(
        (check.collection_status, check.coverage, check.diagnostic_code)
        == ("not_run", "none", "dependency_failed")
        for check in store.report.checks[2:]
    )
