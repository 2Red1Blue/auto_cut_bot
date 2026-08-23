"""Controlled local-media fixtures for Media contract tests.

The corpus intentionally creates its source media at test time.  No media binary
is checked in, and exact timestamps are deliberately left to an ``ffprobe``
consumer instead of being approximated from frame rate or duration.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_CORPUS_SPEC_PATH = Path(__file__).parent / "fixtures" / "corpus-spec.json"
_TEST_PROFILE = "test"
_SHADOW_PROFILE = "shadow"
_FIXTURE_ID = "lavfi-testsrc2-sine-10fps-v1"


@dataclass(frozen=True)
class FixtureCorpusRegistration:
    """Paths and integrity values for one generated controlled media source."""

    fixture_id: str
    profile: str
    source_path: Path
    manifest_path: Path
    sidecar_path: Path
    source_content_sha256: str
    manifest_sha256: str
    sidecar_sha256: str


def ffmpeg_available() -> bool:
    """Return whether the local fixture generator can be executed."""
    return shutil.which("ffmpeg") is not None


def ffprobe_available() -> bool:
    """Return whether exact timestamp evidence can be read locally."""
    return shutil.which("ffprobe") is not None


def load_corpus_spec() -> dict[str, Any]:
    """Load the checked-in, non-binary corpus recipe."""
    with _CORPUS_SPEC_PATH.open(encoding="utf-8") as stream:
        spec = json.load(stream)
    if not isinstance(spec, dict):
        raise ValueError("media fixture corpus spec must be a JSON object")
    return spec


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


def _write_canonical_json(path: Path, payload: dict[str, Any]) -> str:
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    path.write_bytes(encoded)
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _canonical_sha256(payload: object) -> str:
    """Hash a canonical JSON value using the schema's prefixed digest form."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _fixture_definition(spec: dict[str, Any]) -> dict[str, Any]:
    fixtures = spec.get("fixtures")
    if not isinstance(fixtures, list):
        raise ValueError("media fixture corpus spec must contain a fixtures list")
    for fixture in fixtures:
        if isinstance(fixture, dict) and fixture.get("id") == _FIXTURE_ID:
            return fixture
    raise ValueError(f"media fixture corpus spec is missing {_FIXTURE_ID!r}")


