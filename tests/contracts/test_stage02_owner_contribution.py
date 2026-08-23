from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from pathlib import Path

import pytest
from autocut_kernel.contracts.compiler.canonical import (
    CanonicalizationError,
    canonical_json_bytes,
    load_canonical_json_bytes,
)
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
PACK = ROOT / "packages/autocut-kernel/src/autocut_kernel/contracts/source/2_1_3/stage_02"


def load(path):
    return json.loads(path.read_text())


def load_strict_jcs_bytes(raw, *, origin):
    value, canonical = load_canonical_json_bytes(raw, origin=origin)
    if raw != canonical:
        raise ValueError(f"{origin}: source bytes must be canonical JCS")
    return value


def load_strict_jcs(path):
    return load_strict_jcs_bytes(path.read_bytes(), origin=str(path))


def file_paths(root):
    return {
        (Path(directory) / filename).relative_to(root).as_posix()
        for directory, _, filenames in os.walk(root)
        for filename in filenames
    }


def walk_keys(value):
    if isinstance(value, dict):
        for key, nested in value.items():
            yield key
            yield from walk_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from walk_keys(nested)


def walk_strings(value):
    if isinstance(value, dict):
        for nested in value.values():
            yield from walk_strings(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from walk_strings(nested)
    elif isinstance(value, str):
        yield value


def test_stage02_owner_source_is_closed_partial_and_pinned():
    manifest = load_strict_jcs(PACK / "contributions/stage-02-owner-contribution.manifest.json")
    schema = load_strict_jcs(PACK / "contributions/stage-02-owner-contribution.manifest.schema.json")
    assert Draft202012Validator(schema).is_valid(manifest)
    assert manifest["producer_base_commit"] == "50f78ea0b7f754eb8f91ef800924b86da25b3083"
    assert not {"status", "readiness", "result", "action"} & set(manifest)
    assert manifest["b_input"]["used_primitive_blobs"] == [
        {"path": "schemas/primitives/artifact-ref.schema.json", "raw_sha256": "sha256:9df321d5ccc33dd8ff307aae6f33d1e4d2accfc278ea4fb5ed11072e5e5295db"},
        {"path": "schemas/primitives/artifact-set-ref.schema.json", "raw_sha256": "sha256:ddbbaf9bb866c97fa5c4ee9bbe1f78053f03a09abed529846bc5a3b0893a97a4"},
        {"path": "schemas/primitives/domain-ref.schema.json", "raw_sha256": "sha256:72211809fa19f967345ccc41574fea5319feb40bcbde05a3564cf6bc8c63f896"},
        {"path": "schemas/primitives/immutable-blob-ref.schema.json", "raw_sha256": "sha256:52d4ea27825976cecc86e2d96d2388acb0aa885799adb708d6f0db616cc10678"},
        {"path": "schemas/primitives/scope.schema.json", "raw_sha256": "sha256:e0376bbb53ea1ddcc69b371231fb81d2bd948f7f52565e452df413e9499b53ac"},
        {"path": "schemas/primitives/source-span-ref.schema.json", "raw_sha256": "sha256:586c57b03f44a455478d722ab3012dcb33932fcc49fe0d0d847d9cfa2043bb29"},
    ]


def test_manifest_schema_rejects_mutated_pins_and_inventory():
    manifest = load_strict_jcs(PACK / "contributions/stage-02-owner-contribution.manifest.json")
    validator = Draft202012Validator(load_strict_jcs(PACK / "contributions/stage-02-owner-contribution.manifest.schema.json"))
    wrong_pin = deepcopy(manifest)
    wrong_pin["b_input"]["used_primitive_blobs"][0]["raw_sha256"] = "sha256:" + "0" * 64
    assert not validator.is_valid(wrong_pin)
    invented_file = deepcopy(manifest)
    invented_file["producer_files"][0]["path"] = "shapes/invented.local-shape.json"
    assert not validator.is_valid(invented_file)
    wrong_external = deepcopy(manifest)
    wrong_external["closed_self_exclusion_protocol"]["external_attestation_bound_files"][0]["path"] = "contributions/invented.json"
    assert not validator.is_valid(wrong_external)
    injected_status = deepcopy(manifest)
    injected_status["status"] = {"owner_payload_source_partial": True}
    assert not validator.is_valid(injected_status)


def test_all_owner_json_is_strict_jcs_and_rejects_duplicates():
    for relative_path in file_paths(PACK):
        path = PACK / relative_path
        raw = path.read_bytes()
        assert raw == canonical_json_bytes(load_strict_jcs(path))
    manifest_path = PACK / "contributions/stage-02-owner-contribution.manifest.json"
    with pytest.raises((ValueError, CanonicalizationError)):
        load_strict_jcs_bytes(manifest_path.read_bytes() + b"\n", origin=str(manifest_path))
    with pytest.raises(CanonicalizationError, match="duplicate JSON object key"):
        load_strict_jcs_bytes(manifest_path.read_bytes()[:-1] + b',"format":"duplicate"}', origin=str(manifest_path))


def test_source_inventory_and_external_attestation_boundary_are_exact():
    manifest = load_strict_jcs(PACK / "contributions/stage-02-owner-contribution.manifest.json")
    names = file_paths(PACK)
    protocol = manifest["closed_self_exclusion_protocol"]
    external = {item["path"] for item in protocol["external_attestation_bound_files"]}
    assert len(names) == manifest["producer_file_count"] == 12
    assert len(manifest["producer_files"]) == 10
    assert protocol["producer_file_count_breakdown"] == {"external_attestation_bound_files": 2, "hashed_source_files": 10, "total_files": 12}
    assert external == {manifest["producer_manifest_path"], "contributions/stage-02-owner-contribution.manifest.schema.json"}
    assert names == external | {item["path"] for item in manifest["producer_files"]}
    assert [item["path"] for item in manifest["producer_files"]] == sorted(item["path"] for item in manifest["producer_files"])
    for item in manifest["producer_files"]:
        assert item["raw_sha256"] == "sha256:" + hashlib.sha256((PACK / item["path"]).read_bytes()).hexdigest()
    validation = manifest["repository_validation"]
    assert validation["path"] == "tests/contracts/test_stage02_owner_contribution.py"
    assert validation["raw_sha256"] == "sha256:" + hashlib.sha256((ROOT / validation["path"]).read_bytes()).hexdigest()


def test_shapes_and_worklist_remain_structural_and_unbound():
    forbidden_keys = {"scope", "scope_ref", "allowed_scope", "allowed_scopes", "intended_scope", "details", "stage_details", "admission", "decision", "result", "action", "actions", "outcome", "outcomes", "evaluator", "evaluation", "registration", "rule_id", "rule_version", "trace"}
    shapes = list((PACK / "shapes").glob("*.json"))
    assert len(shapes) == 7
    for path in shapes:
        document = load_strict_jcs(path)
        assert document.get("additionalProperties") is False
        assert not set(walk_keys(document)) & forbidden_keys
        assert not any(token in value for value in walk_strings(document) for token in ("SD-", "SD-T-", "Registry", "Envelope"))
    worklist = load_strict_jcs(PACK / "contracts/stage-02-source-obligations.json")
    assert worklist["format"] == "autocut.stage-02-source-anchor-worklist/v1"
    assert worklist["source_anchors"]
    assert not set(walk_keys(worklist)) & forbidden_keys


def test_structural_fixture_validates_only_local_shapes():
    fixture = load_strict_jcs(PACK / "fixtures/stage-02-structural-valid.json")
    assert fixture["fixture_kind"] == "structural_synthetic_only"
    schemas = {
        "candidate_measurement": "candidate-measurement.local-shape.json",
        "candidate_capability": "candidate-capability.local-shape.json",
        "story_design": "story-design.local-shape.json",
        "candidate_catalog": "candidate-catalog.local-shape.json",
        "proposal_set": "proposal-set.local-shape.json",
        "portfolio": "portfolio.local-shape.json",
        "portfolio_local_fields": "portfolio-local-fields.local-shape.json",
    }
    for key, filename in schemas.items():
        assert Draft202012Validator(load_strict_jcs(PACK / "shapes" / filename)).is_valid({key: fixture[key]})
