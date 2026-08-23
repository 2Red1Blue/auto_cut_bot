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
PACK = ROOT / "packages/autocut-kernel/src/autocut_kernel/contracts/source/2_1_3/stage_04"
B_A = "50f78ea0b7f754eb8f91ef800924b86da25b3083"
B_P = "eb7e4181f63da308c405bad6d99fcd085cfdd98a"


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


def test_manifest_has_exact_pins_and_closed_inventory():
    manifest = load(PACK / "contributions/stage-04-owner-contribution.manifest.json")
    schema = load(PACK / "contributions/stage-04-owner-contribution.manifest.schema.json")
    validator = Draft202012Validator(schema)
    assert validator.is_valid(manifest)
    assert manifest["producer_base_commit"] == B_A
    assert manifest["b_input"] == {
        "handoff_git_commit": B_A,
        "handoff_raw_sha256": "sha256:07fd0e9d4dbe03050f9aa3f4823755e4ba1e7f4852a163ab2fa291c0b3710eb6",
        "producer_git_commit": B_P,
        "used_primitive_blobs": [
            {"path": "schemas/primitives/artifact-ref.schema.json", "raw_sha256": "sha256:9df321d5ccc33dd8ff307aae6f33d1e4d2accfc278ea4fb5ed11072e5e5295db"},
            {"path": "schemas/primitives/domain-ref.schema.json", "raw_sha256": "sha256:72211809fa19f967345ccc41574fea5319feb40bcbde05a3564cf6bc8c63f896"},
            {"path": "schemas/primitives/source-span-ref.schema.json", "raw_sha256": "sha256:586c57b03f44a455478d722ab3012dcb33932fcc49fe0d0d847d9cfa2043bb29"},
        ],
    }
    assert not {"status", "readiness", "registry", "generated", "result", "action"} & set(manifest)
    assert len(paths()) == manifest["producer_file_count"] == 13
    assert len(manifest["producer_files"]) == 11
    assert [item["path"] for item in manifest["producer_files"]] == sorted(item["path"] for item in manifest["producer_files"])
    for item in manifest["producer_files"]:
        assert item["raw_sha256"] == sha(PACK / item["path"])


def test_jcs_duplicate_rejection_and_self_exclusion_are_exact():
    manifest_path = PACK / "contributions/stage-04-owner-contribution.manifest.json"
    manifest = load(manifest_path)
    for relative in paths():
        path = PACK / relative
        assert path.read_bytes() == canonical_json_bytes(load(path))
    with pytest.raises((ValueError, CanonicalizationError)):
        strict(manifest_path.read_bytes() + b"\n", str(manifest_path))
    with pytest.raises(CanonicalizationError, match="duplicate JSON object key"):
        strict(manifest_path.read_bytes()[:-1] + b',"format":"duplicate"}', str(manifest_path))
    protocol = manifest["closed_self_exclusion_protocol"]
    external = {item["path"] for item in protocol["external_attestation_bound_files"]}
    assert protocol["producer_file_count_breakdown"] == {"external_attestation_bound_files": 2, "hashed_source_files": 11, "total_files": 13}
    assert external == {manifest["producer_manifest_path"], "contributions/stage-04-owner-contribution.manifest.schema.json"}
    assert paths() == external | {item["path"] for item in manifest["producer_files"]}


def test_structural_schemas_accept_only_synthetic_layouts():
    fixture = load(PACK / "fixtures/stage-04-structural-valid.json")
    assert fixture["fixture_kind"] == "structural_synthetic_only"
    schemas = {
        "time_and_media": "time-and-media.local-shape.json",
        "boundary_selection_policy": "boundary-selection-policy.local-shape.json",
        "media_evidence_wire": "media-evidence-wire.local-shape.json",
        "span_variant": "span-variant.local-shape.json",
        "recipe_and_junction": "recipe-and-junction.local-shape.json",
        "compilation_report": "compilation-report.local-shape.json",
        "certificate_reference": "certificate-reference.local-shape.json",
    }
    forbidden = {"pass", "safety_rule_ids", "applied_policy_rule_ids", "status", "result", "action", "outcome", "evaluator", "trace", "scope"}
    for name, filename in schemas.items():
        shape = load(PACK / "shapes" / filename)
        assert shape["additionalProperties"] is False
        assert not set(walk_keys(shape)) & forbidden
        assert Draft202012Validator(shape).is_valid({name: fixture[name]})
    float_tick = deepcopy(fixture["time_and_media"])
    float_tick["timeline_clock"]["tick"] = 0.5
    assert not Draft202012Validator(load(PACK / "shapes/time-and-media.local-shape.json")).is_valid({"time_and_media": float_tick})
    self_attested = deepcopy(fixture["span_variant"])
    self_attested["boundary_proof_layout"]["pass"] = True
    assert not Draft202012Validator(load(PACK / "shapes/span-variant.local-shape.json")).is_valid({"span_variant": self_attested})


def test_manifest_schema_rejects_pin_and_state_mutations():
    manifest = load(PACK / "contributions/stage-04-owner-contribution.manifest.json")
    validator = Draft202012Validator(load(PACK / "contributions/stage-04-owner-contribution.manifest.schema.json"))
    wrong_pin = deepcopy(manifest)
    wrong_pin["b_input"]["producer_git_commit"] = "0" * 40
    assert not validator.is_valid(wrong_pin)
    wrong_path = deepcopy(manifest)
    wrong_path["producer_files"][0]["path"] = "shapes/invented.json"
    assert not validator.is_valid(wrong_path)
    state = deepcopy(manifest)
    state["status"] = "invented"
    assert not validator.is_valid(state)
