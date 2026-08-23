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
    return digest.hexdigest()


def _write_canonical_json(path: Path, payload: dict[str, Any]) -> str:
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    path.write_bytes(encoded)
    return hashlib.sha256(encoded).hexdigest()


def _fixture_definition(spec: dict[str, Any]) -> dict[str, Any]:
    fixtures = spec.get("fixtures")
    if not isinstance(fixtures, list):
        raise ValueError("media fixture corpus spec must contain a fixtures list")
    for fixture in fixtures:
        if isinstance(fixture, dict) and fixture.get("id") == _FIXTURE_ID:
            return fixture
    raise ValueError(f"media fixture corpus spec is missing {_FIXTURE_ID!r}")


def register_fixture_corpus(tmp_path: Path, *, profile: str = _TEST_PROFILE) -> FixtureCorpusRegistration:
    """Generate and register the controlled MP4 fixture under ``tmp_path``.

    This registration API is test-only.  In particular, a production caller is
    rejected before executable discovery or filesystem writes take place.
    """
    if profile == "production":
        raise ValueError("the media fixture corpus must not be registered with profile='production'")
    if profile != _TEST_PROFILE:
        raise ValueError(f"unsupported media fixture corpus profile: {profile!r}")

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise FileNotFoundError("ffmpeg is required to generate the local media fixture corpus")

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
    sidecar_path = output_dir / "lavfi-testsrc2-sine-10fps-v1.sidecar.json"
    sidecar = {
        "fixture_id": _FIXTURE_ID,
        "generation": generation,
        "ground_truth": definition["ground_truth"],
        "profile": profile,
        "schema_version": spec_schema_version(spec),
        "source": {
            "content_sha256": source_sha256,
            "filename": source_path.name,
        },
    }
    sidecar_sha256 = _write_canonical_json(sidecar_path, sidecar)

    manifest_path = output_dir / "fixture-manifest.json"
    manifest = {
        "fixture_id": _FIXTURE_ID,
        "profile": profile,
        "schema_version": spec_schema_version(spec),
        "sidecar": {"filename": sidecar_path.name, "sha256": sidecar_sha256},
        "source": {"content_sha256": source_sha256, "filename": source_path.name},
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
