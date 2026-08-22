# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownParameterType=false, reportAttributeAccessIssue=false, reportArgumentType=false
"""Closed, typed entries for the five v2.1.3 registry documents.

This module deliberately validates syntax only.  Cross-document references are
checked by :mod:`registry_closure` from one immutable RegistrySet snapshot.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

from .canonical import canonical_json_bytes
from .errors import RegistryValidationError

SHA_PREFIX = "sha256:"
_MACHINE_SOURCE_LOCATOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*\Z")
# These values are copied verbatim from the frozen v2.1.3 production-system
# contract: §4.2 defines the eight scope variants and §3.5 defines the
# requested-capability enum.  Keep the compiler's single authoritative binding
# here; source fixtures must not establish a second, inferred enum.
CLOSED_SCOPE_KINDS = frozenset(
    {
        "root_input",
        "job",
        "portfolio",
        "story",
        "publication_batch",
        "publication_lineage",
        "run_lineage",
        "job_execution",
    }
)
CLOSED_CAPABILITIES = frozenset(
    {
        "media.prepare",
        "run.start",
        "runtime.execute_stage",
        "runtime.advance_usage",
        "runtime.execute_recovery",
        "release.evaluate_story_terminal",
        "release.evaluate_portfolio",
        "release.plan_batch",
        "release.prepare_batch",
        "release.commit_batch",
        "release.abort_batch",
        "release.reconcile_batch",
        "release.publish_independent",
        "run.finalize",
    }
)
_OWNERSHIP_FIELDS = {
    "owner_pack",
    "owner_source_path",
    "owner_source_hash",
    "owner_contract_path",
    "owner_contract_hash",
}
_ARTIFACT_OWNER_PACKS = frozenset(
    {"common", "publication", "stage_01", "stage_02", "stage_03", "stage_04", "stage_05"}
)


def exact(value: object, fields: set[str], label: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != fields:  # noqa: E721
        raise RegistryValidationError(f"{label} must have exactly {sorted(fields)}")
    return value


def text(mapping: Mapping[str, object], key: str) -> str:
    value = mapping[key]
    if type(value) is not str or not value or value != value.strip() or "\n" in value:  # noqa: E721
        raise RegistryValidationError(f"{key} must be a normalized non-empty string")
    return value


def digest(mapping: Mapping[str, object], key: str) -> str:
    value = text(mapping, key)
    if (
        len(value) != 71
        or not value.startswith(SHA_PREFIX)
        or any(c not in "0123456789abcdef" for c in value[7:])
    ):
        raise RegistryValidationError(f"{key} must be a lowercase sha256 digest")
    return value


def strings(mapping: Mapping[str, object], key: str, *, nonempty: bool = False) -> tuple[str, ...]:
    value = mapping[key]
    if type(value) is not list:  # noqa: E721
        raise RegistryValidationError(f"{key} must be an array")
    result = tuple(text({key: item}, key) for item in value)
    if (
        (nonempty and not result)
        or tuple(sorted(result)) != result
        or len(set(result)) != len(result)
    ):
        raise RegistryValidationError(
            f"{key} must be non-empty, sorted, and unique"
            if nonempty
            else f"{key} must be sorted and unique"
        )
    return result


def array(mapping: Mapping[str, object], key: str) -> list[object]:
    value = mapping[key]
    if type(value) is not list:  # noqa: E721
        raise RegistryValidationError(f"{key} must be an array")
    return value


def machine_source_locator(value: object, *, label: str) -> str:
    """Validate an exact physical Registry source locator.

    The source spelling is signed data, so this validator deliberately works
    on the unmodified string.  In particular, no ``Path`` constructor may
    collapse repeated slashes or dot segments before the grammar is checked.
    Trace obligation locators use :func:`contract_obligation_locator` instead.
    """
    if type(value) is not str or not value:  # noqa: E721
        raise RegistryValidationError(
            f"{label} must be a non-empty safe relative path and canonical ASCII POSIX locator"
        )
    if not _MACHINE_SOURCE_LOCATOR.fullmatch(value):
        raise RegistryValidationError(
            f"{label} must be a safe relative path and canonical ASCII POSIX locator"
        )
    if value.endswith("/") or any(part in {"", ".", ".."} for part in value.split("/")):
        raise RegistryValidationError(f"{label} contains an unsafe path segment")
    return value


def path(mapping: Mapping[str, object], key: str) -> str:
    """Validate a physical source/schema/contract/test/fixture locator."""
    return machine_source_locator(mapping[key], label=key)


def contract_obligation_locator(mapping: Mapping[str, object], key: str) -> str:
    """Validate the Trace-only logical contract-obligation locator.

    This is intentionally distinct from :func:`path`: a trace identifies an
    obligation inside a contract and may therefore carry one ``#`` fragment.
    It never names an inventory file.  Validate raw slash-separated segments
    before ``PurePosixPath`` can normalize repeated slashes or dot segments.
    """
    value = text(mapping, key)
    if value.count("#") > 1:
        raise RegistryValidationError(f"{key} must contain at most one fragment delimiter")
    path_part, separator, fragment = value.partition("#")
    if (
        not path_part
        or "\\" in value
        or any(part in {"", ".", ".."} for part in path_part.split("/"))
    ):
        raise RegistryValidationError(f"{key} must be a safe relative contract path")
    if separator and (
        not fragment
        or "/" in fragment
        or "\\" in fragment
        or "#" in fragment
        or fragment in {".", ".."}
        or any(character.isspace() for character in fragment)
    ):
        raise RegistryValidationError(f"{key} fragment must be a non-empty safe identifier")
    return value


def boolean(mapping: Mapping[str, object], key: str) -> bool:
    value = mapping[key]
    if type(value) is not bool:  # noqa: E721
        raise RegistryValidationError(f"{key} must be a boolean")
    return value


@dataclass(frozen=True, slots=True)
class ArtifactEntry:
    artifact_type: str
    owner_pack: str
    owner_source_path: str
    owner_source_hash: str
    owner_contract_path: str
    owner_contract_hash: str
    payload_schema_path: str
    payload_schema_hash: str
    envelope_schema_path: str
    envelope_schema_hash: str
    allowed_scope_kinds: tuple[str, ...]
    authority_writers: tuple[dict[str, object], ...]
    permitted_producer_components: tuple[dict[str, object], ...]
    policy_requirements: dict[str, object]

    @classmethod
    def from_mapping(cls, value: object) -> "ArtifactEntry":
        m = exact(
            value,
            {
                "artifact_type",
                "owner_pack",
                "owner_source_path",
                "owner_source_hash",
                "owner_contract_path",
                "owner_contract_hash",
                "payload_schema_path",
                "payload_schema_hash",
                "envelope_schema_path",
                "envelope_schema_hash",
                "allowed_scope_kinds",
                "authority_writers",
                "permitted_producer_components",
                "policy_requirements",
            },
            "artifact",
        )
        writers = tuple(_writer(v) for v in array(m, "authority_writers"))
        producers = tuple(_producer(v) for v in array(m, "permitted_producer_components"))
        policy = _policy(m["policy_requirements"])
        if not writers or not producers:
            raise RegistryValidationError("artifact writers and producers must be non-empty")
        writer_keys = tuple(
            canonical_json_bytes(writer)
            for writer in writers
        )
        producer_keys = tuple(
            (producer["component_id"], producer["component_version"])
            for producer in producers
        )
        if writer_keys != tuple(sorted(writer_keys)) or len(set(writer_keys)) != len(writer_keys):
            raise RegistryValidationError("authority_writers must be JCS-byte sorted and unique")
        if producer_keys != tuple(sorted(producer_keys)) or len(set(producer_keys)) != len(producer_keys):
            raise RegistryValidationError("permitted_producer_components must be sorted and unique")
        return cls(
            text(m, "artifact_type"),
            _artifact_owner_pack(m),
            path(m, "owner_source_path"),
            digest(m, "owner_source_hash"),
            path(m, "owner_contract_path"),
            digest(m, "owner_contract_hash"),
            path(m, "payload_schema_path"),
            digest(m, "payload_schema_hash"),
            path(m, "envelope_schema_path"),
            digest(m, "envelope_schema_hash"),
            _closed_scope_kinds(m, "allowed_scope_kinds", nonempty=True),
            writers,
            producers,
            policy,
        )


def _writer(value: object) -> dict[str, object]:
    if type(value) is not dict:  # noqa: E721
        raise RegistryValidationError("authority writer must be an object")
    kind = value.get("kind")
    if kind == "command":
        result = exact(value, {"kind", "command_id", "command_version"}, "command authority writer")
        text(result, "command_id")
        text(result, "command_version")
        return result
    if kind in {"dispatcher", "bootstrap", "external_authority"}:
        result = exact(value, {"kind", "authority_id"}, "authority writer")
        text(result, "authority_id")
        return result
    raise RegistryValidationError("authority writer kind is not closed")


def _producer(value: object) -> dict[str, object]:
    result = exact(value, {"component_id", "component_version"}, "artifact producer")
    text(result, "component_id")
    text(result, "component_version")
    return result


def _policy(value: object) -> dict[str, object]:
    if type(value) is not dict:  # noqa: E721
        raise RegistryValidationError("policy_requirements must be an object")
    if value.get("kind") == "none":
        return exact(value, {"kind"}, "policy requirements")
    if value.get("kind") == "required_types":
        result = exact(value, {"kind", "policy_types"}, "policy requirements")
        strings(result, "policy_types", nonempty=True)
        return result
    raise RegistryValidationError("policy requirement kind is not closed")


def _ownership(mapping: dict[str, object]) -> None:
    """Validate the common entry proof before cross-pack provenance checks.

    Direct inventory membership and recomputation are deliberately deferred to
    ``registry_closure`` because only that compiler phase owns the immutable
    manifest snapshot.
    """
    text(mapping, "owner_pack")
    path(mapping, "owner_source_path")
    digest(mapping, "owner_source_hash")
    path(mapping, "owner_contract_path")
    digest(mapping, "owner_contract_hash")


def _artifact_owner_pack(mapping: Mapping[str, object]) -> str:
    owner_pack = text(mapping, "owner_pack")
    if owner_pack not in _ARTIFACT_OWNER_PACKS:
        raise RegistryValidationError("artifact owner_pack must be a common, publication, or stage_* pack")
    return owner_pack


def _closed_scope_kinds(
    mapping: Mapping[str, object], key: str, *, nonempty: bool = False
) -> tuple[str, ...]:
    values = strings(mapping, key, nonempty=nonempty)
    if any(value not in CLOSED_SCOPE_KINDS for value in values):
        raise RegistryValidationError(f"{key} must use the closed v2.1.3 scope enum")
    return values


def _state_id(mapping: Mapping[str, object], key: str) -> str:
    value = text(mapping, key)
    if not value.startswith("state:"):
        raise RegistryValidationError(f"{key} must use the state: ID prefix")
    return value


def _transition_id(mapping: Mapping[str, object], key: str) -> str:
    value = text(mapping, key)
    if not value.startswith("transition:"):
        raise RegistryValidationError(f"{key} must use the transition: ID prefix")
    return value


@dataclass(frozen=True, slots=True)
class CommandEntry:
    entry_kind: str
    data: dict[str, object]

    @classmethod
    def from_mapping(cls, value: object) -> "CommandEntry":
        if type(value) is not dict or type(value.get("entry_kind")) is not str:  # noqa: E721
            raise RegistryValidationError("command entry must declare entry_kind")
        kind = value["entry_kind"]
        fields = _COMMAND_FIELDS.get(kind)
        if fields is None:
            raise RegistryValidationError("command entry_kind is not closed")
        if kind == "recover_scope_outcome":
            branch = value.get("outcome_branch")
            if type(branch) is not str or branch not in {"business_effect", "exhausted_evidence"}:  # noqa: E721
                raise RegistryValidationError("recovery outcome_branch is not closed")
        if kind == "recover_scope_outcome" and value.get("outcome_branch") == "exhausted_evidence":
            fields = {
                "entry_kind",
                "strategy_id",
                "strategy_version",
                "strategy_implementation_contract_hash",
                "outcome_branch",
                "commit_protocol",
                "artifact_set_profile",
            } | _OWNERSHIP_FIELDS
        m = exact(value, fields, f"command {kind}")
        _ownership(m)
        if kind == "command_profile":
            _plan(m["artifact_set_plan"])
            if text(m, "commit_protocol") not in COMMIT_PROTOCOLS:
                raise RegistryValidationError("commit_protocol is not closed")
            for field in (
                "profile_schema_path",
                "request_schema_path",
                "parameter_schema_path",
                "result_schema_path",
            ):
                path(m, field)
            for field in (
                "profile_schema_hash",
                "request_schema_hash",
                "parameter_schema_hash",
                "result_schema_hash",
            ):
                digest(m, field)
            for field in (
                "command_id",
                "command_name",
                "command_version",
                "profile_id",
                "handler_id",
                "handler_version",
                "required_capability",
                "idempotency_algorithm_id",
                "idempotency_algorithm_version",
                "side_effect_class",
            ):
                text(m, field)
            text(m, "parameter_schema_uri")
            text(m, "result_schema_uri")
            if m["required_capability"] not in CLOSED_CAPABILITIES:
                raise RegistryValidationError(
                    "required_capability must use the closed v2.1.3 capability enum"
                )
            digest(m, "idempotency_algorithm_contract_hash")
            for field in (
                "allowed_scope_kinds",
                "required_input_roles",
                "required_policy_roles",
                "lifecycle_slots",
            ):
                (
                    _closed_scope_kinds(m, field)
                    if field == "allowed_scope_kinds"
                    else strings(m, field)
                )
            if (
                m["command_name"] != "RecoverScope"
                and m["artifact_set_plan"].get("kind") == "recover_scope_outcome"
            ):  # type: ignore[union-attr]
                raise RegistryValidationError(
                    "recover_scope_outcome plan is exclusive to RecoverScope"
                )
        elif kind == "artifact_set_profile":
            _artifact_set_profile(m)
        elif kind == "recover_scope_outcome":
            _recovery(m)
        elif kind in {"command_state_transition", "artifact_state_transition"}:
            _transition_id(m, "transition_id")
            _state_id(m, "from_state")
            _state_id(m, "to_state")
            if kind == "command_state_transition":
                text(m, "command_id")
                text(m, "command_version")
            else:
                text(m, "artifact_type")
            for field in ("state_machine_schema_path", "transition_schema_path"):
                path(m, field)
            for field in ("state_machine_schema_hash", "transition_schema_hash"):
                digest(m, field)
        elif kind == "authority_operation":
            if m["authority_kind"] not in {"dispatcher", "bootstrap", "external_authority"}:
                raise RegistryValidationError("authority_kind is not closed")
            text(m, "authority_kind")
            text(m, "authority_id")
            path(m, "contract_path")
            digest(m, "contract_hash")
            strings(m, "allowed_artifact_types", nonempty=True)
        return cls(kind, m)


COMMIT_PROTOCOLS = frozenset(
    {
        "artifact_set_commit",
        "artifact_set_with_lineage_cas",
        "artifact_set_with_publication_lineage_cas",
        "run_bootstrap_cas",
        "lineage_head_cas",
        "external_publication_lifecycle",
        "recovery_outcome_protocol",
    }
)
_COMMAND_FIELDS: dict[str, set[str]] = {
    "command_profile": {
        "entry_kind",
        "command_id",
        "command_name",
        "command_version",
        "profile_id",
        "profile_schema_path",
        "profile_schema_hash",
        "request_schema_path",
        "request_schema_hash",
        "parameter_schema_uri",
        "parameter_schema_path",
        "parameter_schema_hash",
        "result_schema_uri",
        "result_schema_path",
        "result_schema_hash",
        "handler_id",
        "handler_version",
        "allowed_scope_kinds",
        "required_input_roles",
        "required_policy_roles",
        "lifecycle_slots",
        "required_capability",
        "idempotency_algorithm_id",
        "idempotency_algorithm_version",
        "idempotency_algorithm_contract_hash",
        "artifact_set_plan",
        "commit_protocol",
        "side_effect_class",
    } | _OWNERSHIP_FIELDS,
    "artifact_set_profile": {
        "entry_kind",
        "artifact_set_profile",
        "decision_member_role",
        "decision_artifact_type",
        "required_member_roles",
        "conditional_member_roles",
        "forbidden_member_roles",
        "affected_chain_heads",
        "forbidden_reference_directions",
    } | _OWNERSHIP_FIELDS,
    "recover_scope_outcome": {
        "entry_kind",
        "strategy_id",
        "strategy_version",
        "strategy_implementation_contract_hash",
        "outcome_branch",
        "commit_protocol",
        "business_command_id",
        "business_command_version",
        "business_artifact_set_profile",
        "business_commit_protocol",
    } | _OWNERSHIP_FIELDS,
    "command_state_transition": {
        "entry_kind",
        "command_id",
        "command_version",
        "transition_id",
        "from_state",
        "to_state",
        "state_machine_schema_path",
        "state_machine_schema_hash",
        "transition_schema_path",
        "transition_schema_hash",
    } | _OWNERSHIP_FIELDS,
    "artifact_state_transition": {
        "entry_kind",
        "artifact_type",
        "transition_id",
        "from_state",
        "to_state",
        "state_machine_schema_path",
        "state_machine_schema_hash",
        "transition_schema_path",
        "transition_schema_hash",
    } | _OWNERSHIP_FIELDS,
    "authority_operation": {
        "entry_kind",
        "authority_kind",
        "authority_id",
        "contract_path",
        "contract_hash",
        "allowed_artifact_types",
    } | _OWNERSHIP_FIELDS,
}


def _plan(value: object) -> dict[str, object]:
    if type(value) is not dict:  # noqa: E721
        raise RegistryValidationError("artifact_set_plan must be an object")
    if value.get("kind") == "recover_scope_outcome":
        return exact(value, {"kind"}, "artifact_set_plan")
    result = exact(value, {"kind", "artifact_set_profile"}, "artifact_set_plan")
    if result["kind"] != "fixed" or result["artifact_set_profile"] not in ARTIFACT_SET_PROFILES:
        raise RegistryValidationError("artifact_set_plan is not closed")
    return result


ARTIFACT_SET_PROFILES = frozenset(
    {
        "root_input_commit",
        "stage_admission",
        "story_release",
        "portfolio_release",
        "publication_transition",
        "run_outcome",
        "recovery_exhausted_evidence",
        "migration_assessment",
        "absent",
    }
)


def _recovery(m: dict[str, object]) -> None:
    for field in ("strategy_id", "strategy_version", "outcome_branch", "commit_protocol"):
        text(m, field)
    digest(m, "strategy_implementation_contract_hash")
    branch = m["outcome_branch"]
    if m["commit_protocol"] != "recovery_outcome_protocol":
        raise RegistryValidationError("recovery outcomes require recovery_outcome_protocol")
    if branch == "business_effect":
        for field in (
            "business_command_id",
            "business_command_version",
            "business_artifact_set_profile",
            "business_commit_protocol",
        ):
            text(m, field)
        if m["business_artifact_set_profile"] not in ARTIFACT_SET_PROFILES:
            raise RegistryValidationError("recovery business artifact_set_profile is not closed")
        if m["business_commit_protocol"] not in COMMIT_PROTOCOLS:
            raise RegistryValidationError("recovery business commit_protocol is not closed")
        return
    if branch == "exhausted_evidence":
        # The exhausted alternative has a different exact grammar.
        exact(
            m,
            {
                "entry_kind",
                "strategy_id",
                "strategy_version",
                "strategy_implementation_contract_hash",
                "outcome_branch",
                "commit_protocol",
                "artifact_set_profile",
            } | _OWNERSHIP_FIELDS,
            "exhausted recovery outcome",
        )
        if text(m, "artifact_set_profile") != "recovery_exhausted_evidence":
            raise RegistryValidationError(
                "exhausted recovery outcome requires recovery_exhausted_evidence"
            )
        return
    raise RegistryValidationError("recovery outcome_branch is not closed")


def _artifact_set_profile(m: dict[str, object]) -> None:
    """Validate every local discriminated-union member before closure lookup."""
    text(m, "artifact_set_profile")
    text(m, "decision_member_role")
    text(m, "decision_artifact_type")
    for group, conditional in (
        ("required_member_roles", False),
        ("conditional_member_roles", True),
    ):
        roles = array(m, group)
        for value in roles:
            fields = {"role", "artifact_types", "scope_kinds", "min_members", "max_members"}
            if conditional:
                fields |= {"condition_schema_path", "condition_schema_hash"}
            role = exact(value, fields, "artifact-set member role")
            text(role, "role")
            strings(role, "artifact_types", nonempty=True)
            _closed_scope_kinds(role, "scope_kinds", nonempty=True)
            minimum, maximum = role["min_members"], role["max_members"]
            if type(minimum) is not int or type(maximum) is not int or minimum < 0 or maximum < minimum:
                raise RegistryValidationError("artifact-set member counts must be non-negative ordered integers")
            if conditional:
                path(role, "condition_schema_path")
                digest(role, "condition_schema_hash")
    strings(m, "forbidden_member_roles")
    heads = array(m, "affected_chain_heads")
    for value in heads:
        head = exact(value, {"scope_kind", "artifact_type"}, "artifact-set affected chain head")
        text(head, "artifact_type")
        if text(head, "scope_kind") not in CLOSED_SCOPE_KINDS:
            raise RegistryValidationError("artifact-set affected chain head scope_kind is not closed")
    directions = array(m, "forbidden_reference_directions")
    for value in directions:
        direction = exact(value, {"from_role", "to_role"}, "artifact-set reference direction")
        text(direction, "from_role")
        text(direction, "to_role")


@dataclass(frozen=True, slots=True)
class RuleEntry:
    data: dict[str, object]

    @classmethod
    def from_mapping(cls, value: object) -> "RuleEntry":
        if type(value) is not dict or value.get("domain") not in {"admission", "publication"}:  # noqa: E721
            raise RegistryValidationError("rule domain is not closed")
        domain = value["domain"]
        fields = _ADMISSION_RULE_FIELDS if domain == "admission" else _PUBLICATION_RULE_FIELDS
        m = exact(
            value,
            fields | _OWNERSHIP_FIELDS,
            "rule",
        )
        _ownership(m)
        strings(m, "subject_artifact_types", nonempty=True)
        text(m, "rule_id")
        text(m, "rule_class")
        text(m, "evaluator_component")
        text(m, "evaluator_component_version")
        digest(m, "evaluator_contract_hash")
        path(m, "diagnostic_schema_path")
        digest(m, "diagnostic_schema_hash")
        boolean(m, "indeterminate_allowed")
        if domain == "admission":
            recovery_kinds = strings(m, "allowed_recovery_kinds")
            on_fail, on_indeterminate = m["on_fail"], m["on_indeterminate"]
            if on_fail not in {"repair", "quarantine", "stop"} or on_indeterminate not in {
                "repair", "quarantine", "stop"
            }:
                raise RegistryValidationError("admission rule actions are not closed")
            if m["exhaustion_action"] not in {"quarantine", "stop"}:
                raise RegistryValidationError("admission rule exhaustion_action is not closed")
            if not m["indeterminate_allowed"] and on_indeterminate != "stop":
                raise RegistryValidationError("rules that disallow indeterminate must stop")
            repair = on_fail == "repair" or on_indeterminate == "repair"
            if repair:
                if not recovery_kinds or any(kind not in _RECOVERY_KINDS for kind in recovery_kinds):
                    raise RegistryValidationError("repairing admission rules require closed recovery kinds")
            elif recovery_kinds:
                raise RegistryValidationError("non-repairing admission rules require empty recovery kinds")
        else:
            if m["indeterminate_allowed"] is not True or m["on_fail"] != "deny" or m["on_indeterminate"] != "deny":
                raise RegistryValidationError("publication rules are deny-only and allow indeterminate")
        return cls(m)


_ADMISSION_RULE_FIELDS = {
    "domain", "rule_id", "rule_class", "subject_artifact_types", "evaluator_component",
    "evaluator_component_version", "evaluator_contract_hash", "indeterminate_allowed",
    "on_fail", "on_indeterminate", "allowed_recovery_kinds", "exhaustion_action",
    "diagnostic_schema_path", "diagnostic_schema_hash",
}
_PUBLICATION_RULE_FIELDS = {
    "domain", "rule_id", "rule_class", "subject_artifact_types", "evaluator_component",
    "evaluator_component_version", "evaluator_contract_hash", "indeterminate_allowed",
    "on_fail", "on_indeterminate", "diagnostic_schema_path", "diagnostic_schema_hash",
}
_RECOVERY_KINDS = frozenset(
    {"format_repair", "deterministic_recompile", "semantic_regeneration", "portfolio_replan"}
)


@dataclass(frozen=True, slots=True)
class StrategyEntry:
    data: dict[str, object]

    @classmethod
    def from_mapping(cls, value: object) -> "StrategyEntry":
        m = exact(
            value,
            {
                "component_id",
                "component_version",
                "kind",
                "implementation_contract_path",
                "implementation_contract_hash",
                "input_schema_path",
                "input_schema_hash",
                "output_schema_path",
                "output_schema_hash",
                "determinism",
                "owner_pack",
                "capabilities",
            } | _OWNERSHIP_FIELDS,
            "strategy",
        )
        _ownership(m)
        if m["kind"] not in {
            "evaluator",
            "command_handler",
            "generator",
            "recovery",
            "compiler",
            "verifier",
            "idempotency_algorithm",
            "media_detector",
            "renderer",
            "model_adapter",
            "platform_adapter",
            "normalizer",
            "ingestion_adapter",
        }:
            raise RegistryValidationError("strategy kind is not closed")
        for field in ("component_id", "component_version", "owner_pack"):
            text(m, field)
        for field in ("implementation_contract_path", "input_schema_path", "output_schema_path"):
            path(m, field)
        for field in ("implementation_contract_hash", "input_schema_hash", "output_schema_hash"):
            digest(m, field)
        if m["determinism"] not in {"deterministic", "external_effect", "model_port"}:
            raise RegistryValidationError("strategy determinism is not closed")
        capabilities = strings(m, "capabilities")
        if any(capability not in CLOSED_CAPABILITIES for capability in capabilities):
            raise RegistryValidationError(
                "strategy capabilities must use the closed v2.1.3 capability enum"
            )
        return cls(m)


@dataclass(frozen=True, slots=True)
class TraceEntry:
    entry_kind: str
    data: dict[str, object]

    @classmethod
    def from_mapping(cls, value: object) -> "TraceEntry":
        if type(value) is not dict:  # noqa: E721
            raise RegistryValidationError("trace entry must be an object")
        kind = value.get("entry_kind")
        if type(kind) is not str:  # noqa: E721
            raise RegistryValidationError("trace entry_kind is not closed")
        fields = _TRACE_FIELDS.get(kind)
        if fields is None:
            raise RegistryValidationError("trace entry_kind is not closed")
        m = exact(value, fields | _OWNERSHIP_FIELDS, f"trace {kind}")
        _ownership(m)
        if kind == "contract_trace":
            if m["subject_kind"] not in {"rule", "command", "artifact", "state_transition"}:
                raise RegistryValidationError("trace subject_kind is not closed")
            path(m, "schema_path")
            digest(m, "schema_hash")
            contract_obligation_locator(m, "contract_path")
            text(m, "subject_id")
            evaluator = exact(
                m["evaluator"], {"component", "version", "contract_hash"}, "trace evaluator"
            )
            text(evaluator, "component")
            text(evaluator, "version")
            digest(evaluator, "contract_hash")
            tests = exact(m["test_ids"], {"pass", "fail", "indeterminate"}, "trace test_ids")
            strings(tests, "pass", nonempty=True)
            strings(tests, "fail", nonempty=True)
            strings(tests, "indeterminate")
            if (
                set(tests["pass"]) & set(tests["fail"])
                or set(tests["pass"]) & set(tests["indeterminate"])
                or set(tests["fail"]) & set(tests["indeterminate"])
            ):
                raise RegistryValidationError("trace test IDs must be disjoint")
        else:
            if m["test_kind"] not in {
                "unit",
                "integration",
                "conformance",
                "fault_injection",
                "golden",
            }:
                raise RegistryValidationError("test_kind is not closed")
            text(m, "test_id")
            text(m, "pack_id")
            path(m, "test_path")
            digest(m, "test_file_hash")
            fixtures = array(m, "fixture_refs")
            ids: list[str] = []
            for fixture in fixtures:
                ref = exact(
                    fixture,
                    {"fixture_id", "pack_id", "path", "file_hash", "artifact_refs"},
                    "fixture ref",
                )
                ids.append(text(ref, "fixture_id"))
                text(ref, "pack_id")
                path(ref, "path")
                digest(ref, "file_hash")
                if type(ref["artifact_refs"]) is not list:  # noqa: E721
                    raise RegistryValidationError("fixture artifact_refs must be an array")
                artifact_keys: list[tuple[str, str]] = []
                for artifact_ref in ref["artifact_refs"]:
                    artifact = exact(
                        artifact_ref, {"artifact_id", "content_hash"}, "fixture artifact ref"
                    )
                    # §4.1 deliberately specifies no ArtifactRef regex here.
                    # The type prefix and same-job uniqueness are properties of
                    # the referenced Artifact inventory/job, unavailable to this
                    # static source grammar; reject only non-empty non-UTF-8
                    # literals and never invent a local prefix convention.
                    artifact_keys.append((text(artifact, "artifact_id"), digest(artifact, "content_hash")))
                if artifact_keys != sorted(artifact_keys) or len(set(artifact_keys)) != len(artifact_keys):
                    raise RegistryValidationError("fixture artifact_refs must be sorted and unique")
            if ids != sorted(ids) or len(ids) != len(set(ids)):
                raise RegistryValidationError("fixture refs must be sorted and unique")
        return cls(kind, m)


_TRACE_FIELDS = {
    "contract_trace": {
        "entry_kind",
        "contract_path",
        "subject_kind",
        "subject_id",
        "schema_path",
        "schema_hash",
        "evaluator",
        "test_ids",
        "rollout_gate",
    },
    "test_fixture_inventory": {
        "entry_kind",
        "test_id",
        "test_kind",
        "pack_id",
        "test_path",
        "test_file_hash",
        "fixture_refs",
    },
}
