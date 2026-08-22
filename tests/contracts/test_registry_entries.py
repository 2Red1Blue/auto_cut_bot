"""Closed entry-family grammar regression coverage."""

from __future__ import annotations

from pathlib import Path

import pytest

KERNEL_SOURCE = Path(__file__).resolve().parents[2] / "packages" / "autocut-kernel" / "src"
SHA = "sha256:" + "a" * 64


def _ownership() -> dict[str, str]:
    return {
        "owner_pack": "common",
        "owner_source_path": "common/entries.json",
        "owner_source_hash": SHA,
        "owner_contract_path": "common/contract.json",
        "owner_contract_hash": SHA,
    }


def _contract_trace(path: str) -> dict[str, object]:
    return {
        "entry_kind": "contract_trace",
        "contract_path": path,
        "subject_kind": "rule",
        "subject_id": "rule",
        "schema_path": "common/schema.json",
        "schema_hash": SHA,
        "evaluator": {"component": "eval", "version": "1", "contract_hash": SHA},
        "test_ids": {"pass": ["pass"], "fail": ["fail"], "indeterminate": []},
        "rollout_gate": "contract_ci",
        **_ownership(),
    }


def _direct_contract_trace(path: str) -> dict[str, object]:
    trace = _contract_trace(path)
    return {
        key: trace[key]
        for key in ("contract_path", "schema_path", "evaluator", "test_ids", "rollout_gate")
    }


@pytest.fixture(autouse=True)
def _kernel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.syspath_prepend(str(KERNEL_SOURCE))


def test_recovery_outcome_is_a_closed_discriminated_union() -> None:
    from autocut_kernel.contracts.compiler.registry_entries import CommandEntry

    exhausted = {
        "entry_kind": "recover_scope_outcome",
        "strategy_id": "recover",
        "strategy_version": "1",
        "strategy_implementation_contract_hash": SHA,
        "outcome_branch": "exhausted_evidence",
        "commit_protocol": "recovery_outcome_protocol",
        "artifact_set_profile": "recovery_exhausted_evidence",
    } | _ownership()
    assert CommandEntry.from_mapping(exhausted).entry_kind == "recover_scope_outcome"
    with pytest.raises(Exception, match="exactly"):
        CommandEntry.from_mapping(exhausted | {"business_command_id": "cmd"})
    with pytest.raises(Exception, match="normalized non-empty string"):
        CommandEntry.from_mapping(exhausted | {"strategy_id": False})
    with pytest.raises(Exception, match="outcome_branch is not closed"):
        CommandEntry.from_mapping(exhausted | {"outcome_branch": "invented"})


def test_transition_and_authority_variants_reject_bad_local_types_before_closure() -> None:
    from autocut_kernel.contracts.compiler.registry_entries import CommandEntry

    transition = {
        "entry_kind": "command_state_transition", "command_id": "cmd", "command_version": "1",
        "transition_id": "transition:next", "from_state": "state:reserved", "to_state": "state:done",
        "state_machine_schema_path": "common/schema.json", "state_machine_schema_hash": SHA,
        "transition_schema_path": "common/schema.json", "transition_schema_hash": SHA, **_ownership(),
    }
    with pytest.raises(Exception, match="normalized non-empty string"):
        CommandEntry.from_mapping(transition | {"from_state": 7})
    authority = {
        "entry_kind": "authority_operation", "authority_kind": "dispatcher", "authority_id": "dispatch",
        "contract_path": "common/contract.json", "contract_hash": SHA,
        "allowed_artifact_types": ["artifact"], **_ownership(),
    }
    with pytest.raises(Exception, match="allowed_artifact_types must be an array"):
        CommandEntry.from_mapping(authority | {"allowed_artifact_types": "artifact"})


