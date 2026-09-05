"""Mechanical normalization does not create observations or repair references."""

import hashlib
import json
from dataclasses import replace

import pytest

from autocut_kernel.vlm.enum_normalization import normalize_vlm_enum_sets
from autocut_kernel.vlm.normalized_contracts import (
    VLM_PARSER_NORMALIZED_V4, ParserImplementationUnavailableError,
    parse_registered_vlm_response, parser_contract_sha256_for, require_parser_contract,
)
from autocut_kernel.vlm.parser import VlmResponseIndeterminate, VlmResponseRejected
from autocut_kernel.vlm.semantic_contracts import parser_contract_sha256_for as legacy_contract
from tests.vlm.test_semantic_pack_v4 import _v4_context, _wire


def _parse(raw):
    manifest, manifests, policy, identity = _v4_context()
    return parse_registered_vlm_response(
        raw, parser_strategy_version=VLM_PARSER_NORMALIZED_V4,
        parser_contract_sha256=parser_contract_sha256_for(VLM_PARSER_NORMALIZED_V4),
        manifest=manifest, manifest_set=manifests, request_identity=identity, policy=policy,
    )


def test_normalization_preserves_raw_hash_and_only_reports_changed_enum_paths():
    wire = _wire()
    wire["candidate_hypotheses"][0]["tags"] = ["reveal", "dialogue"]
    raw = json.dumps(wire, ensure_ascii=False).encode()
    policy = _v4_context()[2]
    normalized = normalize_vlm_enum_sets(raw, policy)
    assert normalized.raw_response_sha256 == "sha256:" + hashlib.sha256(raw).hexdigest()
    assert [item.path for item in normalized.transformations] == ["$.candidate_hypotheses[0].tags"]
    assert normalized.transformations[0].before == ("reveal", "dialogue")
    assert normalized.transformations[0].after == ("dialogue", "reveal")
    again = normalize_vlm_enum_sets(normalized.normalized_response, policy)
    assert again.normalized_response == normalized.normalized_response
    assert again.transformations == ()
    expected = json.loads(raw)
    expected["candidate_hypotheses"][0]["tags"] = ["dialogue", "reveal"]
    assert json.loads(normalized.normalized_response) == expected
    pack = _parse(raw)
    assert pack.raw_response_sha256 == normalized.raw_response_sha256
    assert "supporting_frame_ids" not in pack.facts[0].support.to_mapping()


@pytest.mark.parametrize("tags", [[], ["dialogue", "dialogue"], ["unknown"], [True], "dialogue"])
def test_invalid_sets_are_not_deduplicated_or_guessed(tags):
    wire = _wire()
    wire["candidate_hypotheses"][0]["tags"] = tags
    with pytest.raises(VlmResponseRejected):
        _parse(json.dumps(wire).encode())


def test_unknown_reference_still_fails_after_enum_sorting():
    wire = _wire()
    wire["candidate_hypotheses"][0]["tags"] = ["reveal", "dialogue"]
    wire["events"][0]["fact_refs"] = ["missing"]
    with pytest.raises(VlmResponseRejected, match="UNKNOWN_REFERENCE"):
        _parse(json.dumps(wire).encode())


@pytest.mark.parametrize("raw", [b'{"schema_version":4,"schema_version":4}', b'{', b'{"value":NaN}', b'\xff'])
def test_strict_json_is_not_repaired(raw):
    with pytest.raises(VlmResponseRejected):
        normalize_vlm_enum_sets(raw, _v4_context()[2])


def test_byte_budget_precedes_parsing():
    policy = replace(_v4_context()[2], max_response_bytes=10)
    with pytest.raises(VlmResponseIndeterminate, match="RESPONSE_BUDGET_EXCEEDED"):
        normalize_vlm_enum_sets(b" " * 11, policy)


def test_legacy_bundle_identity_is_preserved_and_unknown_history_is_unavailable():
    for strategy in ("strict-semantic-pack-v3", "strict-semantic-pack-v4"):
        assert parser_contract_sha256_for(strategy) == legacy_contract(strategy)
    with pytest.raises(ParserImplementationUnavailableError, match="PARSER_IMPLEMENTATION_UNAVAILABLE"):
        require_parser_contract("strict-semantic-pack-v4", "sha256:" + "0" * 64)
    with pytest.raises(ParserImplementationUnavailableError):
        require_parser_contract(VLM_PARSER_NORMALIZED_V4, legacy_contract("strict-semantic-pack-v4"))
