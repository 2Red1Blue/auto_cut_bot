from __future__ import annotations

import hashlib

import pytest
from autocut_kernel.rendering import RecipeValidationError, parse_recipe


def _hash() -> str:
    return "sha256:" + hashlib.sha256(b"source").hexdigest()


def _recipe() -> dict[str, object]:
    return {
        "source": {"sha256": _hash(), "byte_size": 6},
        "span": {"start_pts": 10, "end_pts": 20},
        "timebase": {"numerator": 1, "denominator": 10},
        "evidence": {"fixture_id": "fixture-a", "evidence_mode": "fixture_ground_truth_v1"},
    }


def test_parses_current_one_source_one_span_recipe_for_test_and_shadow() -> None:
    recipe = _recipe()

    parsed = parse_recipe(recipe, expected_source_sha256=_hash(), profile="test")

    assert parsed.start_pts == 10
    assert parsed.end_pts == 20
    assert parsed.duration_pts == 10
    assert parsed.canonical_hash.startswith("sha256:")
    assert parse_recipe(recipe, expected_source_sha256=_hash(), profile="shadow") == parsed


@pytest.mark.parametrize("payload", [{}, {"source": {}}])
def test_rejects_empty_or_incomplete_recipe(payload: object) -> None:
    with pytest.raises(RecipeValidationError):
        parse_recipe(payload, expected_source_sha256=_hash(), profile="test")


@pytest.mark.parametrize("field", ["start_pts", "end_pts"])
def test_rejects_float_pts(field: str) -> None:
    recipe = _recipe()
    span = recipe["span"]
    assert isinstance(span, dict)
    span[field] = 10.0

    with pytest.raises(RecipeValidationError, match="integer PTS tick"):
        parse_recipe(recipe, expected_source_sha256=_hash(), profile="test")


def test_rejects_wrong_source_hash_and_production_fixture() -> None:
    with pytest.raises(RecipeValidationError) as mismatch:
        parse_recipe(_recipe(), expected_source_sha256="sha256:" + "0" * 64, profile="test")
    assert mismatch.value.code == "SOURCE_IDENTITY_MISMATCH"

    with pytest.raises(RecipeValidationError) as production:
        parse_recipe(_recipe(), expected_source_sha256=_hash(), profile="production")
    assert production.value.code == "FIXTURE_PROFILE_DENIED"