def test_command_and_rule_require_versioned_component_identities() -> None:
    from autocut_kernel.contracts.compiler.registry_entries import CommandEntry, RuleEntry

    command = {
        "entry_kind": "command_profile", "command_id": "cmd", "command_name": "StartRun",
        "command_version": "2.1.3", "profile_id": "profile", "profile_schema_path": "common/schema.json",
        "profile_schema_hash": SHA, "request_schema_path": "common/schema.json", "request_schema_hash": SHA,
        "parameter_schema_uri": "schema://command/test/parameters", "parameter_schema_path": "common/schema.json",
        "parameter_schema_hash": SHA, "result_schema_uri": "schema://command/test/result",
        "result_schema_path": "common/schema.json", "result_schema_hash": SHA,
        "handler_id": "handler", "handler_version": "1", "allowed_scope_kinds": [],
        "required_input_roles": [], "required_policy_roles": [], "lifecycle_slots": [],
        "required_capability": "run.start", "idempotency_algorithm_id": "idem",
        "idempotency_algorithm_version": "1", "idempotency_algorithm_contract_hash": SHA,
        "artifact_set_plan": {"kind": "fixed", "artifact_set_profile": "absent"},
        "commit_protocol": "run_bootstrap_cas", "side_effect_class": "store", **_ownership(),
    }
    assert CommandEntry.from_mapping(command).entry_kind == "command_profile"
    with pytest.raises(Exception, match="exactly"):
        CommandEntry.from_mapping({key: value for key, value in command.items() if key != "idempotency_algorithm_version"})
    with pytest.raises(Exception, match="closed v2.1.3 capability"):
        CommandEntry.from_mapping(command | {"required_capability": "execute"})
    rule = {
        "domain": "admission", "rule_id": "rule", "rule_class": "admission", "subject_artifact_types": ["artifact"],
        "evaluator_component": "eval", "evaluator_component_version": "1", "evaluator_contract_hash": SHA,
        "indeterminate_allowed": False, "on_fail": "stop", "on_indeterminate": "stop",
        "allowed_recovery_kinds": [], "exhaustion_action": "stop", "diagnostic_schema_path": "common/schema.json",
        "diagnostic_schema_hash": SHA, **_ownership(),
    }
    assert RuleEntry.from_mapping(rule).data["evaluator_component_version"] == "1"
    with pytest.raises(Exception, match="exactly"):
        RuleEntry.from_mapping({key: value for key, value in rule.items() if key != "evaluator_component_version"})


def test_rule_domain_is_a_closed_union_with_no_publication_recovery_fields() -> None:
    from autocut_kernel.contracts.compiler.registry_entries import RuleEntry

    publication = {
        "domain": "publication", "rule_id": "publish-rule", "rule_class": "publication",
        "subject_artifact_types": ["artifact"], "evaluator_component": "eval",
        "evaluator_component_version": "1", "evaluator_contract_hash": SHA,
        "indeterminate_allowed": True, "on_fail": "deny", "on_indeterminate": "deny",
        "diagnostic_schema_path": "common/schema.json", "diagnostic_schema_hash": SHA,
        **_ownership(),
    }
    assert RuleEntry.from_mapping(publication).data["domain"] == "publication"
    with pytest.raises(Exception, match="exactly"):
        RuleEntry.from_mapping(publication | {"allowed_recovery_kinds": []})
    with pytest.raises(Exception, match="deny-only"):
        RuleEntry.from_mapping(publication | {"on_indeterminate": "stop"})


