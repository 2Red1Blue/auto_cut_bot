"""Regression coverage for the local Media fixture corpus."""

from __future__ import annotations

import hashlib
import json

import pytest

from tests.media.fixture_corpus import ffmpeg_available, load_corpus_spec, register_fixture_corpus


def _sha256(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_corpus_spec_declares_ffprobe_as_the_exact_pts_authority() -> None:
    spec = load_corpus_spec()
    fixture = spec["fixtures"][0]

    assert fixture["profile"] == "test"
    assert fixture["source_filename"].endswith(".mp4")
    assert "ffprobe" in fixture["ground_truth"]["exact_pts"]
    assert "pts" not in fixture["ground_truth"].get("known_values", {})


def test_fixture_registration_rejects_production_before_external_work(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("tests.media.fixture_corpus.shutil.which", lambda _: None)

    with pytest.raises(ValueError, match="production"):
        register_fixture_corpus(tmp_path, profile="production")


def test_fixture_registration_generates_hashed_local_mp4(tmp_path) -> None:
    if not ffmpeg_available():
        pytest.skip("ffmpeg is not installed; local media fixture corpus cannot be generated")

    registration = register_fixture_corpus(tmp_path)

    assert registration.source_path.is_file()
    assert registration.source_path.stat().st_size > 0
    assert registration.source_path.read_bytes()[4:8] == b"ftyp"
    assert registration.source_content_sha256 == _sha256(registration.source_path)
    assert registration.sidecar_sha256 == _sha256(registration.sidecar_path)
    assert registration.manifest_sha256 == _sha256(registration.manifest_path)

    sidecar = json.loads(registration.sidecar_path.read_text(encoding="utf-8"))
    manifest = json.loads(registration.manifest_path.read_text(encoding="utf-8"))
    assert sidecar["source"]["content_sha256"] == registration.source_content_sha256
    assert "ffprobe" in sidecar["ground_truth"]["exact_pts"]
    assert manifest["sidecar"]["sha256"] == registration.sidecar_sha256
    assert manifest["source"]["content_sha256"] == registration.source_content_sha256
