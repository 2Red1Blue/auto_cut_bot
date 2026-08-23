from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from autocut_kernel.rendering import build_render_plan, parse_recipe
from autocut_kernel.rendering.ffmpeg_renderer import (
    FFmpegExecutionError,
    FFmpegRenderer,
    SourceIdentityMismatchError,
)


def _digest(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _source(tmp_path: Path) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg is required")
    path = tmp_path / "source.mp4"
    completed = subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=64x48:rate=25:duration=1", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], capture_output=True, check=False)
    assert completed.returncode == 0, completed.stderr.decode()
    return path


def _recipe(path: Path) -> dict[str, object]:
    digest = _digest(path)
    ticks = [0, 10240]
    evidence = {
        "source": {"sha256": digest, "byte_size": path.stat().st_size},
        "video_stream": {"stream_index": 0, "codec_name": "h264", "width": 64, "height": 48, "time_base": {"numerator": 1, "denominator": 12800}},
        "pts_index": ticks,
        "pts_index_sha256": "sha256:" + hashlib.sha256(json.dumps(ticks, separators=(",", ":")).encode()).hexdigest(),
        "validity_intervals": [{"start_pts": 0, "end_pts": 10240}],
        "ffprobe": {"executable": "ffprobe", "version": "fixture", "stderr_sha256": "sha256:" + "0" * 64},
        "fixture_id": "fixture", "fixture_manifest_sha256": "sha256:" + "1" * 64, "fixture_sidecar_sha256": "sha256:" + "2" * 64,
        "fixture_schema_version": 1, "evidence_mode": "fixture_ground_truth_v1",
    }
    return {"source": {"sha256": digest, "byte_size": path.stat().st_size}, "span": {"start_pts": 0, "end_pts": 10240}, "timebase": {"numerator": 1, "denominator": 12800}, "evidence": evidence}


def test_renderer_creates_hashed_nonempty_staged_attempt(tmp_path: Path) -> None:
    source = _source(tmp_path)
    recipe = parse_recipe(_recipe(source), expected_source_sha256=_digest(source), profile="test")
    plan = build_render_plan(recipe, source_path=source, output_path=tmp_path / "ignored.mp4")
    attempt = FFmpegRenderer().render(recipe, plan, source_path=source, staging_root=tmp_path / "staging")
    assert attempt.output_path.is_file()
    assert attempt.output_path.parent.parent == (tmp_path / "staging").resolve()
    assert attempt.output_byte_size == attempt.output_path.stat().st_size
    assert attempt.output_sha256 == _digest(attempt.output_path)
    assert attempt.ffmpeg_argv[0] == "ffmpeg"
    assert "-ss" not in attempt.ffmpeg_argv and "-to" not in attempt.ffmpeg_argv


def test_renderer_rejects_source_hash_before_execution(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    recipe = parse_recipe(_recipe(source), expected_source_sha256=_digest(source), profile="test")
    source.write_bytes(b"change")
    plan = build_render_plan(recipe, source_path=source, output_path=tmp_path / "out.mp4")
    with pytest.raises(SourceIdentityMismatchError, match="before rendering"):
        FFmpegRenderer(executable="ffmpeg", runner=lambda *_args, **_kwargs: pytest.fail("must not run")).render(recipe, plan, source_path=source, staging_root=tmp_path / "staging")


def test_renderer_reports_mocked_ffmpeg_failure(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    recipe = parse_recipe(_recipe(source), expected_source_sha256=_digest(source), profile="test")
    plan = build_render_plan(recipe, source_path=source, output_path=tmp_path / "out.mp4")

    def failed(command: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(command, 1, b"", b"bad media")

    with pytest.raises(FFmpegExecutionError, match="bad media"):
        FFmpegRenderer(executable="fake-ffmpeg", runner=failed).render(recipe, plan, source_path=source, staging_root=tmp_path / "staging")
