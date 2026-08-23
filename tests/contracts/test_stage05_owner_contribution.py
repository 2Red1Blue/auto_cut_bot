from __future__ import annotations

import hashlib
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
PACK = ROOT / "packages/autocut-kernel/src/autocut_kernel/contracts/source/2_1_3/stage_05"
AUTHORITY = "77f6db99019eb3e24e7b20ac92a2463a3bb3156c"
B_P = "eb7e4181f63da308c405bad6d99fcd085cfdd98a"
B_A = "50f78ea0b7f754eb8f91ef800924b86da25b3083"
FORBIDDEN = {
    "status", "state", "outcome", "decision", "result", "pass", "fail",
    "indeterminate", "allow", "deny", "readiness", "selection", "admission",
    "action", "registry", "envelope", "artifact", "command", "renderer",
    "evaluator", "release", "platform", "transaction", "producer", "writer", "scope", "policy",
}


def strict(raw: bytes, origin: str):
    value, canonical = load_canonical_json_bytes(raw, origin=origin)
    if raw != canonical:
        raise ValueError(f"{origin}: source bytes must be canonical JCS")
    return value


def load(path: Path):
    return strict(path.read_bytes(), str(path))


def paths():
    return {path.relative_to(PACK).as_posix() for path in PACK.rglob("*.json")}


def sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def walk_keys(value):
    if isinstance(value, dict):
        for key, nested in value.items():
            yield key
            yield from walk_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from walk_keys(nested)


def test_manifest_is_closed_pinned_and_exhaustive():
    manifest = load(PACK / "contributions/stage-05-owner-contribution.manifest.json")
    schema = load(PACK / "contributions/stage-05-owner-contribution.manifest.schema.json")
    assert Draft202012Validator(schema).is_valid(manifest)
    assert manifest["authority_commit"] == AUTHORITY
    assert manifest["producer_base_commit"] == B_A
    assert manifest["b_input"] == {
        "handoff_git_commit": B_A,
        "handoff_raw_sha256": "sha256:07fd0e9d4dbe03050f9aa3f4823755e4ba1e7f4852a163ab2fa291c0b3710eb6",
        "producer_git_commit": B_P,
        "selected_ids": ["B0-001", "B0-002", "B0-005", "B0-006", "B0-007", "B0-008", "B0-009", "B0-010", "B0-011"],
        "used_primitive_blobs": [{"path": "schemas/primitives/artifact-ref.schema.json", "raw_sha256": "sha256:9df321d5ccc33dd8ff307aae6f33d1e4d2accfc278ea4fb5ed11072e5e5295db"}],
    }
    assert manifest["producer_file_count"] == 14
    assert len(manifest["producer_files"]) == 12
    assert [item["path"] for item in manifest["producer_files"]] == sorted(item["path"] for item in manifest["producer_files"])
    external = {item["path"] for item in manifest["closed_self_exclusion_protocol"]["external_attestation_bound_files"]}
    assert external == {manifest["producer_manifest_path"], "contributions/stage-05-owner-contribution.manifest.schema.json"}
    assert paths() == external | {item["path"] for item in manifest["producer_files"]}
    for item in manifest["producer_files"]:
        assert item["raw_sha256"] == sha(PACK / item["path"])


def test_jcs_rejects_duplicates_and_noncanonical_bytes():
    manifest_path = PACK / "contributions/stage-05-owner-contribution.manifest.json"
    for relative in paths():
        path = PACK / relative
        assert path.read_bytes() == canonical_json_bytes(load(path))
    with pytest.raises((ValueError, CanonicalizationError)):
        strict(manifest_path.read_bytes() + b"\n", str(manifest_path))
    with pytest.raises(CanonicalizationError, match="duplicate JSON object key"):
        strict(manifest_path.read_bytes()[:-1] + b',"format":"duplicate"}', str(manifest_path))


def test_shapes_accept_synthetic_layouts_only_and_reject_semantics():
    fixture = load(PACK / "fixtures/stage-05-structural-valid.json")
    assert fixture["fixture_kind"] == "structural_synthetic_only"
    schemas = {
        "render_input_wire": "render-input-wire.local-shape.json",
        "render_attempt_observation": "render-attempt-observation.local-shape.json",
        "rendered_asset_wire": "rendered-asset-wire.local-shape.json",
        "qc_metric_profile": "qc-metric-profile.local-shape.json",
        "qc_observation_wire": "qc-observation-wire.local-shape.json",
        "release_lineage_wire": "release-lineage-wire.local-shape.json",
        "publication_target_surface_wire": "publication-target-surface-wire.local-shape.json",
        "publication_evidence_wire": "publication-evidence-wire.local-shape.json",
    }
    prohibited = FORBIDDEN - {"artifact", "command", "release", "platform", "transaction", "policy"}
    for name, filename in schemas.items():
        schema = load(PACK / "shapes" / filename)
        assert schema["additionalProperties"] is False
        assert not set(walk_keys(schema)) & prohibited
        assert Draft202012Validator(schema).is_valid({name: fixture[name]})
        mutation = deepcopy(fixture[name])
        mutation["status"] = "synthetic"
        assert not Draft202012Validator(schema).is_valid({name: mutation})
    metric = deepcopy(fixture["qc_metric_profile"])
    metric["metric_kind"] = "unknown"
    assert not Draft202012Validator(load(PACK / "shapes/qc-metric-profile.local-shape.json")).is_valid({"qc_metric_profile": metric})


def test_manifest_schema_rejects_semantic_and_pin_mutations():
    manifest = load(PACK / "contributions/stage-05-owner-contribution.manifest.json")
    validator = Draft202012Validator(load(PACK / "contributions/stage-05-owner-contribution.manifest.schema.json"))
    wrong_pin = deepcopy(manifest)
    wrong_pin["b_input"]["producer_git_commit"] = "0" * 40
    assert not validator.is_valid(wrong_pin)
    wrong_path = deepcopy(manifest)
    wrong_path["producer_files"][0]["path"] = "shapes/invented.json"
    assert not validator.is_valid(wrong_path)
    for field in ("status", "result", "decision", "readiness", "action"):
        mutation = deepcopy(manifest)
        mutation[field] = "synthetic"
        assert not validator.is_valid(mutation)
