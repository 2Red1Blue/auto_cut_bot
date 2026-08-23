from __future__ import annotations

import hashlib
import json

import pytest
from autocut_kernel.rendering import RecipeValidationError, parse_recipe


def _hash() -> str:
    return "sha256:" + hashlib.sha256(b"source").hexdigest()


def _json_hash(value: object) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _recipe() -> dict[str, object]:
    pts_index = [0, 10, 20, 30]
    return {
        "source": {"sha256": _hash(), "byte_size": 6},
        "span": {"start_pts": 10, "end_pts": 20},
        "timebase": {"numerator": 1, "denominator": 10},
        "evidence": {
            "source": {"sha256": _hash(), "byte_size": 6},
            "video_stream": {
                "stream_index": 0,
                "codec_name": "h264",
                "width": 64,
                "height": 48,
                "time_base": {"numerator": 1, "denominator": 10},
            },
            "pts_index": pts_index,
            "pts_index_sha256": _json_hash(pts_index),
            "validity_intervals": [{"start_pts": 0, "end_pts": 30}],
            "ffprobe": {"executable": "ffprobe", "version": "test", "stderr_sha256": _json_hash("")},
            "fixture_id": "fixture-a",
            "fixture_manifest_sha256": _json_hash("manifest"),
            "fixture_sidecar_sha256": _json_hash("sidecar"),
            "fixture_schema_version": 1,
            "evidence_mode": "fixture_ground_truth_v1",
        },
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


def test_rejects_span_not_bound_to_the_evidence_pts_index() -> None:
    recipe = _recipe()
    span = recipe["span"]
    assert isinstance(span, dict)
    span["start_pts"] = 11

    with pytest.raises(RecipeValidationError) as error:
        parse_recipe(recipe, expected_source_sha256=_hash(), profile="test")

    assert error.value.code == "RECIPE_INVALID_SPAN"


def test_rejects_evidence_that_does_not_bind_recipe_source_or_timebase() -> None:
    recipe = _recipe()
    evidence = recipe["evidence"]
    assert isinstance(evidence, dict)
    evidence_source = evidence["source"]
    assert isinstance(evidence_source, dict)
    evidence_source["sha256"] = "sha256:" + "0" * 64

    with pytest.raises(RecipeValidationError) as error:
        parse_recipe(recipe, expected_source_sha256=_hash(), profile="test")

    assert error.value.code == "SOURCE_IDENTITY_MISMATCH"
