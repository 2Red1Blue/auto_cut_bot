"""Independent projection oracles over the tracked wire schema, never profiles."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
from autocut_kernel.registry.timed_speech_contract import (
    TimedSpeechContractError,
    timed_speech_registry_contract_sha256,
)

SCHEMA_PATH = Path(__file__).parents[2] / "governance/schemas/local-run-profile.schema.json"
DIALECT = "https://json-schema.org/draft/2020-12/schema"
ROOT_NAME = "timed_speech_registry_entry"
ROOT_POINTER = "#/$defs/timed_speech_registry_entry"
# Manually enumerated from the tracked schema, NOT collected by the implementation.
CLOSURE_NAMES = (
    "timed_speech_registry_entry", "timed_speech_guard_policy", "profile_version", "sha256",
    "asr_registry_requirement", "vad_registry_requirement", "canonical_id", "time_base",
    "safe_non_negative_integer", "safe_positive_integer",
)


def _source():
    return json.loads(SCHEMA_PATH.read_bytes())


def _raw(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def _oracle(definitions) -> str:
    # All keys in these manually selected fixtures are ASCII, so sorted JSON
    # independently matches the canonical UTF-16 ordering without its helper.
    material = {
        "schema_version": "timed-speech-registry-contract-projection-v1",
        "schema_dialect": DIALECT,
        "root_pointer": ROOT_POINTER,
        "definitions": definitions,
    }
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _reverse_keys(value):
    if isinstance(value, dict):
        return {key: _reverse_keys(child) for key, child in reversed(value.items())}
    if isinstance(value, list):
        return [_reverse_keys(child) for child in value]
    return value


def test_actual_schema_matches_independent_manual_closure_projection() -> None:
    schema = _source()
    expected = _oracle({name: schema["$defs"][name] for name in CLOSURE_NAMES})
    assert timed_speech_registry_contract_sha256(SCHEMA_PATH.read_bytes()) == expected
    assert expected != "sha256:" + hashlib.sha256(SCHEMA_PATH.read_bytes()).hexdigest()


def test_format_key_order_and_unrelated_profile_content_are_excluded() -> None:
    source = _source()
    expected = timed_speech_registry_contract_sha256(_raw(source))
    source["$id"] = "schema://other/profile"
    source["title"] = "Unrelated profile title"
    source["properties"]["calibration"] = {"description": "Unrelated profile property"}
    source["$defs"]["calibration"] = {"$id": "ignored-unreachable", "$ref": "external-unreachable"}
    source["$defs"]["unrelated"] = {"anything": [None, True, "annotation"]}
    reformatted = json.dumps(_reverse_keys(source), ensure_ascii=False, indent=3).encode() + b"\n"
    assert timed_speech_registry_contract_sha256(reformatted) == expected


@pytest.mark.parametrize("name", CLOSURE_NAMES)
def test_every_reachable_definition_including_annotations_affects_identity(name: str) -> None:
    source = _source()
    expected = timed_speech_registry_contract_sha256(_raw(source))
    source["$defs"][name]["description"] = "reachable annotation changes identity"
    assert timed_speech_registry_contract_sha256(_raw(source)) != expected


def test_transitive_cycle_and_shared_references_have_exact_finite_closure() -> None:
    definitions = {
        ROOT_NAME: {"allOf": [{"$ref": "#/$defs/Node"}, {"$ref": "#/$defs/Node"}]},
        "Node": {"anyOf": [{"$ref": ROOT_POINTER}, {"$ref": "#/$defs/Leaf"}]},
        "Leaf": False,
    }
    source = {"$schema": DIALECT, "properties": {ROOT_NAME: {"$ref": ROOT_POINTER}}, "$defs": definitions}
    assert timed_speech_registry_contract_sha256(_raw(source)) == _oracle(definitions)
    definitions[ROOT_NAME] = {"$ref": ROOT_POINTER}
    assert timed_speech_registry_contract_sha256(_raw(source)) == _oracle({ROOT_NAME: definitions[ROOT_NAME]})


@pytest.mark.parametrize("value", (True, False))
def test_root_definition_accepts_boolean_schema(value: bool) -> None:
    source = _source()
    source["$defs"][ROOT_NAME] = value
    assert timed_speech_registry_contract_sha256(_raw(source)) == _oracle({ROOT_NAME: value})


@pytest.mark.parametrize("name", ("A", "_private", "Name_1", "hyphen-name"))
def test_definition_names_match_frozen_ascii_identifier_subset(name: str) -> None:
    source = _source()
    source["$defs"][ROOT_NAME] = {"$ref": f"#/$defs/{name}"}
    source["$defs"][name] = True
    assert timed_speech_registry_contract_sha256(_raw(source)) == _oracle({ROOT_NAME: source["$defs"][ROOT_NAME], name: True})


@pytest.mark.parametrize("reference", (
    None, True, 1, [], {}, "", "#", "#/$defs", "#/$defs/", "#/$defs/missing",
    "#/$defs/sha256/type", "#/$defs/sha~0256", "#/$defs/sha~1256", "#/$defs/%73ha256",
    "#/$defs/sha256#anchor", "#sha256", "#/definitions/sha256", "https://example.test/schema",
    "other.json#/$defs/sha256", " #/$defs/sha256", "#/$defs/sha256\n", "#/$defs/a b",
    "#/$defs/中文", "#/$defs/0name", "#/$defs/dotted.name",
))
def test_malformed_missing_external_subpath_and_escaped_refs_are_rejected(reference) -> None:
    source = _source()
    source["$defs"][ROOT_NAME] = {"allOf": [{"$ref": reference}]}
    # Existing illegal-name entries still cannot make unsupported refs legal.
    source["$defs"].update({name: True for name in ("a b", "中文", "0name", "dotted.name", "sha~0256")})
    with pytest.raises(TimedSpeechContractError):
        timed_speech_registry_contract_sha256(_raw(source))


@pytest.mark.parametrize("keyword", ("$id", "$schema", "$anchor", "$dynamicAnchor", "$dynamicRef", "$recursiveAnchor", "$recursiveRef"))
@pytest.mark.parametrize("nested", (False, True))
def test_scope_and_reference_mechanisms_are_rejected_through_nested_objects_and_arrays(keyword: str, nested: bool) -> None:
    source = _source()
    forbidden = {keyword: "unsupported"}
    source["$defs"]["sha256"] = {"allOf": [{"properties": {"nested": forbidden}}]} if nested else forbidden
    with pytest.raises(TimedSpeechContractError, match="scope|anchors"):
        timed_speech_registry_contract_sha256(_raw(source))


@pytest.mark.parametrize("keyword", ("$anchor", "$dynamicAnchor", "$dynamicRef", "$recursiveAnchor", "$recursiveRef"))
def test_document_root_cannot_enable_alternate_reference_mechanisms(keyword: str) -> None:
    source = _source()
    source[keyword] = "unsupported"
    with pytest.raises(TimedSpeechContractError, match="reference mechanisms"):
        timed_speech_registry_contract_sha256(_raw(source))


@pytest.mark.parametrize("mutation", ("missing-dialect", "wrong-dialect", "properties", "defs", "missing-root", "missing-property", "inline-property", "extra-property", "wrong-root-ref"))
def test_schema_root_selection_is_exact(mutation: str) -> None:
    source = _source()
    if mutation == "missing-dialect":
        del source["$schema"]
    elif mutation == "wrong-dialect":
        source["$schema"] = "http://json-schema.org/draft-07/schema#"
    elif mutation in {"properties", "defs"}:
        source["$defs" if mutation == "defs" else mutation] = []
    elif mutation == "missing-root":
        del source["$defs"][ROOT_NAME]
    elif mutation == "missing-property":
        del source["properties"][ROOT_NAME]
    elif mutation == "inline-property":
        source["properties"][ROOT_NAME] = copy.deepcopy(source["$defs"][ROOT_NAME])
    elif mutation == "extra-property":
        source["properties"][ROOT_NAME]["description"] = "unsupported root sibling"
    else:
        source["properties"][ROOT_NAME] = {"$ref": "#/$defs/sha256"}
    with pytest.raises(TimedSpeechContractError):
        timed_speech_registry_contract_sha256(_raw(source))


@pytest.mark.parametrize("name", (ROOT_NAME, "sha256"))
@pytest.mark.parametrize("value", (None, [], "schema", 0))
def test_root_and_reachable_definitions_must_be_schema_objects_or_booleans(name: str, value) -> None:
    source = _source()
    source["$defs"][name] = value
    with pytest.raises(TimedSpeechContractError, match="object or boolean"):
        timed_speech_registry_contract_sha256(_raw(source))


@pytest.mark.parametrize("raw", (
    b"", b"not-json", b"\xff", b"[]", b"true", b"null", b"{\"x\":1,\"x\":2}",
    b"{\"x\":{\"a\":1,\"a\":2}}", b"{\"x\":1.0}", b"{\"x\":NaN}",
    b"{\"x\":Infinity}", b"{\"x\":9007199254740992}", b'{"x":"\\ud800"}',
    "{}", bytearray(b"{}"), None,
))
def test_strict_canonical_loader_failures_use_dedicated_error(raw) -> None:
    with pytest.raises(TimedSpeechContractError):
        timed_speech_registry_contract_sha256(raw)
