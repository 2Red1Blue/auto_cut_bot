"""Regression coverage for the deliberately non-executable Registry skeleton."""

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
        "command_name": "BuildEditorialBlueprint", "command_version": "2.1.3",
        "allowed_scope_kinds": ["story"], "parameter_schema_uri": "schema://command/build-editorial/2.1.3/parameters",
        "parameter_schema_hash": SHA, "result_schema_uri": "schema://command/build-editorial/2.1.3/result",
        "result_schema_hash": SHA, "required_input_roles": ["portfolio"], "required_policy_roles": ["job_policy"],
        "lifecycle_slots": ["initial", "recovery"], "transaction_profile": "single_artifact_set_cas",
        "side_effect_class": "model_then_store", "required_capability": "runtime.execute_stage",
        "handler_id": "editorial-blueprint-handler", "handler_version": "1.0.0",
    }


def _trace() -> dict[str, object]:
    return {
        "contract_path": "stage-04#SA-DIALOGUE-001", "schema_path": "stage_04/span_variant.schema.json",
        "evaluator": {"component": "render-admission-evaluator", "version": "1.0.0", "contract_hash": SHA},
        "test_ids": {"pass": ["SA-T-040"], "fail": ["G-CUT-CONF-002"], "indeterminate": ["SA-T-041"]},
        "rollout_gate": "publication_enablement",
    }


def test_closed_profile_and_trace_parse_without_claiming_execution_readiness() -> None:
    contracts = import_module("autocut_kernel.contracts.compiler.registry")
    profile = contracts.CommandContractProfile.from_mapping(_profile())
    trace = contracts.ContractTrace.from_mapping(_trace())
    registry = contracts.PartialRegistrySet.build(command_profiles=(profile,), contract_traces=(trace,))
    assert profile.parameters_are_static_empty
    assert registry.ready is False
    with pytest.raises(contracts.RegistryValidationError, match="not executable"):
        registry.require_ready()


def test_profile_and_trace_reject_unknown_or_unclosed_values() -> None:
    contracts = import_module("autocut_kernel.contracts.compiler.registry")
    bad_profile = _profile() | {"runtime_default": "unsafe"}
    with pytest.raises(contracts.RegistryValidationError, match="exactly"):
        contracts.CommandContractProfile.from_mapping(bad_profile)
    bad_trace = _trace() | {"rollout_gate": "continue"}
    with pytest.raises(contracts.RegistryValidationError, match="closed rollout"):
        contracts.ContractTrace.from_mapping(bad_trace)
    bad_tests = _trace()
    bad_tests["test_ids"] = {"pass": [], "fail": ["F"], "indeterminate": []}
    with pytest.raises(contracts.RegistryValidationError, match="non-empty"):
        contracts.ContractTrace.from_mapping(bad_tests)
    unsafe_path = _trace() | {"schema_path": "../outside.schema.json"}
    with pytest.raises(contracts.RegistryValidationError, match="safe relative"):
        contracts.ContractTrace.from_mapping(unsafe_path)


def test_partial_registry_rejects_duplicate_command_or_trace_identity() -> None:
    contracts = import_module("autocut_kernel.contracts.compiler.registry")
    profile = contracts.CommandContractProfile.from_mapping(_profile())
    trace = contracts.ContractTrace.from_mapping(_trace())
    with pytest.raises(contracts.RegistryValidationError, match="duplicate command"):
        contracts.PartialRegistrySet.build(command_profiles=(profile, profile), contract_traces=(trace,))
    with pytest.raises(contracts.RegistryValidationError, match="duplicate contract trace"):
        contracts.PartialRegistrySet.build(command_profiles=(profile,), contract_traces=(trace, trace))
