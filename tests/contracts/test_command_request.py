"""Closed wire-shell coverage for shared v2.1.3 Command requests."""

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


def _request(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "command_id": "cmd_1", "command_name": "BuildEditorialBlueprint", "command_version": "2.1.3",
        "run_manifest_ref": {"artifact_id": "art_run", "content_hash": SHA},
        "scope": {"kind": "story", "run_id": "run_1", "job_id": "job_1", "portfolio_id": "portfolio_1", "story_id": "story_1"},
        "input_refs": [{"artifact_id": "art_input", "content_hash": SHA}],
        "policy_refs": [{"artifact_id": "art_policy", "content_hash": SHA}], "parameters": {},
        "invocation_id": "inv_1", "idempotency_key": SHA, "requested_capability": "runtime.execute_stage",
    }
    return value | overrides


def test_command_request_accepts_closed_nonbootstrap_shell() -> None:
    contracts = import_module("autocut_kernel.contracts")
    request = contracts.CommandRequest.from_mapping(_request())
    assert request.scope["kind"] == "story"
    assert request.run_manifest_ref is not None
    assert request.profile_resolved is False
    with pytest.raises(contracts.CommandValidationError, match="not dispatcher-admitted"):
        request.require_profile_resolved()
    with pytest.raises(TypeError):
        request.scope["kind"] = "job"  # type: ignore[index]


@pytest.mark.parametrize(
    "override",
    [
        {"parameters": {"temperature": 0.1}},
        {"requested_capability": "runtime.admin"},
        {"run_manifest_ref": None},
        {"idempotency_key": "sha256:" + "A" * 64},
        {"input_refs": ["art_input"]},
    ],
)
def test_command_request_rejects_runtime_controlled_or_unstructured_fields(override: dict[str, object]) -> None:
    contracts = import_module("autocut_kernel.contracts")
    with pytest.raises(contracts.CommandValidationError):
        contracts.CommandRequest.from_mapping(_request(**override))


def test_bootstrap_and_recovery_parameter_boundaries_are_closed() -> None:
    contracts = import_module("autocut_kernel.contracts")
    prepare = _request(
        command_name="PrepareMediaEvidence", run_manifest_ref=None,
        scope={"kind": "root_input", "job_id": "job_1", "root_input_id": "root_1"},
        requested_capability="media.prepare",
    )
    assert contracts.CommandRequest.from_mapping(prepare).run_manifest_ref is None
    with pytest.raises(contracts.CommandValidationError, match="invalid bootstrap scope"):
        contracts.CommandRequest.from_mapping(prepare | {"scope": _request()["scope"]})
    recovery = _request(
        command_name="RecoverScope", requested_capability="runtime.execute_recovery",
        parameters={"kind": "retry", "strategy_id": "s", "strategy_version": "1.0.0", "recovery_reservation_ref": {}, "strategy_parameters": {}},
    )
    assert contracts.CommandRequest.from_mapping(recovery).parameters["kind"] == "retry"
    with pytest.raises(contracts.CommandValidationError, match="exactly"):
        contracts.CommandRequest.from_mapping(recovery | {"parameters": {}})
