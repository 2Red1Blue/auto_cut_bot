"""Closed, non-executable registry primitives extracted from the production contract."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Mapping

from .errors import RegistryValidationError

_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SCHEMA_URI = re.compile(r"schema://[^\s/]+/.+\Z")
_TRACE_GATES = frozenset(
    {"contract_ci", "shadow_entry", "publication_enablement", "platform_capability"}
)
_PROFILE_FIELDS = frozenset(
    {
        "command_name",
        "command_version",
        "allowed_scope_kinds",
        "parameter_schema_uri",
        "parameter_schema_hash",
        "result_schema_uri",
        "result_schema_hash",
        "required_input_roles",
        "required_policy_roles",
        "lifecycle_slots",
        "transaction_profile",
        "side_effect_class",
        "required_capability",
        "handler_id",
        "handler_version",
    }
)
_TRACE_FIELDS = frozenset({"contract_path", "schema_path", "evaluator", "test_ids", "rollout_gate"})


@dataclass(frozen=True, slots=True)
class CommandContractProfile:
    """The closed command profile shell required by total contract §3.5.

    This class validates only frozen structure.  It deliberately does not claim
    that a schema URI exists or that a handler can execute: those are full
    RegistrySet closure checks, to be enabled only with complete owner packs.
    """

    command_name: str
    command_version: str
    allowed_scope_kinds: tuple[str, ...]
    parameter_schema_uri: str
    parameter_schema_hash: str
    result_schema_uri: str
    result_schema_hash: str
    required_input_roles: tuple[str, ...]
    required_policy_roles: tuple[str, ...]
    lifecycle_slots: tuple[str, ...]
    transaction_profile: str
    side_effect_class: str
    required_capability: str
    handler_id: str
    handler_version: str

    @classmethod
    def from_mapping(cls, value: object) -> "CommandContractProfile":
        mapping = _exact_mapping(value, _PROFILE_FIELDS, label="command_contract_profile")
        profile = cls(
            command_name=_text(mapping, "command_name"),
            command_version=_text(mapping, "command_version"),
            allowed_scope_kinds=_identifier_array(mapping, "allowed_scope_kinds"),
            parameter_schema_uri=_schema_uri(mapping, "parameter_schema_uri"),
            parameter_schema_hash=_sha256(mapping, "parameter_schema_hash"),
            result_schema_uri=_schema_uri(mapping, "result_schema_uri"),
            result_schema_hash=_sha256(mapping, "result_schema_hash"),
            required_input_roles=_identifier_array(mapping, "required_input_roles"),
            required_policy_roles=_identifier_array(mapping, "required_policy_roles"),
            lifecycle_slots=_identifier_array(mapping, "lifecycle_slots"),
            transaction_profile=_text(mapping, "transaction_profile"),
            side_effect_class=_text(mapping, "side_effect_class"),
            required_capability=_text(mapping, "required_capability"),
            handler_id=_text(mapping, "handler_id"),
            handler_version=_text(mapping, "handler_version"),
        )
        return profile

    @property
    def key(self) -> tuple[str, str]:
        return (self.command_name, self.command_version)

    @property
    def parameters_are_static_empty(self) -> bool:
        """Whether §3.5 permits only a closed empty parameters object."""

        return self.command_name not in {"RecoverScope", "MigrateLegacyArtifacts"}


@dataclass(frozen=True, slots=True)
class ContractTrace:
    """The closed trace object required by total contract §12."""

    contract_path: str
    schema_path: str
    evaluator_component: str
    evaluator_version: str
    evaluator_contract_hash: str
    pass_test_ids: tuple[str, ...]
    fail_test_ids: tuple[str, ...]
    indeterminate_test_ids: tuple[str, ...]
    rollout_gate: str

    @classmethod
    def from_mapping(cls, value: object) -> "ContractTrace":
        mapping = _exact_mapping(value, _TRACE_FIELDS, label="contract_trace")
        evaluator = _exact_mapping(
            mapping["evaluator"], {"component", "version", "contract_hash"}, label="trace.evaluator"
        )
        test_ids = _exact_mapping(
            mapping["test_ids"], {"pass", "fail", "indeterminate"}, label="trace.test_ids"
        )
        contract_path = _text(mapping, "contract_path")
        schema_path = _text(mapping, "schema_path")
        if not _safe_schema_path(schema_path):
            raise RegistryValidationError("trace.schema_path must be a safe relative schema path")
        gate = _text(mapping, "rollout_gate")
        if gate not in _TRACE_GATES:
            raise RegistryValidationError("trace.rollout_gate is not a closed rollout gate")
        return cls(
            contract_path=contract_path,
            schema_path=schema_path,
            evaluator_component=_text(evaluator, "component"),
            evaluator_version=_text(evaluator, "version"),
            evaluator_contract_hash=_sha256(evaluator, "contract_hash"),
            pass_test_ids=_identifier_array(test_ids, "pass", non_empty=True),
            fail_test_ids=_identifier_array(test_ids, "fail", non_empty=True),
            indeterminate_test_ids=_identifier_array(test_ids, "indeterminate"),
            rollout_gate=gate,
        )


@dataclass(frozen=True, slots=True)
class PartialRegistrySet:
    """An immutable compiler-only registry fragment which is never executable."""

    command_profiles: tuple[CommandContractProfile, ...]
    contract_traces: tuple[ContractTrace, ...]

    @classmethod
    def build(
        cls,
        *,
        command_profiles: tuple[CommandContractProfile, ...],
        contract_traces: tuple[ContractTrace, ...],
    ) -> "PartialRegistrySet":
        _unique((profile.key for profile in command_profiles), label="command profile")
        _unique((trace.contract_path for trace in contract_traces), label="contract trace path")
        return cls(command_profiles=command_profiles, contract_traces=contract_traces)

    @property
    def ready(self) -> bool:
        return False

    def require_ready(self) -> None:
        raise RegistryValidationError("partial RegistrySet is not executable or runtime-selectable")


def _exact_mapping(value: object, expected: frozenset[str] | set[str], *, label: str) -> Mapping[str, object]:
    if type(value) is not dict:  # noqa: E721 - reject mapping subclasses with hidden behavior.
        raise RegistryValidationError(f"{label} must be an object")
    actual = set(value)
    if actual != expected:
        raise RegistryValidationError(f"{label} must have exactly {sorted(expected)}")
    return value


def _text(mapping: Mapping[str, object], key: str) -> str:
    value = mapping[key]
    if type(value) is not str or not value or value != value.strip() or "\n" in value or "\r" in value:  # noqa: E721
        raise RegistryValidationError(f"{key} must be a normalized non-empty single-line string")
    return value


def _identifier_array(mapping: Mapping[str, object], key: str, *, non_empty: bool = False) -> tuple[str, ...]:
    value = mapping[key]
    if type(value) is not list:  # noqa: E721
        raise RegistryValidationError(f"{key} must be an array")
    identifiers = tuple(_text({key: item}, key) for item in value)
    if non_empty and not identifiers:
        raise RegistryValidationError(f"{key} must be non-empty")
    if len(set(identifiers)) != len(identifiers):
        raise RegistryValidationError(f"{key} must not contain duplicate identifiers")
    return identifiers


def _sha256(mapping: Mapping[str, object], key: str) -> str:
    value = _text(mapping, key)
    if not _SHA256.fullmatch(value):
        raise RegistryValidationError(f"{key} must be a lowercase sha256 digest")
    return value


def _schema_uri(mapping: Mapping[str, object], key: str) -> str:
    value = _text(mapping, key)
    if not _SCHEMA_URI.fullmatch(value):
        raise RegistryValidationError(f"{key} must be a schema URI")
    return value


def _safe_schema_path(value: str) -> bool:
    if "\\" in value:
        return False
    path = PurePosixPath(value)
    return bool(value) and not path.is_absolute() and not any(part in {"", ".", ".."} for part in path.parts)


def _unique(values: object, *, label: str) -> None:
    items = tuple(values)  # type: ignore[arg-type]
    if len(set(items)) != len(items):
        raise RegistryValidationError(f"duplicate {label}")
