"""Tests for trusted composition and filesystem invariants of local promotion."""

from __future__ import annotations

import hashlib
import json
from dataclasses import fields
from pathlib import Path
from uuid import uuid4

import pytest
from autocut_kernel.media.types import TimeBase
from autocut_kernel.output import LocalPromotionError, LocalPromotionRequest, promote_local_output
from autocut_kernel.output.local_promotion import LocalPromotionService
from autocut_kernel.rendering import H264_MP4_VIDEO_PROFILE, Recipe
from autocut_kernel.rendering.ffmpeg_renderer import RenderAttempt
from autocut_kernel.rendering.qc import QCCheck, QCReport
from autocut_kernel.store import Job, PersistedRecipe, PostgresRuntimeStore, RecipeReference
from autocut_kernel.store.models import canonical_recipe_scope


def _digest(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _payload(digest: str, size: int) -> str:
    return json.dumps(
        {
            "source": {"sha256": digest, "byte_size": size},
            "span": {"start_pts": 0, "end_pts": 1},
            "timebase": {"numerator": 1, "denominator": 1},
            "evidence": {
                "source": {"sha256": digest, "byte_size": size},
                "video_stream": {
                    "stream_index": 0,
                    "codec_name": "h264",
                    "width": 1,
                    "height": 1,
                    "time_base": {"numerator": 1, "denominator": 1},
                },
                "pts_index": [0, 1],
                "pts_index_sha256": _digest(b"[0,1]"),
                "validity_intervals": [{"start_pts": 0, "end_pts": 1}],
                "ffprobe": {
                    "executable": "ffprobe",
                    "version": "fixture",
                    "stderr_sha256": _digest(b""),
                },
                "fixture_id": "fixture",
                "fixture_manifest_sha256": _digest(b"manifest"),
                "fixture_sidecar_sha256": _digest(b"sidecar"),
                "fixture_schema_version": 1,
                "evidence_mode": "fixture_ground_truth_v1",
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    job_key: str = "job-1",
    root: Path | None = None,
) -> tuple[LocalPromotionRequest, PostgresRuntimeStore]:
    staging = tmp_path / f"{job_key}.mp4"
    staging.write_bytes(b"verified render bytes")
    digest, size = _digest(staging.read_bytes()), staging.stat().st_size
    job = Job(job_key, "test")
    payload = _payload(digest, size)
    recipe_hash = _digest(payload.encode())
    recipe = Recipe(
        digest, size, TimeBase(1, 1), 0, 1, "fixture", "fixture_ground_truth_v1", recipe_hash
    )
    attempt = RenderAttempt(
        recipe.canonical_hash,
        H264_MP4_VIDEO_PROFILE.canonical_hash,
        digest,
        staging,
        digest,
        size,
        ("ffmpeg",),
        _digest(b"stderr"),
    )
    report = QCReport(
        recipe.canonical_hash, digest, (QCCheck("trusted", True, _digest(b"qc")),), recipe, attempt
    )
    reference = RecipeReference(canonical_recipe_scope(job), "recipe", 1, recipe_hash)
    persisted = PersistedRecipe(reference, payload, uuid4(), uuid4(), uuid4(), uuid4())
    store = PostgresRuntimeStore(lambda: None)
    monkeypatch.setattr(store, "read_recipe", lambda *_: persisted)
    monkeypatch.setattr(
        "autocut_kernel.output.local_promotion.LocalQC",
        lambda: type("QC", (), {"inspect": lambda *_: report})(),
    )
    return LocalPromotionRequest(
        root or tmp_path / "output", job, "attempt-1", staging, digest, report, reference
    ), store


def _promote(request: LocalPromotionRequest, store: PostgresRuntimeStore) -> object:
    return LocalPromotionService(store).promote(request)


def test_request_cannot_substitute_a_store() -> None:
    assert "store" not in {field.name for field in fields(LocalPromotionRequest)}


def test_legacy_function_cannot_make_output_visible_without_service() -> None:
    with pytest.raises(LocalPromotionError, match="trusted LocalPromotionService"):
        promote_local_output(object())  # type: ignore[arg-type]


def test_service_rejects_a_postgres_store_subclass() -> None:
    class SubstituteStore(PostgresRuntimeStore):
        pass

    with pytest.raises(LocalPromotionError, match="exact PostgresRuntimeStore"):
        LocalPromotionService(SubstituteStore(lambda: None))  # type: ignore[arg-type]


def test_atomic_idempotent_install_and_namespace_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, store = _request(tmp_path, monkeypatch, job_key="job-a")
    first = _promote(request, store)
    second = _promote(request, store)
    other_request, other_store = _request(
        tmp_path, monkeypatch, job_key="job-b", root=request.output_root
    )
    other = _promote(other_request, other_store)
    assert second == first
    assert first.current_path != other.current_path  # type: ignore[attr-defined]
    assert first.current_path.parent.name == "job-a"  # type: ignore[attr-defined]


def test_digest_conflict_preserves_current_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, store = _request(tmp_path, monkeypatch)
    result = _promote(request, store)
    previous = result.current_path.read_bytes()  # type: ignore[attr-defined]
    request.staging_asset.write_bytes(b"tampered")
    with pytest.raises(LocalPromotionError, match="digest"):
        _promote(request, store)
    assert result.current_path.read_bytes() == previous  # type: ignore[attr-defined]


def test_rejects_hardlinked_asset_and_symlinked_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, store = _request(tmp_path, monkeypatch)
    asset_hex = request.asset_sha256[7:]
    existing = request.output_root / "assets" / "sha256" / asset_hex[:2] / f"{asset_hex}.mp4"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(request.staging_asset.read_bytes())
    (tmp_path / "peer").hardlink_to(existing)
    with pytest.raises(LocalPromotionError, match="conflicting"):
        _promote(request, store)
    symlink_request, symlink_store = _request(
        tmp_path, monkeypatch, job_key="job-symlink", root=tmp_path / "symlink-output"
    )
    symlink_request.output_root.mkdir()
    (symlink_request.output_root / "assets").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(LocalPromotionError, match="directory"):
        _promote(symlink_request, symlink_store)


def test_qc_mismatch_prevents_current_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, store = _request(tmp_path, monkeypatch)
    recipe = request.qc_report.recipe
    assert recipe is not None
    report = QCReport(
        recipe.canonical_hash,
        request.asset_sha256,
        (QCCheck("trusted", False, _digest(b"bad")),),
        recipe,
        request.qc_report.attempt,
    )
    request = LocalPromotionRequest(
        request.output_root,
        request.job,
        request.attempt_id,
        request.staging_asset,
        request.asset_sha256,
        report,
        request.recipe_reference,
    )
    monkeypatch.setattr(
        "autocut_kernel.output.local_promotion.LocalQC",
        lambda: type("RejectedQC", (), {"inspect": lambda *_: report})(),
    )
    with pytest.raises(LocalPromotionError, match="QC"):
        _promote(request, store)
    assert not (request.output_root / "results" / request.job.job_key / "current.json").exists()
