from __future__ import annotations

import hashlib
import json
from copy import deepcopy

import pytest
from autocut_kernel.media.types import TimeBase
from autocut_kernel.physical_edit.candidate_exact_span import CandidateExactSpanPolicy
from autocut_kernel.physical_edit.editorial_exact_span import (
    EDITORIAL_EXACT_SPAN_STRATEGY,
    EditorialExactSpanPolicy,
)
from autocut_kernel.pipeline.compile_production_recipe_command import (
    ProductionRecipeCompilationLimits,
)

from auto_cut_bot.pipeline.runtime.stage4_authority import (
    STAGE4_RECIPE_AUTHORITY_SCHEMA_VERSION,
    Stage4RecipeAuthorityError,
    Stage4RecipeAuthorityProfile,
    decode_stage4_recipe_authority,
)


def _profile() -> Stage4RecipeAuthorityProfile:
    return Stage4RecipeAuthorityProfile(
        1,
        EditorialExactSpanPolicy(EDITORIAL_EXACT_SPAN_STRATEGY, 90_000, TimeBase(1, 90_000)),
        CandidateExactSpanPolicy(100_000, 100_000, 1, 1, 0),
        ProductionRecipeCompilationLimits(10_000, 8_000_000, 32_000_000),
    )


def test_round_trip_is_closed_and_hash_bound() -> None:
    value = _profile()
    decoded = Stage4RecipeAuthorityProfile.from_mapping(value.to_mapping())

    assert decoded == value
    assert decoded.canonical_hash == value.canonical_hash
    assert decoded.to_mapping()["schema_version"] == STAGE4_RECIPE_AUTHORITY_SCHEMA_VERSION


def test_digest_bound_decoder_rejects_drift_and_duplicate_json_keys() -> None:
    raw = json.dumps(_profile().to_mapping(), sort_keys=True, separators=(",", ":")).encode()
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()

    assert decode_stage4_recipe_authority(raw, expected_sha256=digest) == _profile()
    with pytest.raises(Stage4RecipeAuthorityError, match="digest mismatch"):
        decode_stage4_recipe_authority(raw, expected_sha256="sha256:" + "0" * 64)
    duplicated = raw.replace(b'"artifact_revision":', b'"artifact_revision":1,"artifact_revision":', 1)
    duplicated_digest = "sha256:" + hashlib.sha256(duplicated).hexdigest()
    with pytest.raises(Stage4RecipeAuthorityError, match="duplicate keys"):
        decode_stage4_recipe_authority(duplicated, expected_sha256=duplicated_digest)


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda value: value.__setitem__("extra", True), "missing or unknown"),
        (lambda value: value.__setitem__("schema_version", "stage4-recipe-authority-v0"), "schema"),
        (lambda value: value.__setitem__("artifact_revision", 0), "artifact_revision"),
        (
            lambda value: value["candidate_exact_span_policy"].__setitem__(  # type: ignore[index]
                "strategy", "other"
            ),
            "candidate exact-span strategy",
        ),
    ],
)
def test_decoder_rejects_unfrozen_or_unknown_authority_fields(mutate, match: str) -> None:  # type: ignore[no-untyped-def]
    mapping = deepcopy(_profile().to_mapping())
    mutate(mapping)

    with pytest.raises(Stage4RecipeAuthorityError, match=match):
        Stage4RecipeAuthorityProfile.from_mapping(mapping)
