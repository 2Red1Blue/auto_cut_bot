"""Structural-only coverage for the B2b RunManifest payload source."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
COMMON_SOURCE = REPOSITORY_ROOT / "packages" / "autocut-kernel" / "src" / "autocut_kernel" / "contracts" / "source" / "2_1_3" / "common"
RUN_SOURCE = COMMON_SOURCE / "schemas" / "run"
PRIMITIVES_SOURCE = COMMON_SOURCE / "schemas" / "primitives"
SCHEMA_FILENAME = "run-manifest.schema.json"
SCHEMA_ID = "https://autocut.invalid/contracts/2.1.3/common/schemas/run/" + SCHEMA_FILENAME
AUTHORITY_HASH = "sha256:c34af7451919ad9a895644b40136062834b7ba9e857139f10b61f7dc51be67e9"


def _schema(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _validator() -> Draft202012Validator:
    resources = [_schema(RUN_SOURCE / SCHEMA_FILENAME)]
    resources.extend(_schema(PRIMITIVES_SOURCE / name) for name in ("artifact-ref.schema.json", "artifact-set-ref.schema.json", "scope.schema.json"))
    registry = Registry().with_resources((resource["$id"], Resource.from_contents(resource)) for resource in resources)
    return Draft202012Validator(resources[0], registry=registry, format_checker=FormatChecker())


def _artifact_ref(token: str) -> dict[str, str]:
    return {"artifact_id": f"artifact-{token}", "content_hash": "sha256:" + token[0] * 64}


def _artifact_set_ref(token: str) -> dict[str, str]:
    return {"artifact_set_id": f"set-{token}", "set_hash": "sha256:" + token[0] * 64}


def _valid_root() -> dict[str, object]:
    return {
        "run_id": "run-001", "job_id": "job-001", "runtime_mode": "pipeline_unattended", "runtime_version": "1.0.0", "contract_version": "2.1.3", "job_execution_id": "job-execution-001", "prior_job_start_slot_ref": _artifact_ref("a"), "run_lineage_id": "run-lineage-001", "publication_lineage_id": "publication-lineage-001", "derivation_reason": "root", "changed_ref_set": [], "affected_scope_set": [{"kind": "job", "run_id": "run-001", "job_id": "job-001"}], "recovery_budget_epoch_id": "recovery-epoch-001", "job_policy_ref": _artifact_ref("b"), "root_input_set_ref": _artifact_set_ref("c"), "parent_run_ref": None, "created_at": "2026-08-23T12:34:56.789Z"
    }


def _valid_derived() -> dict[str, object]:
    payload = _valid_root()
    payload.update({"run_id": "run-002", "runtime_mode": "agent_native", "derivation_reason": "policy_change", "affected_scope_set": [{"kind": "portfolio", "run_id": "run-002", "job_id": "job-001", "portfolio_id": "portfolio-001"}], "parent_run_ref": {"run_id": "run-001", **_artifact_ref("d")}})
    return payload


def _root_input_change() -> dict[str, object]:
    return {"kind": "root_input_set", "parent_ref": _artifact_set_ref("e"), "child_ref": _artifact_set_ref("f")}


def _policy_change() -> dict[str, object]:
    return {"kind": "job_policy", "parent_ref": _artifact_ref("e"), "child_ref": _artifact_ref("f")}


def _assert_invalid(value: object) -> None:
    assert list(_validator().iter_errors(value)), value


def test_run_manifest_schema_is_closed_payload_only_and_authority_bound() -> None:
    schema = _schema(RUN_SOURCE / SCHEMA_FILENAME)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"] == SCHEMA_ID
    assert AUTHORITY_HASH in schema["$comment"]
    assert "payload-only" in schema["$comment"]
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["additionalProperties"] is False


def test_run_manifest_accepts_root_and_derived_structural_branches() -> None:
    assert _validator().is_valid(_valid_root())
    assert _validator().is_valid(_valid_derived())


@pytest.mark.parametrize(
    "payload",
    [
        {key: value for key, value in _valid_root().items() if key != "run_id"},
        {**_valid_root(), "unexpected": True},
        {**_valid_root(), "runtime_mode": "interactive"},
        {**_valid_root(), "contract_version": "2.1.4"},
        {**_valid_root(), "runtime_version": ""},
        {**_valid_root(), "created_at": "2026-08-23T12:34:56.78Z"},
        {**_valid_root(), "created_at": "2026-08-23T12:34:56.789+00:00"},
        {**_valid_root(), "parent_run_ref": {"run_id": "run-000", **_artifact_ref("a")}},
        {**_valid_root(), "changed_ref_set": [_policy_change()]},
        {**_valid_root(), "affected_scope_set": []},
        {**_valid_root(), "affected_scope_set": [_valid_root()["affected_scope_set"][0], _valid_root()["affected_scope_set"][0]]},
        {**_valid_derived(), "parent_run_ref": None},
        {**_valid_derived(), "derivation_reason": "root"},
        {**_valid_derived(), "affected_scope_set": []},
        {**_valid_derived(), "affected_scope_set": ["job-001"]},
        {**_valid_derived(), "parent_run_ref": {"run_id": "run-001", "artifact_id": "artifact-a"}},
        {**_valid_derived(), "parent_run_ref": {"run_id": "run-001", **_artifact_ref("a"), "extra": True}},
    ],
)
def test_run_manifest_rejects_invalid_root_or_derived_branch_shapes(payload: object) -> None:
    _assert_invalid(payload)


@pytest.mark.parametrize(
    ("changed_ref_set", "is_valid"),
    [
        ([_root_input_change()], True), ([_policy_change()], True), ([_root_input_change(), _policy_change()], True),
        ([{"kind": "unknown", "parent_ref": _artifact_ref("a"), "child_ref": _artifact_ref("b")}], False),
        ([{**_root_input_change(), "unexpected": True}], False),
        ([_root_input_change(), _root_input_change()], False), ([_policy_change(), _root_input_change()], False),
        ([{"kind": "job_policy", "parent_ref": "artifact-a", "child_ref": _artifact_ref("b")}], False),
        ([{"kind": "root_input_set", "parent_ref": _artifact_set_ref("a"), "child_ref": {"artifact_set_id": "set-b"}}], False),
        ([{"kind": "job_policy", "parent_ref": _artifact_ref("a"), "child_ref": {**_artifact_ref("b"), "extra": True}}], False),
    ],
)
def test_run_manifest_changed_ref_set_is_closed_and_canonically_ordered(changed_ref_set: list[object], is_valid: bool) -> None:
    payload = _valid_derived()
    payload["changed_ref_set"] = changed_ref_set
    assert _validator().is_valid(payload) is is_valid


def test_run_manifest_schema_leaves_deferred_semantics_to_later_gates() -> None:
    payload = _valid_derived()
    payload["changed_ref_set"] = [_root_input_change()]
    payload["affected_scope_set"] = [{"kind": "job", "run_id": "another-run", "job_id": "another-job"}]
    assert _validator().is_valid(payload)
