"""Fail-closed coverage for the RegistrySet source boundary."""

from __future__ import annotations

from importlib import import_module
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
KERNEL_SOURCE = REPOSITORY_ROOT / "packages" / "autocut-kernel" / "src"
SHA = "sha256:" + "a" * 64


@pytest.fixture(autouse=True)
def _load_kernel_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.syspath_prepend(str(KERNEL_SOURCE))


def _profile() -> dict[str, object]:
    return {
        "command_name": "BuildEditorialBlueprint",
        "command_version": "2.1.3",
        "allowed_scope_kinds": ["story"],
        "parameter_schema_uri": "schema://command/build-editorial/2.1.3/parameters",
        "parameter_schema_hash": SHA,
        "result_schema_uri": "schema://command/build-editorial/2.1.3/result",
        "result_schema_hash": SHA,
        "required_input_roles": ["portfolio"],
        "required_policy_roles": ["job_policy"],
        "lifecycle_slots": ["initial", "recovery"],
        "artifact_set_plan": {"kind": "fixed", "artifact_set_profile": "stage_admission"},
        "commit_protocol": "artifact_set_commit",
        "side_effect_class": "model_then_store",
        "required_capability": "runtime.execute_stage",
        "handler_id": "editorial-blueprint-handler",
        "handler_version": "1.0.0",
        "idempotency_algorithm_id": "editorial-idempotency",
        "idempotency_algorithm_version": "1.0.0",
        "idempotency_algorithm_contract_hash": SHA,
    }


def _trace() -> dict[str, object]:
    return {
        "contract_path": "stage-04#SA-DIALOGUE-001",
        "schema_path": "stage_04/span_variant.schema.json",
        "evaluator": {
            "component": "render-admission-evaluator",
            "version": "1.0.0",
            "contract_hash": SHA,
        },
        "test_ids": {
            "pass": ["SA-T-040"],
            "fail": ["G-CUT-CONF-002"],
            "indeterminate": ["SA-T-041"],
        },
        "rollout_gate": "publication_enablement",
    }


def _source() -> dict[str, object]:
    return {
        "artifacts": [],
        "commands": [_profile()],
        "rules": [],
        "strategies": [],
        "traces": [_trace()],
    }


def test_old_flat_loader_input_is_rejected_fail_closed() -> None:
    contracts = import_module("autocut_kernel.contracts")
    source = _source()
    with pytest.raises(contracts.RegistryValidationError, match="validated RegistrySourceManifest"):
        contracts.RegistrySet.from_mapping(
            source, expected_source_hash=contracts.canonical_json_hash(source)
        )


def test_loader_rejects_hash_drift_unknown_or_missing_categories() -> None:
    contracts = import_module("autocut_kernel.contracts")
    source = _source()
    with pytest.raises(contracts.RegistryValidationError, match="validated RegistrySourceManifest"):
        contracts.RegistrySet.from_mapping(source, expected_source_hash="sha256:" + "b" * 64)
    source_with_unknown = source | {"unreviewed_default": {}}
    with pytest.raises(contracts.RegistryValidationError, match="validated RegistrySourceManifest"):
        contracts.RegistrySet.from_mapping(
            source_with_unknown,
            expected_source_hash=contracts.canonical_json_hash(source_with_unknown),
        )
    missing = {key: value for key, value in source.items() if key != "strategies"}
    with pytest.raises(contracts.RegistryValidationError, match="validated RegistrySourceManifest"):
        contracts.RegistrySet.from_mapping(
            missing, expected_source_hash=contracts.canonical_json_hash(missing)
        )


def test_opaque_artifact_rule_and_strategy_entries_are_rejected() -> None:
    contracts = import_module("autocut_kernel.contracts")
    source = _source() | {
        "artifacts": [{"unverified": "artifact"}],
        "rules": [{"unverified": "rule"}],
        "strategies": [{"unverified": "strategy"}],
    }
    with pytest.raises(contracts.RegistryValidationError, match="validated RegistrySourceManifest"):
        contracts.RegistrySet.from_mapping(
            source, expected_source_hash=contracts.canonical_json_hash(source)
        )
