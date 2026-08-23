from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from autocut_kernel.rendering import Recipe, build_render_plan, parse_recipe
from autocut_kernel.rendering.ffmpeg_renderer import FFmpegRenderer, RenderAttempt
from autocut_kernel.rendering.qc import LocalQC


def _digest(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _recipe(path: Path, *, end_pts: int = 10240) -> dict[str, object]:
    digest = _digest(path)
    ticks = [0, end_pts]
    evidence = {
        "source": {"sha256": digest, "byte_size": path.stat().st_size},
        "video_stream": {"stream_index": 0, "codec_name": "h264", "width": 64, "height": 48, "time_base": {"numerator": 1, "denominator": 12800}},
        "pts_index": ticks,
        "pts_index_sha256": "sha256:" + hashlib.sha256(json.dumps(ticks, separators=(",", ":")).encode()).hexdigest(),
        "validity_intervals": [{"start_pts": 0, "end_pts": end_pts}],
        "ffprobe": {"executable": "ffprobe", "version": "fixture", "stderr_sha256": "sha256:" + "0" * 64},
        "fixture_id": "fixture", "fixture_manifest_sha256": "sha256:" + "1" * 64, "fixture_sidecar_sha256": "sha256:" + "2" * 64,
        "fixture_schema_version": 1, "evidence_mode": "fixture_ground_truth_v1",
    }
    return {"source": {"sha256": digest, "byte_size": path.stat().st_size}, "span": {"start_pts": 0, "end_pts": end_pts}, "timebase": {"numerator": 1, "denominator": 12800}, "evidence": evidence}


@pytest.fixture
def rendered(tmp_path: Path) -> tuple[Recipe, RenderAttempt]:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg and ffprobe are required")
    source = tmp_path / "source.mp4"
    completed = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=64x48:rate=25:duration=1", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)], capture_output=True, check=False)
    assert completed.returncode == 0, completed.stderr.decode()
    raw = _recipe(source)
    recipe = parse_recipe(raw, expected_source_sha256=_digest(source), profile="test")
    plan = build_render_plan(recipe, source_path=source, output_path=tmp_path / "ignored.mp4")
    return recipe, FFmpegRenderer().render(recipe, plan, source_path=source, staging_root=tmp_path / "staging")


def test_qc_approves_a_real_video_only_h264_mp4(rendered: tuple[Recipe, RenderAttempt]) -> None:
    recipe, attempt = rendered
    report = LocalQC().inspect(recipe, attempt)
    assert report.status == "approved"
    assert report.approved
    assert {check.name for check in report.checks} == {"regular_nonempty_digest", "h264_mp4_video_only", "full_decode", "deterministic_frame_evidence", "coarse_black_freeze_guard"}
    assert report.to_manifest()["status"] == "approved"


def test_qc_rejects_when_actual_output_digest_changed(rendered: tuple[Recipe, RenderAttempt]) -> None:
    recipe, attempt = rendered
    attempt.output_path.write_bytes(attempt.output_path.read_bytes() + b"tampered")
    report = LocalQC().inspect(recipe, attempt)
    assert report.status == "rejected"
    assert not next(check for check in report.checks if check.name == "regular_nonempty_digest").passed


def test_qc_reports_mocked_probe_error_without_a_caller_pass_flag(tmp_path: Path) -> None:
    output = tmp_path / "output.mp4"
    output.write_bytes(b"output")
    digest = _digest(output)
    recipe = parse_recipe(_recipe(output, end_pts=1), expected_source_sha256=digest, profile="test")
    attempt = RenderAttempt(recipe.canonical_hash, "sha256:" + "1" * 64, digest, output, digest, 6, ("ffmpeg", "-i", "x", "y"), "sha256:" + "2" * 64)

    def failed(command: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(command, 1, b"", b"broken")

    report = LocalQC(ffprobe="ffprobe", ffmpeg="ffmpeg", runner=failed).inspect(recipe, attempt)
    assert report.status == "rejected"
    assert not next(check for check in report.checks if check.name == "h264_mp4_video_only").passed