def test_admission_rule_repair_needs_a_closed_nonempty_recovery_set() -> None:
    from autocut_kernel.contracts.compiler.registry_entries import RuleEntry

    rule = {
        "domain": "admission", "rule_id": "repair-rule", "rule_class": "admission",
        "subject_artifact_types": ["artifact"], "evaluator_component": "eval",
        "evaluator_component_version": "1", "evaluator_contract_hash": SHA,
        "indeterminate_allowed": True, "on_fail": "repair", "on_indeterminate": "stop",
        "allowed_recovery_kinds": ["format_repair"], "exhaustion_action": "quarantine",
        "diagnostic_schema_path": "common/schema.json", "diagnostic_schema_hash": SHA,
        **_ownership(),
    }
    assert RuleEntry.from_mapping(rule).data["on_fail"] == "repair"
    with pytest.raises(Exception, match="non-repairing"):
        RuleEntry.from_mapping(rule | {"on_fail": "stop"})
    with pytest.raises(Exception, match="closed recovery"):
        RuleEntry.from_mapping(rule | {"allowed_recovery_kinds": ["closed_without_recipe"]})


def test_fixture_artifact_refs_are_closed_sorted_and_hashed() -> None:
    from autocut_kernel.contracts.compiler.registry_entries import TraceEntry

    fixture = {
        "entry_kind": "test_fixture_inventory", "test_id": "test", "test_kind": "unit",
        "pack_id": "common-pack", "test_path": "common/test.json", "test_file_hash": SHA,
        "fixture_refs": [{"fixture_id": "fixture", "pack_id": "common-pack", "path": "common/test.json",
            "file_hash": SHA, "artifact_refs": [{"artifact_id": "a", "content_hash": SHA}]}], **_ownership(),
    }
    assert TraceEntry.from_mapping(fixture).entry_kind == "test_fixture_inventory"
    bad = fixture | {"fixture_refs": [{**fixture["fixture_refs"][0], "artifact_refs": [{"artifact_id": "a", "content_hash": SHA}, {"artifact_id": "a", "content_hash": SHA}]}]}
    with pytest.raises(Exception, match="sorted and unique"):
        TraceEntry.from_mapping(bad)


@pytest.mark.parametrize(
    "locator",
    [
        "common//schema.json",
        "common/./schema.json",
        "common/../schema.json",
        "common/schema.json/",
        "/common/schema.json",
        "common\\schema.json",
        "common/例.json",
        "common/\u202eschema.json",
        "common/space name.json",
        "common/schema%20name.json",
        "common/schema#name.json",
        "common/schema?name.json",
        "common/schema:name.json",
        "common/schema@name.json",
        "common/\x01schema.json",
        "common/\x7fschema.json",
    ],
)
def test_physical_registry_locator_rejects_raw_aliases_and_non_ascii(locator: str) -> None:
    from autocut_kernel.contracts.compiler.registry_entries import path

    with pytest.raises(Exception, match="canonical ASCII|unsafe path"):
        path({"locator": locator}, "locator")


def test_every_registry_entry_family_uses_physical_machine_locator_grammar() -> None:
    """Artifact, Command, Rule, Strategy and Trace paths share one grammar."""
    from autocut_kernel.contracts.compiler.registry_entries import (
        ArtifactEntry,
        CommandEntry,
        RuleEntry,
        StrategyEntry,
        TraceEntry,
    )

    artifact = {
        "artifact_type": "artifact",
        "payload_schema_path": "common/例.json",
        "payload_schema_hash": SHA,
        "envelope_schema_path": "common/schema.json",
        "envelope_schema_hash": SHA,
        "allowed_scope_kinds": ["job"],
        "authority_writers": [
            {"kind": "dispatcher", "authority_id": "source-dispatcher"}
        ],
        "permitted_producer_components": [
            {"component_id": "producer", "component_version": "1"}
        ],
        "policy_requirements": {"kind": "none"},
        **_ownership(),
    }
    authority = {
        "entry_kind": "authority_operation",
        "authority_kind": "dispatcher",
        "authority_id": "source-dispatcher",
        "contract_path": "common/例.json",
        "contract_hash": SHA,
        "allowed_artifact_types": ["artifact"],
        **_ownership(),
    }
    rule = {
        "domain": "admission",
        "rule_id": "rule",
        "rule_class": "admission",
        "subject_artifact_types": ["artifact"],
        "evaluator_component": "eval",
        "evaluator_component_version": "1",
        "evaluator_contract_hash": SHA,
        "indeterminate_allowed": False,
        "on_fail": "stop",
        "on_indeterminate": "stop",
        "allowed_recovery_kinds": [],
        "exhaustion_action": "stop",
        "diagnostic_schema_path": "common/例.json",
        "diagnostic_schema_hash": SHA,
        **_ownership(),
    }
    strategy = {
        "component_id": "eval",
        "component_version": "1",
        "kind": "evaluator",
        "implementation_contract_path": "common/例.json",
        "implementation_contract_hash": SHA,
        "input_schema_path": "common/schema.json",
        "input_schema_hash": SHA,
        "output_schema_path": "common/schema.json",
        "output_schema_hash": SHA,
        "determinism": "deterministic",
        "capabilities": [],
        **_ownership(),
    }
    trace = _contract_trace("原理/阶段-04#SA-DIALOGUE-001")
    trace["schema_path"] = "common/例.json"

    for parser, value in (
        (ArtifactEntry.from_mapping, artifact),
        (CommandEntry.from_mapping, authority),
        (RuleEntry.from_mapping, rule),
        (StrategyEntry.from_mapping, strategy),
        (TraceEntry.from_mapping, trace),
    ):
        with pytest.raises(Exception, match="canonical ASCII"):
            parser(value)


