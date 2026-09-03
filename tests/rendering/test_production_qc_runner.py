"""Deterministic unit coverage for the production QC runner boundary."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from autocut_kernel.rendering.production_process import (
    PinnedExecutable,
    ProductionExecutableIdentity,
    ProductionProcessResult,
    ProductionProcessTimeoutError,
    ProductionStreamingProcessResult,
    ProductionStreamResult,
    close_process_pipe,
)
from autocut_kernel.rendering.production_qc_runner import (
    ProductionRenderQcCancelledError,
    ProductionRenderQcCollectorProfile,
    ProductionRenderQcExecutionLimits,
    ProductionRenderQcIdentityDriftError,
    ProductionRenderQcPlanProjection,
    ProductionRenderQcRetryableError,
    run_production_render_qc,
)
from autocut_kernel.store.errors import CommandStateError
from autocut_kernel.store.models import (
    PRODUCTION_RENDER_QC_CHECK_SET_VERSION,
    PRODUCTION_RENDER_QC_REQUIRED_CHECKS,
    BlobRef,
    Job,
    MaterializationLimits,
    ProductionRenderQcAttempt,
    ProductionRenderQcEvidenceReport,
    ProductionRenderQcLease,
)


def _hash(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _stream(value: bytes = b"") -> ProductionStreamResult:
    return ProductionStreamResult(len(value), _hash(value), value[:32], len(value) > 32)


class _Materialized:
    def __init__(self, reference: BlobRef, path: Path) -> None:
        self.reference = reference
        self.path = path
        self.closed = False

    def close(self) -> None:
        self.closed = True
        self.path.unlink(missing_ok=True)


class _Store:
    def __init__(self, attempt: ProductionRenderQcAttempt, root: Path, content: bytes) -> None:
        self.attempt = attempt
        self.root = root
        self.content = content
        self.materialized: _Materialized | None = None
        self.evidence: list[bytes] = []
        self.report: ProductionRenderQcEvidenceReport | None = None
        self.record_calls = 0
        self.current_lease: ProductionRenderQcLease | None = None
        self.stale_on_record = False

    def materialize_immutable_blob(
        self, job: Job, reference: BlobRef, limits: MaterializationLimits
    ) -> _Materialized:
        del job, limits
        path = self.root / f"materialized-{uuid4()}.mp4"
        path.write_bytes(self.content)
        self.materialized = _Materialized(reference, path.resolve())
        return self.materialized

    def put_immutable_blob(
        self, job: Job, *, content: bytes, content_hash: str, media_type: str
    ) -> BlobRef:
        del job
        assert media_type == "application/json"
        assert content_hash == _hash(content)
        self.evidence.append(content)
        return BlobRef(uuid4(), content_hash, len(content), media_type)

    def renew_production_render_qc_lease(
        self, lease: ProductionRenderQcLease, *, lease_seconds: int
    ) -> ProductionRenderQcLease:
        renewed = replace(
            lease,
            version=lease.version + 1,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=lease_seconds),
        )
        self.current_lease = renewed
        return renewed

    def record_production_render_qc_evidence(
        self, lease: ProductionRenderQcLease, report: ProductionRenderQcEvidenceReport
    ) -> ProductionRenderQcAttempt:
        self.record_calls += 1
        if self.stale_on_record:
            raise CommandStateError("production render QC lease is stale")
        assert self.current_lease == lease
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


class _Processes:
    def __init__(self, *, topology: bytes, timeout_check: str | None = None) -> None:
        self.topology = topology
        self.timeout_check = timeout_check
        self.argv: list[tuple[str, ...]] = []
        self.malformed_frames = False
        self.empty_framehash_stream: str | None = None

    def bounded(
        self,
        argv: tuple[str, ...],
        *,
        timeout_milliseconds: int,
        stdout_max_bytes: int,
        stderr_max_bytes: int,
        pass_fds: tuple[int, ...],
    ) -> ProductionProcessResult:
        del timeout_milliseconds, stdout_max_bytes, stderr_max_bytes, pass_fds
        name = Path(argv[0]).name
        return ProductionProcessResult(0, f"{name} fixture\n".encode(), b"")

    def streaming(
        self,
        argv: tuple[str, ...],
        *,
        timeout_milliseconds: int,
        stdout_diagnostic_max_bytes: int,
        stderr_diagnostic_max_bytes: int,
        progress_diagnostic_max_bytes: int,
        stdout_sink,
        stderr_sink,
        progress_sink,
        progress_pipe,
        pass_fds: tuple[int, ...],
        environment,
        terminate_on_diagnostic_limit: bool,
    ) -> ProductionStreamingProcessResult:
        del (
            timeout_milliseconds,
            stdout_diagnostic_max_bytes,
            stderr_diagnostic_max_bytes,
            progress_diagnostic_max_bytes,
            stderr_sink,
            pass_fds,
            terminate_on_diagnostic_limit,
        )
        assert environment == {
            "AV_LOG_FORCE_NOCOLOR": "1",
            "LANG": "C",
            "LC_ALL": "C",
            "TZ": "UTC",
        }
        # Match the real runner ownership contract: once this callable accepts
        # ``progress_pipe``, it alone closes both descriptors on every path.
        try:
            self.argv.append(argv)
            if self.timeout_check == "packet" and "-show_packets" in argv:
                raise ProductionProcessTimeoutError("fixture timeout")

            if "-show_streams" in argv:
                output = self.topology
            elif "-show_packets" in argv:
                index = argv[argv.index("-select_streams") + 1]
                output = (
                    f"packet|stream_index={index}|pts=1|dts=1|duration=1|flags=K_\n"
                ).encode()
            elif "-show_frames" in argv:
                index = argv[argv.index("-select_streams") + 1]
                if self.malformed_frames:
                    output = b"frame|stream_index=broken\n"
                else:
                    samples = "1024" if index == "7" else "N/A"
                    output = (
                        f"frame|stream_index={index}|pts=1|pkt_dts=1|pkt_duration=1|"
                        f"best_effort_timestamp=1|nb_samples={samples}\n"
                    ).encode()
            elif "-f" in argv and argv[argv.index("-f") + 1] == "framehash":
                selected = argv[argv.index("-map") + 1].removeprefix("0:")
                output = (
                    b""
                    if selected == self.empty_framehash_stream
                    else b"0, 0, 1, 1, 1, abcdef\n"
                )
            elif any("astats=" in item for item in argv):
                output = b"frame:0 pts:0 pts_time:0\nlavfi.astats.1.Peak_level=-1.5\n"
            else:
                output = b"frame:0 pts:0 pts_time:0\n"
            if stdout_sink is not None:
                stdout_sink(output)
            progress = b""
            if progress_pipe is not None:
                progress = b"frame=1\nprogress=end\n"
                assert progress_sink is not None
                progress_sink(progress)
            return ProductionStreamingProcessResult(
                0,
                _stream(output),
                _stream(),
                _stream(progress) if progress_pipe is not None else None,
            )
        finally:
            if progress_pipe is not None:
                close_process_pipe(progress_pipe)


def _topology(*, streams: bool = True) -> bytes:
    values = []
    if streams:
        values = [
            {
                "codec_name": "h264",
                "codec_type": "video",
                "height": 720,
                "index": 3,
                "nb_read_packets": "1",
                "pix_fmt": "yuv420p",
                "time_base": "1/90000",
                "width": 1280,
            },
            {
                "channel_layout": "stereo",
                "channels": 2,
                "codec_name": "aac",
                "codec_type": "audio",
                "index": 7,
                "nb_read_packets": "1",
                "sample_rate": "48000",
                "time_base": "1/48000",
            },
        ]
    return json.dumps(
        {
            "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2"},
            "programs": [],
            "stream_groups": [],
            "streams": values,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _case(tmp_path: Path, *, media: bytes = b"exact-media"):
    ffmpeg = tmp_path / "ffmpeg-source"
    ffprobe = tmp_path / "ffprobe-source"
    ffmpeg.write_bytes(b"ffmpeg-executable")
    ffprobe.write_bytes(b"ffprobe-executable")
    ffmpeg.chmod(0o700)
    ffprobe.chmod(0o700)
    # The runner pins to canonical names, and the version fixture keys on those names.
    ffmpeg_identity = ProductionExecutableIdentity(
        _hash(ffmpeg.read_bytes()), len(ffmpeg.read_bytes()), _hash(b"ffmpeg fixture\n\0")
    )
    ffprobe_identity = ProductionExecutableIdentity(
        _hash(ffprobe.read_bytes()), len(ffprobe.read_bytes()), _hash(b"ffprobe fixture\n\0")
    )
    profile = ProductionRenderQcCollectorProfile(
        "fixture_profile_v1", ffmpeg_identity, ffprobe_identity
    )
    output = BlobRef(uuid4(), _hash(media), len(media), "video/mp4")
    now = datetime.now(timezone.utc)
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
        render_facts_sha256=_hash(b"render-facts"),
        qc_policy_sha256=_hash(b"qc-policy"),
        required_check_set_version=PRODUCTION_RENDER_QC_CHECK_SET_VERSION,
        qc_runner_identity_sha256=profile.qc_runner_identity_sha256,
        state="scanning",
        version=1,
        reserved_at=now,
        lease_expires_at=now + timedelta(minutes=5),
    )
    lease = ProductionRenderQcLease(
        attempt.qc_attempt_id,
        render_attempt_id,
        job_id,
        slot_id,
        uuid4(),
        now + timedelta(minutes=5),
        1,
    )
    store = _Store(attempt, tmp_path, media)
    store.current_lease = lease
    return {
        "store": store,
        "job": Job(f"qc-runner-{uuid4()}", "production"),
        "attempt": attempt,
        "lease": lease,
        "plan": ProductionRenderQcPlanProjection(attempt.render_facts_sha256, (100,)),
        "materialization_limits": MaterializationLimits(1024, 1024, 64, 4096),
        "ffmpeg_path": str(ffmpeg),
        "ffprobe_path": str(ffprobe),
        "profile": profile,
        "execution_limits": ProductionRenderQcExecutionLimits(
            process_timeout_milliseconds=1000,
            tool_probe_timeout_milliseconds=1000,
            tool_version_max_bytes=4096,
            diagnostic_max_bytes=1024,
            topology_max_bytes=4096,
            evidence_max_bytes=4096,
            aggregate_evidence_max_bytes=12 * 4096,
            lease_seconds=60,
            renew_every_operations=3,
        ),
    }


def _run(case, processes: _Processes, **overrides):
    arguments = dict(case)
    arguments.update(overrides)
    arguments["streaming_runner"] = processes.streaming
    arguments["bounded_runner"] = processes.bounded
    return run_production_render_qc(**arguments)


def test_success_records_all_checks_in_order_with_path_free_bounded_evidence(
    tmp_path: Path,
) -> None:
    case = _case(tmp_path)
    processes = _Processes(topology=_topology())

    result = _run(case, processes)

    store = case["store"]
    assert result.state == "evidence_ready"
    assert store.record_calls == 1
    assert store.report is not None
    assert (
        tuple(check.check_id for check in store.report.checks)
        == PRODUCTION_RENDER_QC_REQUIRED_CHECKS
    )
    assert all(check.collection_status == "completed" for check in store.report.checks)
    assert all(len(body) <= 4096 for body in store.evidence)
    evidence = [json.loads(body) for body in store.evidence]
    assert all(
        set(item) >= {
            "examples",
            "progress_stream_byte_length",
            "progress_stream_sha256",
            "stream_byte_length",
            "stream_sha256",
        }
        for item in evidence
    )
    full_video_decode = evidence[4]
    assert full_video_decode["examples"]
    assert full_video_decode["progress_stream_byte_length"] > 0
    assert full_video_decode["progress_stream_sha256"] != _hash(b"")
    for body in store.evidence:
        decoded = body.decode()
        assert str(tmp_path) not in decoded
        assert "locator" not in decoded
        assert "stderr" not in decoded
        assert "pass" not in decoded and "fail" not in decoded
    assert all("v:0" not in item and "a:0" not in item for argv in processes.argv for item in argv)
    assert all(
        "-ss" not in argv and "-t" not in argv and "-to" not in argv for argv in processes.argv
    )
    selectors = [
        argv[argv.index("-select_streams") + 1]
        for argv in processes.argv
        if "-select_streams" in argv
    ]
    assert selectors == ["3", "7", "3", "7"]
    maps = [argv[argv.index("-map") + 1] for argv in processes.argv if "-map" in argv]
    assert set(maps) == {"0:3", "0:7"}
    assert store.materialized is not None and store.materialized.closed
    assert not store.materialized.path.exists()
    assert all(not Path(argv[0]).exists() for argv in processes.argv)


def test_exact_identity_mismatch_blocks_every_tool_scan(tmp_path: Path) -> None:
    case = _case(tmp_path, media=b"expected")
    case["store"].content = b"substitute"
    processes = _Processes(topology=_topology())

    _run(case, processes)

    report = case["store"].report
    assert report is not None
    assert processes.argv == []
    assert report.checks[0].collection_status == "incomplete"
    assert report.checks[0].coverage == "none"
    assert report.checks[0].diagnostic_code == "exact_object_identity_mismatch"
    assert [item.collection_status for item in report.checks[1:]] == ["not_run"] * 11


def test_every_mapped_stream_requires_nonempty_framehash_output(tmp_path: Path) -> None:
    case = _case(tmp_path)
    topology = json.loads(_topology())
    topology["streams"].append(
        {
            "codec_name": "h264",
            "codec_type": "video",
            "height": 720,
            "index": 5,
            "nb_read_packets": "1",
            "pix_fmt": "yuv420p",
            "time_base": "1/90000",
            "width": 1280,
        }
    )
    processes = _Processes(
        topology=json.dumps(topology, separators=(",", ":"), sort_keys=True).encode()
    )
    processes.empty_framehash_stream = "5"

    _run(case, processes)

    report = case["store"].report
    assert report is not None
    by_id = {check.check_id: check for check in report.checks}
    assert by_id["full_video_decode"].collection_status == "incomplete"
    assert by_id["full_video_decode"].coverage == "partial"
    assert by_id["full_video_decode"].diagnostic_code == "empty_mapped_decode"
    assert by_id["video_black_intervals"].collection_status == "not_run"
    assert by_id["video_freeze_intervals"].collection_status == "not_run"
    decode_evidence = json.loads(case["store"].evidence[4])
    assert decode_evidence["progress_stream_byte_length"] > 0
    assert decode_evidence["progress_stream_sha256"] != _hash(b"")
    assert decode_evidence["examples"]


def test_zero_streams_and_zero_junction_are_not_applicable(tmp_path: Path) -> None:
    case = _case(tmp_path)
    case["plan"] = ProductionRenderQcPlanProjection(case["attempt"].render_facts_sha256, ())
    processes = _Processes(topology=_topology(streams=False))

    _run(case, processes)

    report = case["store"].report
    assert report is not None
    statuses = {check.check_id: check.collection_status for check in report.checks}
    assert statuses["packet_timeline_integrity"] == "completed"
    assert statuses["decoded_frame_timeline"] == "completed"
    for check_id in PRODUCTION_RENDER_QC_REQUIRED_CHECKS[4:]:
        assert statuses[check_id] == "not_applicable"


def test_strict_parser_error_is_durable_incomplete_and_propagates_dependencies(
    tmp_path: Path,
) -> None:
    case = _case(tmp_path)
    processes = _Processes(topology=_topology())
    processes.malformed_frames = True

    _run(case, processes)

    report = case["store"].report
    assert report is not None
    by_id = {check.check_id: check for check in report.checks}
    assert by_id["decoded_frame_timeline"].collection_status == "incomplete"
    assert by_id["decoded_frame_timeline"].diagnostic_code == "parser_error"
    assert by_id["av_presentation_envelope"].collection_status == "not_run"
    assert by_id["edit_junction_continuity"].collection_status == "not_run"
    assert by_id["full_video_decode"].collection_status == "completed"


def test_timeout_is_retryable_and_never_attaches_a_report(tmp_path: Path) -> None:
    case = _case(tmp_path)
    processes = _Processes(topology=_topology(), timeout_check="packet")

    with pytest.raises(ProductionRenderQcRetryableError, match="timeout"):
        _run(case, processes)

    store = case["store"]
    assert store.record_calls == 0
    assert store.report is None
    assert store.materialized is not None and store.materialized.closed


def test_stale_lease_rejects_the_single_final_attachment(tmp_path: Path) -> None:
    case = _case(tmp_path)
    case["store"].stale_on_record = True
    processes = _Processes(topology=_topology())

    with pytest.raises(CommandStateError, match="stale"):
        _run(case, processes)

    store = case["store"]
    assert store.record_calls == 1
    assert store.report is None
    assert store.materialized is not None and store.materialized.closed


def test_injected_heartbeat_is_used_and_cancellation_never_attaches(tmp_path: Path) -> None:
    case = _case(tmp_path)
    processes = _Processes(topology=_topology())
    renewals: list[int] = []

    def heartbeat(lease: ProductionRenderQcLease) -> ProductionRenderQcLease:
        renewals.append(lease.version)
        renewed = replace(
            lease,
            version=lease.version + 1,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
        )
        case["store"].current_lease = renewed
        return renewed

    _run(case, processes, heartbeat=heartbeat)
    # Every external FFprobe/FFmpeg scan is preceded by a forced lease renewal;
    # ordinary Store checkpoints may add further renewals.
    assert len(renewals) >= len(processes.argv)

    cancelled_root = tmp_path / "cancelled"
    cancelled_root.mkdir()
    cancelled_case = _case(cancelled_root)
    cancelled_processes = _Processes(topology=_topology())
    with pytest.raises(ProductionRenderQcCancelledError, match="cancelled"):
        _run(cancelled_case, cancelled_processes, cancelled=lambda: True)
    assert cancelled_case["store"].record_calls == 0


def test_external_process_timeout_must_fit_inside_renewed_lease_budget() -> None:
    with pytest.raises(ValueError, match="lease safety budget"):
        ProductionRenderQcExecutionLimits(
            process_timeout_milliseconds=45_001,
            tool_probe_timeout_milliseconds=1_000,
            tool_version_max_bytes=4096,
            diagnostic_max_bytes=1024,
            topology_max_bytes=4096,
            evidence_max_bytes=4096,
            aggregate_evidence_max_bytes=12 * 4096,
            lease_seconds=60,
        )


def test_cleanup_reverification_failure_is_not_swallowed(tmp_path: Path) -> None:
    case = _case(tmp_path)
    processes = _Processes(topology=_topology())
    calls: dict[UUID, int] = {}

    def reverify(executable: PinnedExecutable) -> None:
        key = UUID(int=int(executable.sha256[7:39], 16))
        calls[key] = calls.get(key, 0) + 1
        if sum(calls.values()) == 3:
            raise RuntimeError("fixture identity drift")

    # Two tools are reverified before attachment and again in finally.
    with pytest.raises(ProductionRenderQcIdentityDriftError, match="reverification"):
        _run(case, processes, executable_reverifier=reverify)
    assert sum(calls.values()) == 4
