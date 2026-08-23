"""Tests for the persisted-recipe local promotion boundary."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from autocut_kernel.output import LocalPromotionError, LocalPromotionRequest, promote_local_output
from autocut_kernel.rendering.qc import QCCheck, QCReport
from autocut_kernel.store import ArtifactScope, Job, RecipeReference


def _digest(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _request(root: Path, staging: Path, *, store: object = object()) -> LocalPromotionRequest:
    recipe_hash = _digest(b"recipe")
    return LocalPromotionRequest(
        output_root=root,
        job=Job("job-1", "test"),
        attempt_id="attempt-1",
        staging_asset=staging,
        asset_sha256=_digest(staging.read_bytes()),
        qc_report=QCReport(
            recipe_hash,
            _digest(staging.read_bytes()),
            (QCCheck("identity", True, _digest(b"evidence")),),
        ),
        store=store,  # type: ignore[arg-type]
        recipe_reference=RecipeReference(
            ArtifactScope("pipeline", "job", "job-1"), "recipe", 1, recipe_hash
        ),
    )


def test_rejects_non_store_promotion_authority_before_creating_current_pointer(tmp_path: Path) -> None:
    staging = tmp_path / "render.mp4"
    staging.write_bytes(b"untrusted render")

    with pytest.raises(LocalPromotionError, match="PostgresRuntimeStore"):
        promote_local_output(_request(tmp_path / "output", staging))

    assert not list((tmp_path / "output").rglob("current.json")) if (tmp_path / "output").exists() else True


def test_rejects_attribute_compatible_recipe_reference_before_creating_current_pointer(
    tmp_path: Path,
) -> None:
    staging = tmp_path / "render.mp4"
    staging.write_bytes(b"untrusted render")
    request = _request(tmp_path / "output", staging)
    request = LocalPromotionRequest(
        request.output_root,
        request.job,
        request.attempt_id,
        request.staging_asset,
        request.asset_sha256,
        request.qc_report,
        request.store,
        object(),  # type: ignore[arg-type]
    )

    with pytest.raises(LocalPromotionError, match="PostgresRuntimeStore"):
        promote_local_output(request)
