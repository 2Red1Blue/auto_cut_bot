# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownParameterType=false
"""RegistrySet boundary and frozen CommandContractProfile grammar."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .canonical import canonical_json_hash
from .errors import RegistryValidationError
from .registry_entries import (
    CLOSED_CAPABILITIES,
    CLOSED_SCOPE_KINDS,
    ArtifactEntry,
    CommandEntry,
    RuleEntry,
    StrategyEntry,
    TraceEntry,
    array,
    contract_obligation_locator,
    digest,
    exact,
    path,
    strings,
    text,
)

_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SCHEMA_URI = re.compile(r"schema://[^\s/]+/.+\Z")
_PROFILE_FIELDS = {
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
    "artifact_set_plan",
    "commit_protocol",
    "side_effect_class",
    "required_capability",
    "handler_id",
    "handler_version",
    "idempotency_algorithm_id",
    "idempotency_algorithm_version",
    "idempotency_algorithm_contract_hash",
}
_TRACE_FIELDS = {"contract_path", "schema_path", "evaluator", "test_ids", "rollout_gate"}
_SOURCE_FIELDS = {"artifacts", "commands", "rules", "strategies", "traces"}
_GATES = {"contract_ci", "shadow_entry", "publication_enablement", "platform_capability"}


@dataclass(frozen=True, slots=True)
class CommandContractProfile:
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
    artifact_set_plan: dict[str, object]
    commit_protocol: str
    side_effect_class: str
    required_capability: str
    handler_id: str
    handler_version: str
    idempotency_algorithm_id: str
    idempotency_algorithm_version: str
    idempotency_algorithm_contract_hash: str

    @classmethod
    def from_mapping(cls, value: object) -> "CommandContractProfile":
        m = exact(value, _PROFILE_FIELDS, "command_contract_profile")
        plan = m["artifact_set_plan"]
        if type(plan) is not dict:
            raise RegistryValidationError("artifact_set_plan must be an object")  # noqa: E721
        if plan.get("kind") == "recover_scope_outcome":
            exact(plan, {"kind"}, "artifact_set_plan")
        else:
            exact(plan, {"kind", "artifact_set_profile"}, "artifact_set_plan")
            if plan["kind"] != "fixed":
                raise RegistryValidationError("artifact_set_plan is not closed")
        from .registry_entries import COMMIT_PROTOCOLS

        protocol = text(m, "commit_protocol")
        if protocol not in COMMIT_PROTOCOLS:
            raise RegistryValidationError("commit_protocol is not closed")
        parameter_uri, result_uri = text(m, "parameter_schema_uri"), text(m, "result_schema_uri")
        if not _SCHEMA_URI.fullmatch(parameter_uri) or not _SCHEMA_URI.fullmatch(result_uri):
            raise RegistryValidationError("schema URI is invalid")
        return cls(
            text(m, "command_name"),
            text(m, "command_version"),
            _closed_scope_kinds(m, "allowed_scope_kinds"),
            parameter_uri,
            digest(m, "parameter_schema_hash"),
            result_uri,
            digest(m, "result_schema_hash"),
            strings(m, "required_input_roles"),
            strings(m, "required_policy_roles"),
            strings(m, "lifecycle_slots"),
            plan,
            protocol,
            text(m, "side_effect_class"),
            _closed_capability(m, "required_capability"),
            text(m, "handler_id"),
            text(m, "handler_version"),
            text(m, "idempotency_algorithm_id"),
            text(m, "idempotency_algorithm_version"),
            digest(m, "idempotency_algorithm_contract_hash"),
        )

    @property
    def key(self) -> tuple[str, str]:
        return self.command_name, self.command_version

    @property
    def parameters_are_static_empty(self) -> bool:
        return self.command_name not in {"RecoverScope", "MigrateLegacyArtifacts"}


@dataclass(frozen=True, slots=True)
class ContractTrace:
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
        m = exact(value, _TRACE_FIELDS, "contract_trace")
        evaluator, tests = (
            exact(m["evaluator"], {"component", "version", "contract_hash"}, "trace.evaluator"),
            exact(m["test_ids"], {"pass", "fail", "indeterminate"}, "trace.test_ids"),
        )
        gate = text(m, "rollout_gate")
        if gate not in _GATES:
            raise RegistryValidationError("trace.rollout_gate is not a closed rollout gate")
        contract_obligation_locator(m, "contract_path")
        path(m, "schema_path")
        return cls(
            text(m, "contract_path"),
            text(m, "schema_path"),
            text(evaluator, "component"),
            text(evaluator, "version"),
            digest(evaluator, "contract_hash"),
            strings(tests, "pass", nonempty=True),
            strings(tests, "fail", nonempty=True),
            strings(tests, "indeterminate"),
            gate,
        )


@dataclass(frozen=True, slots=True)
class PartialRegistrySet:
    command_profiles: tuple[CommandContractProfile, ...]
    contract_traces: tuple[ContractTrace, ...]

    @classmethod
    def build(
        cls,
        *,
        command_profiles: tuple[CommandContractProfile, ...],
        contract_traces: tuple[ContractTrace, ...],
    ) -> "PartialRegistrySet":
        if len({p.key for p in command_profiles}) != len(command_profiles):
            raise RegistryValidationError("duplicate command profile")
        if len({t.contract_path for t in contract_traces}) != len(contract_traces):
            raise RegistryValidationError("duplicate contract trace path")
        return cls(command_profiles, contract_traces)

    @property
    def ready(self) -> bool:
        return False

    def require_ready(self) -> None:
        raise RegistryValidationError("partial RegistrySet is not executable or runtime-selectable")


@dataclass(frozen=True, slots=True)
class RegistrySet:
    source_hash: str
    artifacts: tuple[ArtifactEntry, ...]
    commands: tuple[CommandEntry, ...]
    rules: tuple[RuleEntry, ...]
    strategies: tuple[StrategyEntry, ...]
    traces: tuple[TraceEntry, ...]
    command_profiles: tuple[CommandContractProfile, ...]
    contract_traces: tuple[ContractTrace, ...]
    category_counts: tuple[tuple[str, int], ...]
    incompleteness_reasons: tuple[str, ...]

    @classmethod
    def from_mapping(cls, value: object, *, expected_source_hash: str) -> "RegistrySet":
        """Reject unproven in-memory sources.

        A flat mapping has no source-pack provenance and must never become a
        runtime-selectable registry.  ``from_manifest`` is the sole compiler
        path deliberately exposed on this type.
        """
        del value, expected_source_hash
        raise RegistryValidationError(
            "RegistrySet requires a validated RegistrySourceManifest; "
            "flat mappings are not executable"
        )

    @classmethod
    def from_manifest(cls, manifest: object) -> "RegistrySet":
        """Compile one validated manifest snapshot into a ready RegistrySet."""
        from .registry_source import RegistrySourceManifest, load_registry_source_manifest

        if not isinstance(manifest, RegistrySourceManifest):
            raise RegistryValidationError("RegistrySet requires a validated RegistrySourceManifest")
        # A frozen dataclass is not an authority boundary: an untrusted caller
        # can construct one directly.  Re-read the named snapshot and require
        # exact equality before parsing any entry.
        verified = load_registry_source_manifest(manifest.root)
        if verified != manifest:
            raise RegistryValidationError(
                "RegistrySourceManifest is forged or stale relative to its source snapshot"
            )
        manifest = verified
        documents = {
            document.registry_kind: document.value for document in manifest.registry_documents
        }
        if set(documents) != _SOURCE_FIELDS:
            raise RegistryValidationError(
                "RegistrySourceManifest has incomplete registry documents"
            )
        raw = {name: array(documents[name], "entries") for name in _SOURCE_FIELDS}
        artifacts = tuple(ArtifactEntry.from_mapping(value) for value in raw["artifacts"])
        commands = tuple(CommandEntry.from_mapping(value) for value in raw["commands"])
        rules = tuple(RuleEntry.from_mapping(value) for value in raw["rules"])
        strategies = tuple(StrategyEntry.from_mapping(value) for value in raw["strategies"])
        traces = tuple(TraceEntry.from_mapping(value) for value in raw["traces"])
        profiles = tuple(
            _profile(entry) for entry in commands if entry.entry_kind == "command_profile"
        )
        contract_traces = tuple(
            _trace(entry) for entry in traces if entry.entry_kind == "contract_trace"
        )
        PartialRegistrySet.build(command_profiles=profiles, contract_traces=contract_traces)
        from .registry_closure import closure_errors

        reasons = closure_errors(
            artifacts,
            commands,
            rules,
            strategies,
            traces,
            source_packs=manifest.source_packs,
            source_snapshot=manifest.source_snapshot,
        )
        if reasons:
            raise RegistryValidationError("RegistrySet closure failed: " + "; ".join(reasons))
        return cls(
            manifest.registry_set_hash,
            artifacts,
            commands,
            rules,
            strategies,
            traces,
            profiles,
            contract_traces,
            tuple(sorted((name, len(items)) for name, items in raw.items())),
            (),
        )

    @classmethod
    def _legacy_from_mapping(cls, value: object, *, expected_source_hash: str) -> "RegistrySet":
        m = exact(value, _SOURCE_FIELDS, "registry_set_source")
        if (
            not _SHA256.fullmatch(expected_source_hash)
            or canonical_json_hash(m) != expected_source_hash
        ):
            raise RegistryValidationError(
                "registry_set_source hash does not match expected_source_hash"
            )
        raw = {name: array(m, name) for name in _SOURCE_FIELDS}
        artifacts, commands = (
            tuple(ArtifactEntry.from_mapping(v) for v in raw["artifacts"]),
            tuple(CommandEntry.from_mapping(v) for v in raw["commands"]),
        )
        rules, strategies, traces = (
            tuple(RuleEntry.from_mapping(v) for v in raw["rules"]),
            tuple(StrategyEntry.from_mapping(v) for v in raw["strategies"]),
            tuple(TraceEntry.from_mapping(v) for v in raw["traces"]),
        )
        profiles = tuple(_profile(e) for e in commands if e.entry_kind == "command_profile")
        contract_traces = tuple(_trace(e) for e in traces if e.entry_kind == "contract_trace")
        PartialRegistrySet.build(command_profiles=profiles, contract_traces=contract_traces)
        from .registry_closure import closure_errors

        reasons = (
            *closure_errors(artifacts, commands, rules, strategies, traces),
            "source-pack provenance and real fixture coverage are unavailable",
        )
        return cls(
            expected_source_hash,
            artifacts,
            commands,
            rules,
            strategies,
            traces,
            profiles,
            contract_traces,
            tuple(sorted((name, len(items)) for name, items in raw.items())),
            reasons,
        )

    @property
    def ready(self) -> bool:
        return not self.incompleteness_reasons

    def require_ready(self) -> None:
        if not self.ready:
            raise RegistryValidationError(
                "RegistrySet is not executable or runtime-selectable: "
                + "; ".join(self.incompleteness_reasons)
            )


def _profile(entry: CommandEntry) -> CommandContractProfile:
    return CommandContractProfile.from_mapping({key: entry.data[key] for key in _PROFILE_FIELDS})


def _trace(entry: TraceEntry) -> ContractTrace:
    return ContractTrace.from_mapping({key: entry.data[key] for key in _TRACE_FIELDS})


def _closed_scope_kinds(value: dict[str, object], key: str) -> tuple[str, ...]:
    scopes = strings(value, key)
    if any(scope not in CLOSED_SCOPE_KINDS for scope in scopes):
        raise RegistryValidationError(f"{key} must use the closed v2.1.3 scope enum")
    return scopes


def _closed_capability(value: dict[str, object], key: str) -> str:
    capability = text(value, key)
    if capability not in CLOSED_CAPABILITIES:
        raise RegistryValidationError(f"{key} must use the closed v2.1.3 capability enum")
    return capability