def _executable_version(executable: str) -> str:
    completed = subprocess.run(
        [executable, "-version"], capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        raise RuntimeError(f"could not read version for executable: {executable}")
    version = completed.stdout.splitlines()[0] if completed.stdout else ""
    if not version:
        raise RuntimeError(f"executable did not report a version: {executable}")
    return version


def _probe_video_evidence(ffprobe: str, source_path: Path) -> tuple[str, tuple[int, ...]]:
    """Read the exact video frame index as integer ticks from ffprobe."""
    command = [
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
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or "ffprobe returned no stderr"
        raise RuntimeError(f"ffprobe could not read media fixture evidence: {stderr}")
    try:
        payload = json.loads(completed.stdout)
        streams = payload["streams"]
        frames = payload["frames"]
        time_base = streams[0]["time_base"]
    except (IndexError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise RuntimeError("ffprobe returned incomplete media fixture evidence") from error
    if not isinstance(time_base, str) or "/" not in time_base:
        raise RuntimeError("ffprobe did not return a video time base")

    pts: list[int] = []
    for frame in frames:
        if not isinstance(frame, dict):
            raise RuntimeError("ffprobe returned a malformed frame record")
        if frame.get("media_type") != "video":
            continue
        raw_pts = frame.get("best_effort_timestamp")
        if not isinstance(raw_pts, int) or isinstance(raw_pts, bool):
            raise RuntimeError("ffprobe frame is missing an integer best_effort_timestamp")
        pts.append(raw_pts)
    if not pts:
        raise RuntimeError("ffprobe did not return video frame timestamps")
    if pts != sorted(set(pts)):
        raise RuntimeError("ffprobe video frame PTS index is not strictly increasing")
    return time_base, tuple(pts)


def register_fixture_corpus(tmp_path: Path, *, profile: str = _TEST_PROFILE) -> FixtureCorpusRegistration:
    """Generate and register the controlled MP4 fixture under ``tmp_path``.

    This registration API is test-only.  In particular, a production caller is
    rejected before executable discovery or filesystem writes take place.
    """
    if profile == "production":
        raise ValueError("the media fixture corpus must not be registered with profile='production'")
    if profile not in {_TEST_PROFILE, _SHADOW_PROFILE}:
        raise ValueError(f"unsupported media fixture corpus profile: {profile!r}")

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise FileNotFoundError("ffmpeg is required to generate the local media fixture corpus")
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        raise FileNotFoundError("ffprobe is required to record exact local media fixture timestamps")

    spec = load_corpus_spec()
    definition = _fixture_definition(spec)
    generation = definition.get("generation")
    if not isinstance(generation, dict):
        raise ValueError("media fixture definition must contain generation metadata")
    inputs = generation.get("inputs")
    if not isinstance(inputs, list) or len(inputs) != 2 or not all(isinstance(item, str) for item in inputs):
        raise ValueError("media fixture definition must contain two lavfi input strings")

    filename = definition.get("source_filename")
    if not isinstance(filename, str) or Path(filename).name != filename or not filename.endswith(".mp4"):
        raise ValueError("media fixture source_filename must be an MP4 basename")

    video = generation.get("video")
    audio = generation.get("audio")
    duration = generation.get("duration_seconds")
    if not isinstance(video, dict) or not isinstance(audio, dict) or not isinstance(duration, str):
        raise ValueError("media fixture generation metadata is incomplete")

    output_dir = tmp_path / "media-fixture-corpus"
    output_dir.mkdir(parents=True, exist_ok=True)
    source_path = output_dir / filename
    command = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        inputs[0],
        "-f",
        "lavfi",
        "-i",
        inputs[1],
        "-t",
        duration,
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-map_metadata",
        "-1",
        "-c:v",
        str(video["codec"]),
        "-q:v",
        str(video["quality"]),
        "-pix_fmt",
        str(video["pixel_format"]),
        "-c:a",
        str(audio["codec"]),
        "-b:a",
        str(audio["bitrate"]),
        "-movflags",
        "+faststart",
        str(source_path),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or "ffmpeg returned no stderr"
        raise RuntimeError(f"ffmpeg could not generate media fixture: {stderr}")
    if not source_path.is_file() or source_path.stat().st_size == 0:
        raise RuntimeError("ffmpeg completed without producing a non-empty MP4 fixture")

    source_sha256 = _sha256_file(source_path)
    source_byte_size = source_path.stat().st_size
    time_base, pts_index = _probe_video_evidence(ffprobe, source_path)
    ground_truth_template = definition.get("ground_truth")
    if not isinstance(ground_truth_template, dict):
        raise ValueError("media fixture definition must contain ground truth metadata")
    validity_template = ground_truth_template.get("validity_intervals")
    if not isinstance(validity_template, dict):
        raise ValueError("media fixture ground truth must contain validity interval metadata")
    interval_schema_version = validity_template.get("schema_version")
    if not isinstance(interval_schema_version, int):
        raise ValueError("media fixture validity interval schema_version must be an integer")
    interval_representation = validity_template.get("representation")
    if interval_representation != "integer_pts_half_open":
        raise ValueError("media fixture validity intervals must use integer_pts_half_open")
    validity_intervals = [
        {
            "end_pts": end_pts,
            "representation": interval_representation,
            "start_pts": start_pts,
        }
        for start_pts, end_pts in zip(pts_index, pts_index[1:])
    ]
    if not validity_intervals:
        raise RuntimeError("ffprobe needs at least two video PTS values for half-open fixture coverage")
    manifest_binding = {
        "fixture_id": _FIXTURE_ID,
        "profile": profile,
        "schema_version": spec_schema_version(spec),
        "source": {
            "byte_size": source_byte_size,
            "content_sha256": source_sha256,
            "filename": source_path.name,
        },
    }

    sidecar_path = output_dir / "lavfi-testsrc2-sine-10fps-v1.sidecar.json"
    sidecar = {
        "fixture_id": _FIXTURE_ID,
        "generation": generation,
        "generator": {"ffmpeg_path": ffmpeg, "ffmpeg_version": _executable_version(ffmpeg)},
        "ground_truth": {
            "exact_pts": {
                "authoritative_source": "ffprobe video-frame best_effort_timestamp",
                "representation": "integer_pts_index",
                "time_base": time_base,
                "values": list(pts_index),
            },
            "validity_intervals": {
                "coverage": "consecutive_indexed_pts_pairs",
                "representation": interval_representation,
                "schema_version": interval_schema_version,
            },
        },
        "evidence_mode": "fixture_ground_truth_v1",
        "manifest_hash_binding": {
            "representation": "canonical_manifest_without_sidecar_sha256_v1",
            "sha256": _canonical_sha256(manifest_binding),
        },
        "profile": profile,
        "pts_index_sha256": _canonical_sha256(list(pts_index)),
        "schema_version": spec_schema_version(spec),
        "source": {
            "byte_size": source_byte_size,
            "content_sha256": source_sha256,
            "filename": source_path.name,
        },
        "validity_intervals": validity_intervals,
    }
    sidecar_sha256 = _write_canonical_json(sidecar_path, sidecar)

    manifest_path = output_dir / "fixture-manifest.json"
    manifest = {
        "fixture_id": _FIXTURE_ID,
        "profile": profile,
        "probe": {"ffprobe_path": ffprobe, "ffprobe_version": _executable_version(ffprobe)},
        "schema_version": spec_schema_version(spec),
        "sidecar": {"filename": sidecar_path.name, "sha256": sidecar_sha256},
        "source": manifest_binding["source"],
    }
    manifest_sha256 = _write_canonical_json(manifest_path, manifest)

    return FixtureCorpusRegistration(
        fixture_id=_FIXTURE_ID,
        profile=profile,
        source_path=source_path,
        manifest_path=manifest_path,
        sidecar_path=sidecar_path,
        source_content_sha256=source_sha256,
        manifest_sha256=manifest_sha256,
        sidecar_sha256=sidecar_sha256,
    )


def spec_schema_version(spec: dict[str, Any]) -> int:
    """Return the validated integer schema version for serialized evidence."""
    schema_version = spec.get("schema_version")
    if not isinstance(schema_version, int):
        raise ValueError("media fixture corpus spec must contain an integer schema_version")
    return schema_version
