from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
PACK = ROOT / "packages/autocut-kernel/src/autocut_kernel/contracts/source/2_1_3/stage_01"


def load(path):
    return json.loads(path.read_text())


def file_paths(root):
    return {
        (Path(directory) / filename).relative_to(root).as_posix()
        for directory, _, filenames in os.walk(root)
        for filename in filenames
    }


def schema_documents(root):
    return [load(path) for path in root.glob("schemas/**/*.json")]


def schema_keys(value):
    if isinstance(value, dict):
        yield from value
        for nested in value.values():
            yield from schema_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from schema_keys(nested)


def schema_strings(value):
    if isinstance(value, dict):
        for nested in value.values():
            yield from schema_strings(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from schema_strings(nested)
    elif isinstance(value, str):
        yield value


def test_stage01_owner_source_is_closed_and_partial():
    manifest=load(PACK/"contributions/stage-01-owner-contribution.manifest.json")
    schema=load(PACK/"contributions/stage-01-owner-contribution.manifest.schema.json")
    assert Draft202012Validator(schema).is_valid(manifest)
    assert manifest["status"] == {"owner_payload_source_partial": True, "stage_01_ready": False}
    assert manifest["b_input"]["selected_ids"] == ["B0-001", "B0-002", "B0-005", "B0-006", "B0-007", "B0-008", "B0-009", "B0-010", "B0-011"]
    assert manifest["b_input"]["used_primitive_blobs"] == [
        {"path": "schemas/primitives/immutable-blob-ref.schema.json", "raw_sha256": "sha256:52d4ea27825976cecc86e2d96d2388acb0aa885799adb708d6f0db616cc10678"},
        {"path": "schemas/primitives/domain-ref.schema.json", "raw_sha256": "sha256:72211809fa19f967345ccc41574fea5319feb40bcbde05a3564cf6bc8c63f896"},
        {"path": "schemas/primitives/source-span-ref.schema.json", "raw_sha256": "sha256:586c57b03f44a455478d722ab3012dcb33932fcc49fe0d0d847d9cfa2043bb29"},
    ]


def test_stage01_owner_manifest_schema_rejects_mutated_primitive_blob_path_or_hash():
    manifest = load(PACK / "contributions/stage-01-owner-contribution.manifest.json")
    schema = load(PACK / "contributions/stage-01-owner-contribution.manifest.schema.json")
    validator = Draft202012Validator(schema)

    wrong_path = deepcopy(manifest)
    wrong_path["b_input"]["used_primitive_blobs"][0]["path"] = "schemas/primitives/wrong.schema.json"
    assert not validator.is_valid(wrong_path)

    wrong_hash = deepcopy(manifest)
    wrong_hash["b_input"]["used_primitive_blobs"][1]["raw_sha256"] = "sha256:" + "0" * 64
    assert not validator.is_valid(wrong_hash)


def test_rule_obligations_are_a_non_executable_source_anchor_worklist():
    worklist = load(PACK / "contracts/stage-01-rule-obligations.json")
    assert worklist == {
        "format": "autocut.stage-01-source-anchor-worklist/v1",
        "contract_version": "2.1.3",
        "source_anchors": [
            {
                "source_anchor": "v2-stage-01-knowledge-chain-v2.md#7",
                "expected_shape": "knowledge-chain source material",
            }
        ],
    }


def test_stage01_source_has_no_registry_or_runtime_identity():
    names = file_paths(PACK)
    assert not any(part in {"registry","generated","commands","authority","handoff"} for name in names for part in name.split("/"))
    assert "artifacts.yaml" not in names and "rules.yaml" not in names and "traces.yaml" not in names
    manifest = load(PACK / "contributions/stage-01-owner-contribution.manifest.json")
    assert len(names) == manifest["producer_file_count"] == 13
    assert len(manifest["producer_files"]) == 12
    assert [item["path"] for item in manifest["producer_files"]] == sorted(item["path"] for item in manifest["producer_files"])
    assert names == {manifest["producer_manifest_path"]} | {item["path"] for item in manifest["producer_files"]}
    for item in manifest["producer_files"]:
        assert item["raw_sha256"] == "sha256:" + hashlib.sha256((PACK / item["path"]).read_bytes()).hexdigest()


def test_stage01_schemas_exclude_unowned_admission_diagnostic_artifact_and_rule_semantics():
    names = file_paths(PACK)
    assert not any(part == "admission" for name in names for part in name.split("/"))

    documents = schema_documents(PACK)
    keys = {key for document in documents for key in schema_keys(document)}
    assert not keys & {"scope", "coverage_mode", "diagnostic_refs", "rule_id", "rule_version", "graph_ref", "policy_ref"}

    strings = {item for document in documents for item in schema_strings(document)}
    assert "https://autocut.invalid/contracts/2.1.3/common/schemas/primitives/artifact-ref.schema.json" not in strings


def test_repository_validation_is_a_named_non_producer_b_p_evidence_path():
    manifest = load(PACK / "contributions/stage-01-owner-contribution.manifest.json")
    validation = manifest["repository_validation"]
    assert validation["path"] == "tests/contracts/test_stage01_owner_contribution.py"
    assert validation["path"] not in file_paths(PACK)
    assert validation["raw_sha256"] == "sha256:" + hashlib.sha256((ROOT / validation["path"]).read_bytes()).hexdigest()


def test_structural_fixture_is_schema_only():
    fixture = load(PACK / "fixtures/stage-01-owner-source-valid.json")
    schema = load(PACK / "schemas/source-knowledge-input-set.schema.json")
    assert Draft202012Validator(schema).is_valid(fixture["source_knowledge_input_set"])
