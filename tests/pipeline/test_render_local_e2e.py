from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from autocut_kernel.pipeline import (
    RenderLocalDenied,
    RenderLocalFailed,
    RenderLocalRequest,
    RenderLocalSuccess,
    render_local,
)
from autocut_kernel.rendering.ffmpeg_renderer import FFmpegRenderer
from autocut_kernel.rendering.qc import LocalQC


def _digest(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _source(tmp_path: Path) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg is required")
    source = tmp_path / "source.mp4"
    completed = subprocess.run(
        [ffmpeg, "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=64x48:rate=25:duration=1", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)],
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode()
    return source


def _recipe(source: Path) -> dict[str, object]:
    digest = _digest(source)
    ticks = [0, 10240]
    evidence = {
        "source": {"sha256": digest, "byte_size": source.stat().st_size},
        "video_stream": {"stream_index": 0, "codec_name": "h264", "width": 64, "height": 48, "time_base": {"numerator": 1, "denominator": 12800}},
        "pts_index": ticks,
        "pts_index_sha256": "sha256:" + hashlib.sha256(json.dumps(ticks, separators=(",", ":")).encode()).hexdigest(),
        "validity_intervals": [{"start_pts": 0, "end_pts": 10240}],
        "ffprobe": {"executable": "ffprobe", "version": "fixture", "stderr_sha256": "sha256:" + "0" * 64},
        "fixture_id": "fixture", "fixture_manifest_sha256": "sha256:" + "1" * 64, "fixture_sidecar_sha256": "sha256:" + "2" * 64,
        "fixture_schema_version": 1, "evidence_mode": "fixture_ground_truth_v1",
    }
    return {"source": evidence["source"], "span": {"start_pts": 0, "end_pts": 10240}, "timebase": {"numerator": 1, "denominator": 12800}, "evidence": evidence}


def _request(source: Path, root: Path, recipe: object | None = None) -> RenderLocalRequest:
    return RenderLocalRequest(recipe=_recipe(source) if recipe is None else recipe, source_path=source, output_root=root, job_id="job-1", attempt_id="attempt-1")


def test_real_recipe_render_qc_and_atomic_local_pointer(tmp_path: Path) -> None:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg and ffprobe are required")
    source, root = _source(tmp_path), tmp_path / "output"

    result = render_local(_request(source, root))

    assert isinstance(result, RenderLocalSuccess)
    assert result.promotion.asset_path.is_file()
    pointer = json.loads(result.promotion.current_path.read_text())
    assert pointer["asset"]["sha256"] == result.attempt.output_sha256
    assert pointer["manifest"]["sha256"] == result.promotion.manifest_sha256
    manifest = json.loads(result.promotion.manifest_path.read_text())
    assert manifest["qc_report"]["status"] == "approved"
    assert (result.attempt.output_path.parent / "qc-report.json").is_file()


def test_invalid_recipe_denies_before_staging_or_current(tmp_path: Path) -> None:
    source, root = _source(tmp_path), tmp_path / "output"

    result = render_local(_request(source, root, {}))

    assert isinstance(result, RenderLocalDenied)
    assert result.code == "RECIPE_EMPTY"
    assert not (root / "staging").exists()
    assert not list(root.rglob("current.json")) if root.exists() else True


def test_renderer_failure_never_creates_current_pointer(tmp_path: Path) -> None:
    source, root = _source(tmp_path), tmp_path / "output"

    result = render_local(_request(source, root), renderer=FFmpegRenderer(executable="missing-ffmpeg"))

    assert isinstance(result, RenderLocalFailed)
    assert result.code == "RENDER_EXECUTION_FAILED"
    assert not list(root.rglob("current.json")) if root.exists() else True


def test_qc_failure_never_creates_current_pointer(tmp_path: Path) -> None:
    source, root = _source(tmp_path), tmp_path / "output"

    def fail(_: list[str], **__: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess([], 1, b"", b"forced QC failure")

    result = render_local(_request(source, root), qc=LocalQC(ffprobe="ffprobe", ffmpeg="ffmpeg", runner=fail))

    assert isinstance(result, RenderLocalDenied)
    assert result.code.startswith("QC_")
    assert not list(root.rglob("current.json")) if root.exists() else True
