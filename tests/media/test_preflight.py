"""Tests for fail-closed ffprobe-backed fixture evidence."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from autocut_kernel.media.ffprobe_port import FFprobePort, FFprobePtsIndexError
from autocut_kernel.media.preflight import (
    FixtureEvidenceError,
    MediaPreflightRequest,
    preflight,
    preflight_fixture,
)


def _completed(stdout: object, *, stderr: bytes = b"") -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(["ffprobe"], 0, json.dumps(stdout).encode(), stderr)


def _metadata() -> dict[str, object]:
    return {
        "streams": [
            {"codec_type": "video", "codec_name": "mpeg4", "index": 0, "width": 64, "height": 48, "time_base": "1/10240"}
        ]
    }


def test_port_uses_only_strict_decimal_best_effort_timestamps() -> None:
    calls: list[list[str]] = []

    def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(command)
        if "-show_streams" in command:
            return _completed(_metadata())
        if "-show_frames" in command:
            return _completed({"frames": [{"media_type": "video", "stream_index": 0, "best_effort_timestamp": "0"}, {"media_type": "video", "stream_index": 0, "best_effort_timestamp": "1024"}]})
        return subprocess.CompletedProcess(command, 0, b"ffprobe version mock\n", b"")

    result = FFprobePort("ffprobe", runner=runner).probe(Path("fixture.mp4"))

    assert result.pts_index.ticks == (0, 1024)
    assert "-of" in calls[0] and "json" in calls[0]
    assert "-show_frames" in calls[1] and any("best_effort_timestamp" in item for item in calls[1])


def test_port_resolves_a_leading_dash_source_path_after_option_terminator() -> None:
    calls: list[list[str]] = []

    def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(command)
        if "-show_streams" in command:
            return _completed(_metadata())
        if "-show_frames" in command:
            return _completed({"frames": [{"media_type": "video", "stream_index": 0, "best_effort_timestamp": 0}]})
        return subprocess.CompletedProcess(command, 0, b"ffprobe version mock\n", b"")

    FFprobePort("ffprobe", runner=runner).probe(Path("-fixture.mp4"))

    for command in calls[:2]:
        assert command[-2] == "--"
        assert Path(command[-1]).is_absolute()
        assert Path(command[-1]).name == "-fixture.mp4"


@pytest.mark.parametrize("timestamp", [None, True, 0.0, "0.0", "1e3", "+1"])
def test_port_rejects_non_decimal_or_float_endpoint(timestamp: object) -> None:
    def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        if "-show_streams" in command:
            return _completed(_metadata())
        return _completed({"frames": [{"media_type": "video", "stream_index": 0, "best_effort_timestamp": timestamp}]})

    with pytest.raises(FFprobePtsIndexError):
        FFprobePort("ffprobe", runner=runner).probe(Path("fixture.mp4"))


@pytest.mark.parametrize("ticks", [("0", "0"), ("1024", "0")])
def test_port_rejects_duplicate_or_unordered_pts(ticks: tuple[str, str]) -> None:
    def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        if "-show_streams" in command:
            return _completed(_metadata())
        return _completed(
            {
                "frames": [
                    {"media_type": "video", "stream_index": 0, "best_effort_timestamp": ticks[0]},
                    {"media_type": "video", "stream_index": 0, "best_effort_timestamp": ticks[1]},
                ]
            }
        )

    with pytest.raises(FFprobePtsIndexError):
        FFprobePort("ffprobe", runner=runner).probe(Path("fixture.mp4"))


def _sha256_prefixed(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _write_canonical(path: Path, payload: dict[str, object]) -> str:
    raw = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    path.write_bytes(raw)
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _canonical_sha256(payload: object) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg and ffprobe are required",
)
def test_actual_ffprobe_preflight_accepts_zero_pts_and_validates_fixture_provenance(tmp_path: Path) -> None:
    source_path = tmp_path / "controlled.mp4"
    completed = subprocess.run(
        [
            str(shutil.which("ffmpeg")), "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
            "-i", "testsrc2=size=64x48:rate=10", "-frames:v", "12", "-an", "-c:v", "mpeg4", "-q:v", "5",
            "-pix_fmt", "yuv420p", str(source_path),
        ],
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    probe = FFprobePort().probe(source_path)
    assert probe.pts_index.ticks[0] == 0

    source = {"content_sha256": _sha256_prefixed(source_path), "byte_size": source_path.stat().st_size}
    manifest = {
        "fixture_id": "controlled-fixture-v1",
        "profile": "test",
        "schema_version": 1,
        "source": source,
        "sidecar": {},
    }
    manifest_binding = {
        "fixture_id": manifest["fixture_id"],
        "profile": manifest["profile"],
        "schema_version": manifest["schema_version"],
        "source": source,
    }
    sidecar = {
        "fixture_id": "controlled-fixture-v1",
        "profile": "test",
        "schema_version": 1,
        "evidence_mode": "fixture_ground_truth_v1",
        "source": source,
        "pts_index_sha256": f"sha256:{hashlib.sha256(json.dumps(list(probe.pts_index.ticks), sort_keys=True, separators=(',', ':')).encode()).hexdigest()}",
        "validity_intervals": [{"start_pts": probe.pts_index.ticks[0], "end_pts": probe.pts_index.ticks[-1]}],
        "manifest_hash_binding": {
            "representation": "canonical_manifest_without_sidecar_sha256_v1",
            "sha256": _canonical_sha256(manifest_binding),
        },
        "ground_truth": {
            "exact_pts": {
                "representation": "integer_pts_index",
                "time_base": f"{probe.video_stream.time_base.numerator}/{probe.video_stream.time_base.denominator}",
                "values": list(probe.pts_index.ticks),
            }
        },
    }
    sidecar_path = tmp_path / "controlled.sidecar.json"
    sidecar_sha256 = _write_canonical(sidecar_path, sidecar)
    manifest["sidecar"] = {"sha256": sidecar_sha256}
    manifest_path = tmp_path / "controlled.manifest.json"
    _write_canonical(manifest_path, manifest)

    request = MediaPreflightRequest(
        profile="test",
        source_path=source_path,
        fixture_id="controlled-fixture-v1",
        expected_source_sha256=_sha256_prefixed(source_path),
        manifest_path=manifest_path,
        sidecar_path=sidecar_path,
    )
    evidence = preflight_fixture(request)

    assert evidence.pts_index.ticks[0] == 0
    assert evidence.validity_intervals.intervals[0].start_pts == 0
    assert evidence.source.byte_size == source_path.stat().st_size

    manifest["profile"] = "shadow"
    _write_canonical(manifest_path, manifest)
    with pytest.raises(FixtureEvidenceError, match="profiles must match"):
        preflight_fixture(request)


def test_production_fixture_is_denied_before_probe_or_source_read(tmp_path: Path) -> None:
    request = MediaPreflightRequest(
        profile="production",
        source_path=tmp_path / "missing.mp4",
        fixture_id="fixture",
        expected_source_sha256="sha256:" + "0" * 64,
        manifest_path=tmp_path / "missing-manifest.json",
        sidecar_path=tmp_path / "missing-sidecar.json",
    )

    result = preflight(request)

    assert result.denial is not None
    assert result.denial.code == "TEST_FIXTURE_PROFILE_FORBIDDEN"
