from __future__ import annotations

import hashlib
import json
import os
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


def test_stage01_owner_source_is_closed_and_partial():
    manifest=load(PACK/"contributions/stage-01-owner-contribution.manifest.json")
    schema=load(PACK/"contributions/stage-01-owner-contribution.manifest.schema.json")
    assert Draft202012Validator(schema).is_valid(manifest)
    assert manifest["status"] == {"owner_payload_source_partial": True, "stage_01_ready": False}
    assert manifest["b_input"]["selected_ids"] == ["B0-001", "B0-002", "B0-005", "B0-006", "B0-007", "B0-008", "B0-009", "B0-010", "B0-011"]
    assert {item["path"] for item in manifest["b_input"]["used_primitive_blobs"]} == {"schemas/primitives/immutable-blob-ref.schema.json", "schemas/primitives/domain-ref.schema.json", "schemas/primitives/source-span-ref.schema.json"}


def test_stage01_source_has_no_registry_or_runtime_identity():
    names = file_paths(PACK)
    assert not any(part in {"registry","generated","commands","authority","handoff"} for name in names for part in name.split("/"))
    assert "artifacts.yaml" not in names and "rules.yaml" not in names and "traces.yaml" not in names
    manifest = load(PACK / "contributions/stage-01-owner-contribution.manifest.json")
    assert len(names) == manifest["producer_file_count"] == 14
    assert len(manifest["producer_files"]) == 13
    assert [item["path"] for item in manifest["producer_files"]] == sorted(item["path"] for item in manifest["producer_files"])
    assert names == {manifest["producer_manifest_path"]} | {item["path"] for item in manifest["producer_files"]}
    for item in manifest["producer_files"]:
        assert item["raw_sha256"] == "sha256:" + hashlib.sha256((PACK / item["path"]).read_bytes()).hexdigest()


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