def test_fixture_and_conditional_schema_paths_use_machine_locator_grammar() -> None:
    from autocut_kernel.contracts.compiler.registry_entries import CommandEntry, TraceEntry

    fixture = {
        "entry_kind": "test_fixture_inventory",
        "test_id": "test",
        "test_kind": "unit",
        "pack_id": "common-pack",
        "test_path": "common/test.json",
        "test_file_hash": SHA,
        "fixture_refs": [
            {
                "fixture_id": "fixture",
                "pack_id": "common-pack",
                "path": "common/例.json",
                "file_hash": SHA,
                "artifact_refs": [],
            }
        ],
        **_ownership(),
    }
    with pytest.raises(Exception, match="canonical ASCII"):
        TraceEntry.from_mapping(fixture)

    profile = {
        "entry_kind": "artifact_set_profile",
        "artifact_set_profile": "stage_admission",
        "decision_member_role": "decision",
        "decision_artifact_type": "artifact",
        "required_member_roles": [],
        "conditional_member_roles": [
            {
                "role": "conditional",
                "artifact_types": ["artifact"],
                "scope_kinds": ["job"],
                "min_members": 0,
                "max_members": 1,
                "condition_schema_path": "common//condition.json",
                "condition_schema_hash": SHA,
            }
        ],
        "forbidden_member_roles": [],
        "affected_chain_heads": [],
        "forbidden_reference_directions": [],
        **_ownership(),
    }
    with pytest.raises(Exception, match="unsafe path"):
        CommandEntry.from_mapping(profile)


@pytest.mark.parametrize(
    "locator",
    [
        "#fragment",
        "stage-04#",
        "stage-04#first#second",
        "stage-04//nested#R",
        "stage-04/#R",
        "stage-04/../nested#R",
        "stage-04\\nested#R",
        "stage-04#not/a-fragment",
        "stage-04#not a fragment",
    ],
)
def test_trace_consumers_share_closed_contract_obligation_locator_grammar(locator: str) -> None:
    """Both direct trace paths must reject raw, pre-normalization bypasses."""
    from autocut_kernel.contracts.compiler.registry import ContractTrace
    from autocut_kernel.contracts.compiler.registry_entries import TraceEntry

    with pytest.raises(Exception):
        ContractTrace.from_mapping(_direct_contract_trace(locator))
    with pytest.raises(Exception):
        TraceEntry.from_mapping(_contract_trace(locator))


