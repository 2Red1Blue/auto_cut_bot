"""Regression coverage for the local Media fixture corpus."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess

import pytest
from autocut_kernel.media.preflight import MediaPreflightRequest, preflight_fixture

from tests.media.fixture_corpus import (
    ffmpeg_available,
    ffprobe_available,
    load_corpus_spec,
    register_fixture_corpus,
)


def _sha256(path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _independent_video_probe(source_path) -> tuple[str, list[int]]:
    """Perform a second ffprobe read instead of trusting corpus serialization."""
    ffprobe = shutil.which("ffprobe")
    assert ffprobe is not None
    completed = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=time_base:frame=media_type,best_effort_timestamp",
            "-show_frames",
            "-of",
            "json",
            str(source_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(completed.stdout)
    return payload["streams"][0]["time_base"], [
        frame["best_effort_timestamp"]
        for frame in payload["frames"]
        if frame["media_type"] == "video"
    ]


def test_corpus_spec_declares_ffprobe_as_the_exact_pts_authority() -> None:
    spec = load_corpus_spec()
    fixture = spec["fixtures"][0]

    assert fixture["profile"] == "test"
    assert fixture["source_filename"].endswith(".mp4")
    assert fixture["ground_truth"]["exact_pts"]["representation"] == "integer_pts_index"
    validity = fixture["ground_truth"]["validity_intervals"]
    assert validity["schema_version"] == 1
    assert validity["representation"] == "integer_pts_half_open"


def test_fixture_registration_rejects_production_before_external_work(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("tests.media.fixture_corpus.shutil.which", lambda _: None)

    with pytest.raises(ValueError, match="production"):
        register_fixture_corpus(tmp_path, profile="production")


@pytest.mark.parametrize("profile", ["test", "shadow"])
def test_fixture_registration_generates_hashed_local_mp4(tmp_path, profile) -> None:
    if not ffmpeg_available() or not ffprobe_available():
        pytest.skip("ffmpeg and ffprobe are required to generate and verify the local media fixture corpus")

    registration = register_fixture_corpus(tmp_path, profile=profile)

    assert registration.source_path.is_file()
    assert registration.source_path.stat().st_size > 0
    assert registration.source_path.read_bytes()[4:8] == b"ftyp"
    assert registration.source_content_sha256 == _sha256(registration.source_path)
    assert registration.sidecar_sha256 == _sha256(registration.sidecar_path)
    assert registration.manifest_sha256 == _sha256(registration.manifest_path)

    sidecar = json.loads(registration.sidecar_path.read_text(encoding="utf-8"))
    manifest = json.loads(registration.manifest_path.read_text(encoding="utf-8"))
    assert sidecar["source"]["content_sha256"] == registration.source_content_sha256
    assert sidecar["source"]["byte_size"] == registration.source_path.stat().st_size
    assert registration.profile == profile
    assert sidecar["generator"]["ffmpeg_path"] == shutil.which("ffmpeg")
    assert sidecar["generator"]["ffmpeg_version"].startswith("ffmpeg version")
    assert manifest["sidecar"]["sha256"] == registration.sidecar_sha256
    assert manifest["source"]["content_sha256"] == registration.source_content_sha256
    assert manifest["source"]["byte_size"] == registration.source_path.stat().st_size
    assert manifest["probe"]["ffprobe_path"] == shutil.which("ffprobe")
    assert manifest["probe"]["ffprobe_version"].startswith("ffprobe version")

    time_base, pts_index = _independent_video_probe(registration.source_path)
    exact_pts = sidecar["ground_truth"]["exact_pts"]
    assert exact_pts["time_base"] == time_base
    assert exact_pts["values"] == pts_index
    assert all(isinstance(value, int) and not isinstance(value, bool) for value in pts_index)
    assert sidecar["evidence_mode"] == "fixture_ground_truth_v1"
    assert sidecar["pts_index_sha256"] == _canonical_sha256(pts_index)
    intervals = sidecar["validity_intervals"]
    assert intervals == [
        {"end_pts": end, "representation": "integer_pts_half_open", "start_pts": start}
        for start, end in zip(pts_index, pts_index[1:])
    ]
    assert all(interval["start_pts"] in pts_index and interval["end_pts"] in pts_index for interval in intervals)
    assert all(interval["start_pts"] < interval["end_pts"] for interval in intervals)

    manifest_binding = {
        "fixture_id": registration.fixture_id,
        "profile": profile,
        "schema_version": manifest["schema_version"],
        "source": manifest["source"],
    }
    assert sidecar["manifest_hash_binding"] == {
        "representation": "canonical_manifest_without_sidecar_sha256_v1",
        "sha256": _canonical_sha256(manifest_binding),
    }

    evidence = preflight_fixture(
        MediaPreflightRequest(
            profile=profile,
            source_path=registration.source_path,
            fixture_id=registration.fixture_id,
            expected_source_sha256=registration.source_content_sha256,
            manifest_path=registration.manifest_path,
            sidecar_path=registration.sidecar_path,
        )
    )
    assert evidence.pts_index.ticks == tuple(pts_index)
    assert evidence.validity_intervals.intervals[-1].end_pts == pts_index[-1]
