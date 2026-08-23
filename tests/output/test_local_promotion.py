"""Tests for the atomic local render/QC output promotion boundary."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from autocut_kernel.output import LocalPromotionError, LocalPromotionRequest, promote_local_output
from autocut_kernel.rendering.qc import QCCheck, QCReport


def _digest(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _request(
    root: Path,
    staging: Path,
    *,
    job_id: str = "job-1",
    attempt_id: str = "attempt-1",
    recipe_hash: str | None = None,
    qc_report: QCReport | None = None,
) -> LocalPromotionRequest:
    recipe_hash = recipe_hash or _digest(b"recipe-1")
    asset_sha256 = _digest(staging.read_bytes())
    return LocalPromotionRequest(
        output_root=root,
        job_id=job_id,
        attempt_id=attempt_id,
        staging_asset=staging,
        recipe_hash=recipe_hash,
        asset_sha256=asset_sha256,
        qc_report=qc_report or _approved_report(recipe_hash, asset_sha256),
    )


def _approved_report(recipe_hash: str, output_sha256: str) -> QCReport:
    return QCReport(recipe_hash, output_sha256, (QCCheck("identity", True, _digest(b"evidence")),))


def _json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_promotes_content_addressed_asset_manifest_then_atomic_current_pointer(tmp_path: Path) -> None:
    staging = tmp_path / "render.mp4"
    staging.write_bytes(b"verified render bytes")

    result = promote_local_output(_request(tmp_path / "output", staging))

    current = _json(result.current_path)
    assert result.asset_path.read_bytes() == staging.read_bytes()
    assert current["asset"] == {"path": result.asset_path.relative_to(tmp_path / "output").as_posix(), "sha256": result.asset_sha256}
    assert current["manifest"] == {"path": result.manifest_path.relative_to(tmp_path / "output").as_posix(), "sha256": result.manifest_sha256}
    assert _json(result.manifest_path)["qc_report"] == {
        "checks": [{"evidence_sha256": _digest(b"evidence"), "name": "identity", "passed": True}],
        "output_sha256": result.asset_sha256,
        "recipe_hash": _digest(b"recipe-1"),
        "status": "approved",
    }


def test_same_promoted_output_is_idempotent(tmp_path: Path) -> None:
    staging = tmp_path / "render.mp4"
    staging.write_bytes(b"verified render bytes")
    request = _request(tmp_path / "output", staging)

    first = promote_local_output(request)
    second = promote_local_output(request)

    assert second == first
    assert len(list((tmp_path / "output" / "assets" / "sha256" / request.asset_sha256[7:9]).iterdir())) == 1


def test_rejects_nonapproved_derived_qc_before_creating_current_pointer(tmp_path: Path) -> None:
    staging = tmp_path / "render.mp4"
    staging.write_bytes(b"verified render bytes")
    recipe_hash = _digest(b"recipe-1")
    rejected = QCReport(recipe_hash, _digest(staging.read_bytes()), (QCCheck("identity", False, _digest(b"evidence")),))

    with pytest.raises(LocalPromotionError, match="approved"):
        promote_local_output(_request(tmp_path / "output", staging, qc_report=rejected))

    assert not (tmp_path / "output" / "current.json").exists()


def test_digest_mismatch_preserves_existing_current_pointer(tmp_path: Path) -> None:
    root = tmp_path / "output"
    old_staging = tmp_path / "old.mp4"
    old_staging.write_bytes(b"old approved output")
    previous_result = promote_local_output(_request(root, old_staging))
    previous = previous_result.current_path.read_bytes()
    new_staging = tmp_path / "new.mp4"
    new_staging.write_bytes(b"new output")
    request = _request(root, new_staging)
    wrong_digest = _digest(b"wrong")
    request = LocalPromotionRequest(
        root,
        request.job_id,
        request.attempt_id,
        new_staging,
        request.recipe_hash,
        wrong_digest,
        _approved_report(request.recipe_hash, wrong_digest),
    )

    with pytest.raises(LocalPromotionError, match="digest"):
        promote_local_output(request)

    assert previous_result.current_path.read_bytes() == previous


def test_existing_conflicting_asset_fails_without_replacing_current(tmp_path: Path) -> None:
    root = tmp_path / "output"
    staging = tmp_path / "render.mp4"
    staging.write_bytes(b"verified render bytes")
    request = _request(root, staging)
    asset_hex = request.asset_sha256.removeprefix("sha256:")
    conflict = root / "assets" / "sha256" / asset_hex[:2] / f"{asset_hex}.mp4"
    conflict.parent.mkdir(parents=True)
    conflict.write_bytes(b"different bytes")

    with pytest.raises(LocalPromotionError, match="conflicting"):
        promote_local_output(request)

    assert not (root / "current.json").exists()


def test_attempt_namespaces_keep_cross_job_current_pointers_isolated(tmp_path: Path) -> None:
    root = tmp_path / "output"
    staging = tmp_path / "render.mp4"
    staging.write_bytes(b"verified render bytes")

    first = promote_local_output(_request(root, staging, job_id="job-a", attempt_id="attempt-a"))
    second = promote_local_output(_request(root, staging, job_id="job-b", attempt_id="attempt-b"))

    assert first.asset_path == second.asset_path
    assert first.current_path != second.current_path
    assert first.current_path == root / "results" / "job-a" / "attempt-a" / "current.json"
    assert second.current_path == root / "results" / "job-b" / "attempt-b" / "current.json"


def test_hardlinked_existing_cas_asset_is_rejected_even_when_bytes_match(tmp_path: Path) -> None:
    root = tmp_path / "output"
    staging = tmp_path / "render.mp4"
    staging.write_bytes(b"verified render bytes")
    request = _request(root, staging)
    asset_hex = request.asset_sha256.removeprefix("sha256:")
    existing = root / "assets" / "sha256" / asset_hex[:2] / f"{asset_hex}.mp4"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(staging.read_bytes())
    peer = tmp_path / "hardlink-peer.mp4"
    peer.hardlink_to(existing)

    with pytest.raises(LocalPromotionError, match="conflicting"):
        promote_local_output(request)


@pytest.mark.parametrize("value", ["../escape", "attempt/a", ".", ""])
def test_rejects_unsafe_namespace_components(tmp_path: Path, value: str) -> None:
    staging = tmp_path / "render.mp4"
    staging.write_bytes(b"verified render bytes")

    with pytest.raises(LocalPromotionError, match="namespace"):
        promote_local_output(_request(tmp_path / "output", staging, job_id=value))


def test_rejects_symlinked_generated_directory_component(tmp_path: Path) -> None:
    root = tmp_path / "output"
    root.mkdir()
    (root / "assets").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    staging = tmp_path / "render.mp4"
    staging.write_bytes(b"verified render bytes")

    with pytest.raises(LocalPromotionError, match="directory component"):
        promote_local_output(_request(root, staging))


def test_rejects_caller_forged_approval_mapping(tmp_path: Path) -> None:
    staging = tmp_path / "render.mp4"
    staging.write_bytes(b"verified render bytes")
    request = _request(tmp_path / "output", staging)
    forged = LocalPromotionRequest(
        request.output_root,
        request.job_id,
        request.attempt_id,
        request.staging_asset,
        request.recipe_hash,
        request.asset_sha256,
        {"status": "approved"},  # type: ignore[arg-type]
    )

    with pytest.raises(LocalPromotionError, match="QCReport"):
        promote_local_output(forged)


def test_rejects_qc_report_with_mismatched_recipe_or_output_identity(tmp_path: Path) -> None:
    staging = tmp_path / "render.mp4"
    staging.write_bytes(b"verified render bytes")
    request = _request(tmp_path / "output", staging)
    wrong_recipe = _approved_report(_digest(b"other recipe"), request.asset_sha256)
    wrong_output = _approved_report(request.recipe_hash, _digest(b"other output"))

    with pytest.raises(LocalPromotionError, match="recipe_hash"):
        promote_local_output(_request(request.output_root, staging, qc_report=wrong_recipe))
    with pytest.raises(LocalPromotionError, match="output_sha256"):
        promote_local_output(_request(request.output_root, staging, qc_report=wrong_output))