@pytest.mark.parametrize("locator", ["stage-04#SA-DIALOGUE-001", "contract-0"])
def test_trace_consumers_accept_the_same_contract_obligation_locators(locator: str) -> None:
    from autocut_kernel.contracts.compiler.registry import ContractTrace
    from autocut_kernel.contracts.compiler.registry_entries import TraceEntry

    assert ContractTrace.from_mapping(_direct_contract_trace(locator)).contract_path == locator
    assert TraceEntry.from_mapping(_contract_trace(locator)).data["contract_path"] == locator


def test_artifact_writer_and_producer_order_are_part_of_the_grammar() -> None:
    from autocut_kernel.contracts.compiler.registry_entries import ArtifactEntry

    artifact = {
        "artifact_type": "artifact", "payload_schema_path": "common/schema.json", "payload_schema_hash": SHA,
        "envelope_schema_path": "common/schema.json", "envelope_schema_hash": SHA,
        "allowed_scope_kinds": ["job"],
        "authority_writers": [{"kind": "command", "command_id": "a", "command_version": "1"}],
        "permitted_producer_components": [{"component_id": "producer", "component_version": "1"}],
        "policy_requirements": {"kind": "none"}, **_ownership(),
    }
    assert ArtifactEntry.from_mapping(artifact).artifact_type == "artifact"
    duplicated = artifact | {"authority_writers": artifact["authority_writers"] * 2}
    with pytest.raises(Exception, match="sorted and unique"):
        ArtifactEntry.from_mapping(duplicated)


def test_scope_and_strategy_capability_enums_are_closed_at_entry_boundary() -> None:
    from autocut_kernel.contracts.compiler.registry_entries import ArtifactEntry, StrategyEntry

    artifact = {
        "artifact_type": "artifact", "payload_schema_path": "common/schema.json", "payload_schema_hash": SHA,
        "envelope_schema_path": "common/schema.json", "envelope_schema_hash": SHA,
        "allowed_scope_kinds": ["job"],
        "authority_writers": [{"kind": "command", "command_id": "a", "command_version": "1"}],
        "permitted_producer_components": [{"component_id": "producer", "component_version": "1"}],
        "policy_requirements": {"kind": "none"}, **_ownership(),
    }
    with pytest.raises(Exception, match="closed v2.1.3 scope"):
        ArtifactEntry.from_mapping(artifact | {"allowed_scope_kinds": ["invented_scope"]})
    strategy = {
        "component_id": "eval", "component_version": "1", "kind": "evaluator",
        "implementation_contract_path": "common/contract.json", "implementation_contract_hash": SHA,
        "input_schema_path": "common/schema.json", "input_schema_hash": SHA,
        "output_schema_path": "common/schema.json", "output_schema_hash": SHA,
        "determinism": "deterministic", "capabilities": ["runtime.execute_stage"], **_ownership(),
    }
    assert StrategyEntry.from_mapping(strategy).data["capabilities"] == ["runtime.execute_stage"]
    with pytest.raises(Exception, match="closed v2.1.3 capability"):
        StrategyEntry.from_mapping(strategy | {"capabilities": ["runtime.execute_anything"]})


def test_artifact_owner_pack_is_closed_before_cross_registry_resolution() -> None:
    from autocut_kernel.contracts.compiler.registry_entries import ArtifactEntry

    artifact = {
        "artifact_type": "artifact", "payload_schema_path": "common/schema.json", "payload_schema_hash": SHA,
        "envelope_schema_path": "common/schema.json", "envelope_schema_hash": SHA,
        "allowed_scope_kinds": ["job"],
        "authority_writers": [{"kind": "command", "command_id": "a", "command_version": "1"}],
        "permitted_producer_components": [{"component_id": "producer", "component_version": "1"}],
        "policy_requirements": {"kind": "none"}, **_ownership(),
    }
    with pytest.raises(Exception, match="artifact owner_pack"):
        ArtifactEntry.from_mapping(artifact | {"owner_pack": "commands"})
