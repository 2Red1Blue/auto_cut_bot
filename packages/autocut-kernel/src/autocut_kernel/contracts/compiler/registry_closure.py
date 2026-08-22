# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportAttributeAccessIssue=false, reportUnusedVariable=false, reportOperatorIssue=false, reportGeneralTypeIssues=false, reportIndexIssue=false
"""Fail-closed cross-registry closure checks for a RegistrySet snapshot."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

from .canonical import canonical_json_bytes, canonical_json_hash, sha256_bytes
from .errors import RegistryValidationError
from .registry_entries import (
    CLOSED_SCOPE_KINDS,
    ArtifactEntry,
    CommandEntry,
    RuleEntry,
    StrategyEntry,
    TraceEntry,
)

if TYPE_CHECKING:
    from .registry_source import SourcePack, SourceSnapshot

_MATRIX = {
    "PrepareMediaEvidence": ("root_input_commit", "artifact_set_commit"),
    "StartRun": ("absent", "run_bootstrap_cas"),
    "BuildNarrativeGraph": ("stage_admission", "artifact_set_commit"),
    "CompileStoryPortfolio": ("stage_admission", "artifact_set_commit"),
    "BuildEditorialBlueprint": ("stage_admission", "artifact_set_commit"),
    "CompilePhysicalEdit": ("stage_admission", "artifact_set_with_lineage_cas"),
    "AdvanceSourceUsageLedger": ("absent", "lineage_head_cas"),
    "RenderAndEvaluatePublication": ("story_release", "artifact_set_commit"),
    "CreatePreRenderStoryDeny": ("story_release", "artifact_set_commit"),
    "DecidePortfolioRelease": ("portfolio_release", "artifact_set_commit"),
    "PlanPublicationBatch": ("publication_transition", "artifact_set_with_publication_lineage_cas"),
    "PreparePublicationBatch": ("publication_transition", "external_publication_lifecycle"),
    "CommitPublicationBatch": ("publication_transition", "external_publication_lifecycle"),
    "AbortPublicationBatch": ("publication_transition", "external_publication_lifecycle"),
    "ReconcilePublicationBatch": ("publication_transition", "external_publication_lifecycle"),
    "PublishIndependentOutput": ("absent", "external_publication_lifecycle"),
    "RecoverScope": ("recover_scope_outcome", "recovery_outcome_protocol"),
    "FinalizeRunOutcome": ("run_outcome", "artifact_set_commit"),
    "MigrateLegacyArtifacts": ("migration_assessment", "artifact_set_commit"),
}


class _SourceInventoryResolver:
    """Read declared source from a verified immutable source-manifest snapshot."""

    def __init__(self, packs: tuple["SourcePack", ...], snapshot: tuple["SourceSnapshot", ...]) -> None:
        self.known = {entry.path: entry.file_hash for pack in packs for entry in pack.source_paths}
        self._bytes_by_path = {entry.path: entry.raw for entry in snapshot}
        if set(self.known) != set(self._bytes_by_path):
            raise RegistryValidationError("source snapshot does not exactly cover manifest inventory")
        if any(sha256_bytes(self._bytes_by_path[path]) != digest for path, digest in self.known.items()):
            raise RegistryValidationError("source snapshot raw bytes do not match manifest inventory")

    def _bytes(self, path: object) -> bytes:
        if not isinstance(path, str) or path not in self.known:
            raise RegistryValidationError("source is absent from manifest inventory")
        return self._bytes_by_path[path]

    def read_raw_hash(self, path: object) -> str:
        return sha256_bytes(self._bytes(path))

    def read_json(self, path: object, expected_hash: object) -> object:
        if not isinstance(expected_hash, str):
            raise RegistryValidationError("JSON source hash is invalid")
        value = _load_json_no_duplicates(self._bytes(path))
        if canonical_json_hash(value) != expected_hash:
            raise RegistryValidationError("JSON source hash does not match")
        return value

    def read_json_hash(self, path: object) -> str:
        try:
            return canonical_json_hash(_load_json_no_duplicates(self._bytes(path)))
        except RegistryValidationError as error:
            raise RegistryValidationError("declared source is not JSON") from error

    def contract_hash(self, path: object) -> str:
        raw = self._bytes(path)
        if isinstance(path, str) and path.endswith(".json"):
            try:
                return canonical_json_hash(_load_json_no_duplicates(raw))
            except RegistryValidationError as error:
                raise RegistryValidationError("JSON contract cannot be parsed") from error
        return sha256_bytes(raw)


def _load_json_no_duplicates(raw: bytes) -> object:
    """Decode UTF-8 JSON with duplicate keys rejected at every nesting level."""
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise RegistryValidationError("JSON source contains a duplicate key")
            result[key] = value
        return result

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise RegistryValidationError("declared source is not JSON") from error


def _strict_sorted_strings(value: object) -> bool:
    return (
        isinstance(value, list)
        and all(type(item) is str and item and item == item.strip() and "\n" not in item for item in value)
        and value == sorted(value)
        and len(set(value)) == len(value)
    )


def _scope_enum_errors(
    artifacts: tuple[ArtifactEntry, ...],
    profiles: list[CommandEntry],
    commands: tuple[CommandEntry, ...],
    errors: list[str],
) -> None:
    """Defence in depth for every Registry field that carries a scope kind."""
    for artifact in artifacts:
        if any(scope not in CLOSED_SCOPE_KINDS for scope in artifact.allowed_scope_kinds):
            errors.append("artifact allowed_scope_kinds uses an unknown v2.1.3 scope")
    for profile in profiles:
        scopes = profile.data["allowed_scope_kinds"]
        if not isinstance(scopes, list) or any(scope not in CLOSED_SCOPE_KINDS for scope in scopes):
            errors.append("command allowed_scope_kinds uses an unknown v2.1.3 scope")
    for command in commands:
        if command.entry_kind != "artifact_set_profile":
            continue
        for group in ("required_member_roles", "conditional_member_roles"):
            members = command.data[group]
            if isinstance(members, list):
                for member in members:
                    if isinstance(member, dict) and isinstance(member.get("scope_kinds"), list) and any(
                        scope not in CLOSED_SCOPE_KINDS for scope in member["scope_kinds"]
                    ):
                        errors.append("artifact-set member scope_kinds uses an unknown v2.1.3 scope")
        heads = command.data["affected_chain_heads"]
        if isinstance(heads, list) and any(
            isinstance(head, dict) and head.get("scope_kind") not in CLOSED_SCOPE_KINDS
            for head in heads
        ):
            errors.append("artifact-set affected_chain_heads uses an unknown v2.1.3 scope")


def closure_errors(
    artifacts: tuple[ArtifactEntry, ...],
    commands: tuple[CommandEntry, ...],
    rules: tuple[RuleEntry, ...],
    strategies: tuple[StrategyEntry, ...],
    traces: tuple[TraceEntry, ...],
    *,
    source_snapshot: tuple["SourceSnapshot", ...] = (),
    source_packs: tuple["SourcePack", ...] = (),
) -> tuple[str, ...]:
    """Return all detectable closure failures; never infer missing evidence."""
    errors: list[str] = []
    _duplicates([a.artifact_type for a in artifacts], "artifact identity", errors)
    _duplicates([r.data["rule_id"] for r in rules], "rule identity", errors)
    components = {(s.data["component_id"], s.data["component_version"]): s for s in strategies}
    _duplicates(
        [(s.data["component_id"], s.data["component_version"]) for s in strategies],
        "component identity",
        errors,
    )
    profiles = [c for c in commands if c.entry_kind == "command_profile"]
    profile_by_key = {(p.data["command_id"], p.data["command_version"]): p for p in profiles}
    _duplicates(
        [(p.data["command_id"], p.data["command_version"]) for p in profiles],
        "command profile identity",
        errors,
    )
    _duplicates([p.data["profile_id"] for p in profiles], "command profile_id", errors)
    set_profile_entries = [c for c in commands if c.entry_kind == "artifact_set_profile"]
    _duplicates(
        [c.data["artifact_set_profile"] for c in set_profile_entries],
        "artifact-set profile identity",
        errors,
    )
    command_transitions = [c for c in commands if c.entry_kind == "command_state_transition"]
    artifact_transitions = [c for c in commands if c.entry_kind == "artifact_state_transition"]
    _duplicates(
        [
            (c.data["command_id"], c.data["command_version"], c.data["transition_id"])
            for c in command_transitions
        ],
        "command transition identity",
        errors,
    )
    _duplicates(
        [(c.data["artifact_type"], c.data["transition_id"]) for c in artifact_transitions],
        "artifact transition identity",
        errors,
    )
    authority_operations = [c for c in commands if c.entry_kind == "authority_operation"]
    _duplicates(
        [(c.data["authority_kind"], c.data["authority_id"]) for c in authority_operations],
        "authority operation identity",
        errors,
    )
    names = [p.data["command_name"] for p in profiles]
    if set(names) != set(_MATRIX) or len(names) != len(_MATRIX):
        errors.append("command matrix is incomplete or contains an unknown command")
    for profile in profiles:
        plan = profile.data["artifact_set_plan"]
        command_name = profile.data["command_name"]
        if type(command_name) is not str or type(plan) is not dict:  # noqa: E721
            errors.append("invalid command profile")
            continue
        expected = _MATRIX.get(command_name)
        observed = (
            plan.get("kind")
            if plan.get("kind") == "recover_scope_outcome"
            else plan.get("artifact_set_profile"),
            profile.data["commit_protocol"],
        )
        if expected != observed:
            errors.append(f"invalid canonical command matrix for {profile.data['command_name']}")
        if profile.data["command_version"] != "2.1.3":
            errors.append("canonical command version must be 2.1.3")
        if (
            profile.data["command_name"] == "RecoverScope"
            and plan.get("kind") != "recover_scope_outcome"
        ):
            errors.append("RecoverScope must use recover_scope_outcome")
        handler = (profile.data["handler_id"], profile.data["handler_version"])
        handler_entry = components.get(handler)
        if (
            handler_entry is None
            or handler_entry.data["kind"] != "command_handler"
            or profile.data["required_capability"] not in handler_entry.data["capabilities"]
        ):
            errors.append("dangling command handler")
        idempotency = components.get(
            (
                profile.data["idempotency_algorithm_id"],
                profile.data["idempotency_algorithm_version"],
            )
        )
        if (
            idempotency is None
            or idempotency.data["kind"] != "idempotency_algorithm"
            or idempotency.data["implementation_contract_hash"]
            != profile.data["idempotency_algorithm_contract_hash"]
        ):
            errors.append("dangling idempotency algorithm")
    profiles_by_name = {p.data["command_name"]: p for p in profiles}
    set_profiles = {c.data["artifact_set_profile"] for c in set_profile_entries}
    for name, (shape, _) in _MATRIX.items():
        if shape not in {"absent", "recover_scope_outcome"} and shape not in set_profiles:
            errors.append("dangling artifact-set profile")
    _scope_enum_errors(artifacts, profiles, commands, errors)
    _artifact_set_profile_errors(commands, artifacts, errors)
    if not source_snapshot:
        errors.append("profile schema metadata provenance is unavailable")
    else:
        _profile_schema_metadata_errors(
            profiles,
            artifacts,
            _SourceInventoryResolver(source_packs, source_snapshot),
            errors,
        )
    for artifact in artifacts:
        for producer in artifact.permitted_producer_components:
            if (producer["component_id"], producer["component_version"]) not in components:
                errors.append("dangling artifact producer component")
        for writer in artifact.authority_writers:
            if (
                writer["kind"] == "command"
                and (writer["command_id"], writer["command_version"]) not in profile_by_key
            ):
                errors.append("dangling artifact command authority")
            if writer["kind"] != "command" and not any(
                entry.entry_kind == "authority_operation"
                and entry.data["authority_kind"] == writer["kind"]
                and entry.data["authority_id"] == writer["authority_id"]
                and artifact.artifact_type in entry.data["allowed_artifact_types"]
                for entry in commands
            ):
                errors.append("dangling artifact authority operation")
        policy = artifact.policy_requirements
        if policy["kind"] == "required_types" and any(
            policy_type not in {entry.artifact_type for entry in artifacts}
            for policy_type in policy["policy_types"]
        ):
            errors.append("dangling artifact policy type")
    _authority_operation_errors(artifacts, authority_operations, errors)
    _transition_subject_errors(
        profiles, artifacts, command_transitions, artifact_transitions, errors
    )
    outcomes = [c for c in commands if c.entry_kind == "recover_scope_outcome"]
    grouped: dict[tuple[object, object, object], list[CommandEntry]] = {}
    for outcome in outcomes:
        d = outcome.data
        key = (d["strategy_id"], d["strategy_version"], d["strategy_implementation_contract_hash"])
        grouped.setdefault(key, []).append(outcome)
        strategy = components.get((d["strategy_id"], d["strategy_version"]))
        if (
            strategy is None
            or strategy.data["kind"] != "recovery"
            or strategy.data["implementation_contract_hash"]
            != d["strategy_implementation_contract_hash"]
        ):
            errors.append("recovery outcome has dangling or non-recovery strategy")
        if d["outcome_branch"] == "business_effect":
            business = profile_by_key.get((d["business_command_id"], d["business_command_version"]))
            if business is None:
                errors.append("recovery business outcome has dangling command")
            else:
                bp = business.data["artifact_set_plan"]
                if (
                    bp.get("kind") != "fixed"
                    or d["business_artifact_set_profile"] != bp.get("artifact_set_profile")
                    or d["business_commit_protocol"] != business.data["commit_protocol"]
                ):
                    errors.append("recovery business outcome does not match business command")
    for grouped_outcomes in grouped.values():
        if {o.data["outcome_branch"] for o in grouped_outcomes} != {
            "business_effect",
            "exhausted_evidence",
        } or len(grouped_outcomes) != 2:
            errors.append(
                "recovery mapping must have exactly business_effect and exhausted_evidence"
            )
    _duplicates(
        [
            (
                o.data["strategy_id"],
                o.data["strategy_version"],
                o.data["strategy_implementation_contract_hash"],
                o.data["outcome_branch"],
            )
            for o in outcomes
        ],
        "recovery outcome identity",
        errors,
    )
    if profiles_by_name.get("RecoverScope") and not grouped:
        errors.append("RecoverScope has no outcome mapping")
    trace_entries = [t for t in traces if t.entry_kind == "contract_trace"]
    for profile in profiles:
        subject = f"command:{profile.data['command_id']}@{profile.data['command_version']}"
        matches = [trace for trace in trace_entries if trace.data["subject_kind"] == "command" and trace.data["subject_id"] == subject]
        if len(matches) != 1:
            errors.append("missing command trace coverage")
        elif matches[0].data["schema_path"] != profile.data["profile_schema_path"] or matches[0].data["schema_hash"] != profile.data["profile_schema_hash"]:
            errors.append("command trace schema does not match its exact profile schema")
    for artifact in artifacts:
        matches = [
            trace for trace in trace_entries
            if trace.data["subject_kind"] == "artifact" and trace.data["subject_id"] == artifact.artifact_type
        ]
        if len(matches) != 1 or matches[0].data["schema_path"] != artifact.payload_schema_path or matches[0].data["schema_hash"] != artifact.payload_schema_hash:
            errors.append("artifact requires exactly one trace with its registered payload schema")
    for rule in rules:
        matches = [
            trace for trace in trace_entries
            if trace.data["subject_kind"] == "rule" and trace.data["subject_id"] == rule.data["rule_id"]
        ]
        if len(matches) != 1 or matches[0].data["schema_path"] != rule.data["diagnostic_schema_path"] or matches[0].data["schema_hash"] != rule.data["diagnostic_schema_hash"]:
            errors.append("rule requires exactly one trace with its registered diagnostic schema")
    _trace_subject_errors(trace_entries, artifacts, profiles, commands, rules, components, errors)
    if not source_snapshot:
        errors.append("state-machine schema provenance is unavailable")
    else:
        _state_machine_closure_errors(
            command_transitions,
            artifact_transitions,
            trace_entries=trace_entries,
            inventory=_SourceInventoryResolver(source_packs, source_snapshot),
            errors=errors,
        )
    transitions = [
        c
        for c in commands
        if c.entry_kind in {"command_state_transition", "artifact_state_transition"}
    ]
    for transition in transitions:
        if transition.entry_kind == "command_state_transition":
            subject = f"command:{transition.data['command_id']}@{transition.data['command_version']}#{transition.data['transition_id']}"
        else:
            subject = (
                f"artifact:{transition.data['artifact_type']}#{transition.data['transition_id']}"
            )
        matches = [
            t for t in trace_entries
            if t.data["subject_kind"] == "state_transition" and t.data["subject_id"] == subject
        ]
        if len(matches) != 1:
            errors.append("state transition requires exactly one trace")
        elif (
            matches[0].data["schema_path"] != transition.data["transition_schema_path"]
            or matches[0].data["schema_hash"] != transition.data["transition_schema_hash"]
        ):
            errors.append("state transition trace must bind its exact transition schema")
    _duplicates([t.data["contract_path"] for t in trace_entries], "trace identity", errors)
    inventory = [t for t in traces if t.entry_kind == "test_fixture_inventory"]
    _duplicates([t.data["test_id"] for t in inventory], "test fixture identity", errors)
    inventory_ids = {t.data["test_id"] for t in inventory}
    for trace in trace_entries:
        all_ids = [
            *trace.data["test_ids"]["pass"],
            *trace.data["test_ids"]["fail"],
            *trace.data["test_ids"]["indeterminate"],
        ]
        if any(test_id not in inventory_ids for test_id in all_ids):
            errors.append("trace references missing test fixture inventory")
    for rule in rules:
        if any(
            artifact_type not in {artifact.artifact_type for artifact in artifacts}
            for artifact_type in rule.data["subject_artifact_types"]
        ):
            errors.append("rule subject artifact type does not resolve to artifact inventory")
        candidates = [
            strategy for strategy in strategies
            if strategy.data["component_id"] == rule.data["evaluator_component"]
            and strategy.data["component_version"] == rule.data["evaluator_component_version"]
            and strategy.data["implementation_contract_hash"] == rule.data["evaluator_contract_hash"]
        ]
        if len(candidates) != 1 or candidates[0].data["kind"] != "evaluator":
            errors.append("dangling rule evaluator")
        if not rule.data["indeterminate_allowed"] and any(
            t.data["subject_kind"] == "rule"
            and t.data["subject_id"] == rule.data["rule_id"]
            and t.data["test_ids"]["indeterminate"]
            for t in trace_entries
        ):
            errors.append("rule disallows indeterminate trace tests")
    if not source_snapshot or not source_packs:
        errors.append("source-pack provenance is unavailable")
    else:
        _source_inventory_errors(
            source_packs, source_snapshot, artifacts, commands, rules, strategies, traces, errors
        )
    return tuple(dict.fromkeys(errors))


def _artifact_set_profile_errors(
    commands: tuple[CommandEntry, ...],
    artifacts: tuple[ArtifactEntry, ...],
    errors: list[str],
) -> None:
    """Validate the set-shape grammar whose meaning cannot be local to YAML."""
    artifact_by_type = {entry.artifact_type: entry for entry in artifacts}
    artifact_types = set(artifact_by_type)
    scopes = CLOSED_SCOPE_KINDS
    for entry in commands:
        if entry.entry_kind != "artifact_set_profile":
            continue
        data = entry.data
        decision_role, decision_type = data["decision_member_role"], data["decision_artifact_type"]
        if not isinstance(decision_role, str) or decision_type not in artifact_types:
            errors.append("artifact-set decision member is unresolved")
        role_names: set[str] = set()
        role_specs: dict[str, list[tuple[str, str]]] = {}
        for group, conditional in (("required_member_roles", False), ("conditional_member_roles", True)):
            items = data[group]
            if not isinstance(items, list):
                errors.append("artifact-set member roles must be an array")
                continue
            observed: list[str] = []
            for item in items:
                expected = {"role", "artifact_types", "scope_kinds", "min_members", "max_members"}
                if conditional:
                    expected |= {"condition_schema_path", "condition_schema_hash"}
                if not isinstance(item, dict) or set(item) != expected:
                    errors.append("artifact-set member role grammar is not closed")
                    continue
                role = item.get("role")
                kinds, item_scopes = item.get("artifact_types"), item.get("scope_kinds")
                minimum, maximum = item.get("min_members"), item.get("max_members")
                if (
                    not isinstance(role, str) or not isinstance(kinds, list) or not isinstance(item_scopes, list)
                    or type(minimum) is not int or type(maximum) is not int
                    or minimum < 0 or maximum < minimum
                    or kinds != sorted(set(kinds)) or item_scopes != sorted(set(item_scopes))
                    or not kinds or not item_scopes
                    or any(kind not in artifact_types for kind in kinds)
                    or any(scope not in scopes for scope in item_scopes)
                    or any(
                        scope not in artifact_by_type[kind].allowed_scope_kinds
                        for kind in kinds for scope in item_scopes
                    )
                ):
                    errors.append("artifact-set member role constraints are invalid")
                if conditional and (not isinstance(item.get("condition_schema_path"), str) or not isinstance(item.get("condition_schema_hash"), str)):
                    errors.append("conditional member role is missing its condition schema")
                observed.append(role if isinstance(role, str) else "")
                if isinstance(role, str) and isinstance(kinds, list) and isinstance(item_scopes, list):
                    role_specs[role] = [
                        (kind, scope)
                        for kind in kinds if isinstance(kind, str)
                        for scope in item_scopes if isinstance(scope, str)
                    ]
            if observed != sorted(observed) or len(set(observed)) != len(observed):
                errors.append("artifact-set member roles must be UTF-8 sorted and unique")
            overlap = role_names.intersection(observed)
            if overlap:
                errors.append("artifact-set required and conditional roles overlap")
            role_names.update(observed)
        forbidden = data["forbidden_member_roles"]
        if (
            not isinstance(forbidden, list)
            or not _strict_sorted_strings(forbidden)
            or role_names.intersection(forbidden)
        ):
            errors.append("artifact-set forbidden member roles conflict")
        heads = data["affected_chain_heads"]
        if not isinstance(heads, list) or not heads:
            errors.append("artifact-set affected chain heads are required")
        else:
            observed_heads: list[bytes] = []
            for head in heads:
                if not isinstance(head, dict) or set(head) != {"scope_kind", "artifact_type"} or head.get("scope_kind") not in scopes or head.get("artifact_type") not in artifact_types:
                    errors.append("artifact-set affected chain head is unresolved")
                    continue
                if not any(
                    (head["artifact_type"], head["scope_kind"]) in pairs
                    for pairs in role_specs.values()
                ):
                    errors.append("artifact-set affected chain head uses an undeclared member role")
                observed_heads.append(canonical_json_bytes(head))
            if observed_heads != sorted(observed_heads) or len(set(observed_heads)) != len(observed_heads):
                errors.append("artifact-set affected chain heads must be sorted and unique")
        directions = data["forbidden_reference_directions"]
        if not isinstance(directions, list):
            errors.append("artifact-set forbidden reference directions must be an array")
        else:
            pairs: list[tuple[str, str]] = []
            for direction in directions:
                if not isinstance(direction, dict) or set(direction) != {"from_role", "to_role"}:
                    errors.append("artifact-set forbidden reference direction grammar is not closed")
                    continue
                left, right = direction.get("from_role"), direction.get("to_role")
                if not isinstance(left, str) or not isinstance(right, str) or left == right:
                    errors.append("artifact-set forbidden reference direction is invalid")
                elif left not in role_specs or right not in role_specs:
                    errors.append("artifact-set reference direction uses an undeclared member role")
                else:
                    pairs.append((left, right))
            if pairs != sorted(pairs) or len(set(pairs)) != len(pairs):
                errors.append("artifact-set forbidden reference directions must be sorted and unique")
        # The decision artifact is itself a required single member, never an
        # implicit compiler default.
        matching = [
            item for item in data.get("required_member_roles", [])
            if isinstance(item, dict) and item.get("role") == decision_role
            and decision_type in item.get("artifact_types", [])
            and item.get("min_members") == item.get("max_members") == 1
        ]
        if len(matching) != 1:
            errors.append("artifact-set decision member must be exactly one required member")
        elif not isinstance(decision_role, str) or not any(
            decision_type == kind for kind, _ in role_specs.get(decision_role, [])
        ):
            errors.append("artifact-set decision member is incompatible with its role")


def _profile_schema_metadata_errors(
    profiles: list[CommandEntry],
    artifacts: tuple[ArtifactEntry, ...],
    inventory: "_SourceInventoryResolver",
    errors: list[str],
) -> None:
    """Bind profile roles to the exact 2020-12 schema source.

    The role declaration is intentionally metadata rather than a Python
    default: it is verified from the same JCS-hashed JSON file named by every
    command profile.
    """
    artifact_by_type = {entry.artifact_type: entry for entry in artifacts}
    for profile in profiles:
        data = profile.data
        candidate = data.get("profile_schema_path")
        if not isinstance(candidate, str):
            errors.append("command profile schema path is invalid")
            continue
        try:
            schema = inventory.read_json(candidate, data.get("profile_schema_hash"))
        except RegistryValidationError:
            errors.append("command profile schema cannot be read as JSON")
            continue
        if not isinstance(schema, dict) or schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
            errors.append("command profile schema is not JSON Schema 2020-12")
            continue
        metadata = schema.get("x-autocut-role-declarations")
        if not isinstance(metadata, dict) or set(metadata) != {"input", "policy", "lifecycle_slots"}:
            errors.append("command profile role metadata is missing or not closed")
            continue
        declared: dict[str, tuple[str, ...]] = {}
        for field in ("input", "policy"):
            entries = metadata[field]
            if not isinstance(entries, list):
                errors.append("command profile role metadata arrays are invalid")
                continue
            roles: list[str] = []
            for entry in entries:
                if not isinstance(entry, dict) or set(entry) != {
                    "role", "artifact_types", "scope_kinds", "min_refs", "max_refs"
                }:
                    errors.append("command profile role metadata item is not closed")
                    continue
                role, types, scopes = entry.get("role"), entry.get("artifact_types"), entry.get("scope_kinds")
                minimum, maximum = entry.get("min_refs"), entry.get("max_refs")
                valid_arrays = (
                    isinstance(types, list) and bool(types) and all(isinstance(item, str) for item in types)
                    and types == sorted(set(types))
                    and isinstance(scopes, list) and bool(scopes) and all(isinstance(item, str) for item in scopes)
                    and scopes == sorted(set(scopes))
                    and all(item in CLOSED_SCOPE_KINDS for item in scopes)
                )
                valid_counts = type(minimum) is int and type(maximum) is int and minimum >= 0 and maximum >= minimum
                if not isinstance(role, str) or not role or not valid_arrays or not valid_counts:
                    errors.append("command profile role metadata constraints are invalid")
                    continue
                typed_types = cast(list[str], types)
                typed_scopes = cast(list[str], scopes)
                if any(item not in artifact_by_type for item in typed_types):
                    errors.append("command profile role metadata has unknown artifact type")
                elif any(
                    scope not in artifact_by_type[item].allowed_scope_kinds
                    for item in typed_types for scope in typed_scopes
                ):
                    errors.append("command profile role metadata has incompatible artifact scope")
                roles.append(role)
            if roles != sorted(roles) or len(set(roles)) != len(roles):
                errors.append("command profile role metadata roles must be sorted and unique")
            declared[field] = tuple(roles)
        slots = metadata["lifecycle_slots"]
        if (
            not isinstance(slots, list)
            or not all(isinstance(slot, str) for slot in slots)
            or slots != sorted(set(slots))
            or any(slot not in {"initial", "recovery", "reconcile", "migration", "bootstrap"} for slot in slots)
        ):
            errors.append("command profile lifecycle slot metadata is invalid")
            slots = []
        input_roles = data["required_input_roles"]
        policy_roles = data["required_policy_roles"]
        profile_slots = data["lifecycle_slots"]
        if (
            not isinstance(input_roles, list)
            or not isinstance(policy_roles, list)
            or not isinstance(profile_slots, list)
        ):
            errors.append("command profile role arrays are invalid")
            continue
        if tuple(input_roles) != declared.get("input", ()):
            errors.append("command required_input_roles do not exactly match profile schema metadata")
        if tuple(policy_roles) != declared.get("policy", ()):
            errors.append("command required_policy_roles do not exactly match profile schema metadata")
        if tuple(profile_slots) != tuple(cast(list[str], slots)):
            errors.append("command lifecycle_slots do not exactly match profile schema metadata")


def _authority_operation_errors(
    artifacts: tuple[ArtifactEntry, ...],
    operations: list[CommandEntry],
    errors: list[str],
) -> None:
    """Require the authority inventory and Artifact writer refs to be exact inverses."""
    writers: dict[tuple[object, object], set[object]] = {}
    for artifact in artifacts:
        for writer in artifact.authority_writers:
            if writer["kind"] == "command":
                continue
            key = (writer["kind"], writer["authority_id"])
            writers.setdefault(key, set()).add(artifact.artifact_type)
    for operation in operations:
        key = (operation.data["authority_kind"], operation.data["authority_id"])
        allowed = operation.data["allowed_artifact_types"]
        if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
            errors.append("authority operation allowed artifact types are invalid")
            continue
        if key not in writers:
            errors.append("orphan authority operation has no artifact writer reference")
        elif writers[key] != set(allowed):
            errors.append("authority operation allowed artifacts do not exactly match writer refs")


def _transition_subject_errors(
    profiles: list[CommandEntry],
    artifacts: tuple[ArtifactEntry, ...],
    command_transitions: list[CommandEntry],
    artifact_transitions: list[CommandEntry],
    errors: list[str],
) -> None:
    """A transition cannot obtain trace coverage until its owning subject resolves."""
    profile_keys = {(entry.data["command_id"], entry.data["command_version"]) for entry in profiles}
    artifact_types = {entry.artifact_type for entry in artifacts}
    for transition in command_transitions:
        key = (transition.data["command_id"], transition.data["command_version"])
        if key not in profile_keys:
            errors.append("command transition does not resolve to an exact command profile")
    for transition in artifact_transitions:
        if transition.data["artifact_type"] not in artifact_types:
            errors.append("artifact transition does not resolve to an exact artifact")


def _state_machine_closure_errors(
    command_transitions: list[CommandEntry],
    artifact_transitions: list[CommandEntry],
    *,
    trace_entries: list[TraceEntry],
    inventory: "_SourceInventoryResolver",
    errors: list[str],
) -> None:
    """Prove the state-machine/schema/Registry/Trace four-way closure.

    State semantics are intentionally read only from the two explicit root
    metadata objects named by the authority.  JSON Schema properties, enums,
    descriptions, examples and runtime conventions are never used as a
    fallback state machine.
    """
    # A state-machine declaration belongs to exactly one family.  Grouping by
    # family first would let a resigned command and artifact entry each claim
    # the same `(machine source, transition_id)` independently, which is the
    # ambiguity the source grammar expressly prohibits.
    groups: dict[tuple[object, object], list[CommandEntry]] = {}
    for entry in (*command_transitions, *artifact_transitions):
        groups.setdefault(
            (entry.data["state_machine_schema_path"], entry.data["state_machine_schema_hash"]),
            [],
        ).append(entry)
    for (machine_path, machine_hash), entries in groups.items():
        try:
            machine = inventory.read_json(machine_path, machine_hash)
        except RegistryValidationError:
            errors.append("state-machine schema cannot be read as exact JSON source")
            continue
        declared = _state_machine_transitions(machine, errors)
        if declared is None:
            continue
        declared_by_id = {item[0]: item for item in declared}
        registry_by_id: dict[str, CommandEntry] = {}
        for entry in entries:
            transition_id = entry.data["transition_id"]
            if not isinstance(transition_id, str):
                errors.append("state transition ID is invalid")
                continue
            if transition_id in registry_by_id:
                prior = registry_by_id[transition_id]
                if prior.entry_kind != entry.entry_kind:
                    errors.append(
                        "state-machine transition declaration is occupied by multiple families"
                    )
                else:
                    # The identity check above will report the duplicate too,
                    # but this prevents an arbitrary last entry masking it.
                    errors.append("state-machine declaration has duplicate registry transition")
                continue
            registry_by_id[transition_id] = entry
            metadata = declared_by_id.get(transition_id)
            observed = (
                entry.data["transition_id"],
                entry.data["from_state"],
                entry.data["to_state"],
            )
            if metadata != observed:
                errors.append("registry transition does not exactly match state-machine declaration")
            _transition_schema_metadata_errors(entry, observed, inventory, errors)
        if set(registry_by_id) != set(declared_by_id):
            errors.append("state-machine declared transitions and registry transitions are not one-to-one")
        for transition_id, entry in registry_by_id.items():
            family = entry.entry_kind
            subject = (
                f"command:{entry.data['command_id']}@{entry.data['command_version']}#{transition_id}"
                if family == "command_state_transition"
                else f"artifact:{entry.data['artifact_type']}#{transition_id}"
            )
            traces = [
                trace
                for trace in trace_entries
                if trace.data["subject_kind"] == "state_transition"
                and trace.data["subject_id"] == subject
            ]
            if len(traces) != 1:
                errors.append("state-machine transition requires exactly one trace")
            elif (
                traces[0].data["schema_path"] != entry.data["transition_schema_path"]
                or traces[0].data["schema_hash"] != entry.data["transition_schema_hash"]
            ):
                errors.append("state-machine transition trace does not bind exact transition schema")
            # A trace with this exact transition schema and transition ID but
            # the opposite subject prefix is an attempted cross-family bind.
            conflicting_family_traces = [
                trace
                for trace in trace_entries
                if trace.data["subject_kind"] == "state_transition"
                and trace.data["schema_path"] == entry.data["transition_schema_path"]
                and trace.data["schema_hash"] == entry.data["transition_schema_hash"]
                and isinstance(trace.data["subject_id"], str)
                and trace.data["subject_id"].endswith(f"#{transition_id}")
                and trace.data["subject_id"] != subject
            ]
            if conflicting_family_traces:
                errors.append(
                    "state transition trace subject family does not match exact transition entry"
                )


def _state_machine_transitions(
    schema: object, errors: list[str]
) -> list[tuple[str, str, str]] | None:
    if not isinstance(schema, dict) or schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        errors.append("state-machine schema is not JSON Schema 2020-12")
        return None
    metadata = schema.get("x-autocut-state-machine")
    if not isinstance(metadata, dict) or set(metadata) != {"states", "transitions"}:
        errors.append("state-machine metadata is missing or not closed")
        return None
    states = metadata.get("states")
    if not _sorted_prefixed_ids(states, "state:", nonempty=True):
        errors.append("state-machine states must be non-empty sorted unique state: IDs")
        return None
    assert isinstance(states, list)
    transitions = metadata.get("transitions")
    if not isinstance(transitions, list):
        errors.append("state-machine transitions must be an array")
        return None
    values: list[tuple[str, str, str]] = []
    ids: list[str] = []
    tuples: list[tuple[str, str]] = []
    for item in transitions:
        if not isinstance(item, dict) or set(item) != {"transition_id", "from_state", "to_state"}:
            errors.append("state-machine transition metadata item is not closed")
            continue
        transition_id, from_state, to_state = (
            item.get("transition_id"),
            item.get("from_state"),
            item.get("to_state"),
        )
        if (
            not isinstance(transition_id, str)
            or not transition_id.startswith("transition:")
            or not isinstance(from_state, str)
            or not from_state.startswith("state:")
            or not isinstance(to_state, str)
            or not to_state.startswith("state:")
            or from_state not in states
            or to_state not in states
        ):
            errors.append("state-machine transition metadata is invalid")
            continue
        values.append((transition_id, from_state, to_state))
        ids.append(transition_id)
        tuples.append((from_state, to_state))
    if values != sorted(values, key=lambda item: tuple(value.encode("utf-8") for value in item)):
        errors.append("state-machine transitions must be sorted")
    if len(set(ids)) != len(ids) or len(set(tuples)) != len(tuples):
        errors.append("state-machine transition ID and state tuple identities must be unique")
    return values


def _transition_schema_metadata_errors(
    entry: CommandEntry,
    expected: tuple[object, object, object],
    inventory: "_SourceInventoryResolver",
    errors: list[str],
) -> None:
    try:
        schema = inventory.read_json(
            entry.data["transition_schema_path"], entry.data["transition_schema_hash"]
        )
    except RegistryValidationError:
        errors.append("transition schema cannot be read as exact JSON source")
        return
    if not isinstance(schema, dict) or schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        errors.append("transition schema is not JSON Schema 2020-12")
        return
    metadata = schema.get("x-autocut-transition")
    if not isinstance(metadata, dict) or set(metadata) != {"transition_id", "from_state", "to_state"}:
        errors.append("transition schema metadata is missing or not closed")
        return
    observed = (metadata.get("transition_id"), metadata.get("from_state"), metadata.get("to_state"))
    if observed != expected:
        errors.append("transition schema metadata does not exactly match registry transition")


def _sorted_prefixed_ids(value: object, prefix: str, *, nonempty: bool) -> bool:
    if not isinstance(value, list) or (nonempty and not value):
        return False
    values = [item for item in value if type(item) is str]
    if len(values) != len(value):
        return False
    return (
        all(
            bool(item)
            and item == item.strip()
            and "\n" not in item
            and item.startswith(prefix)
            for item in values
        )
        and values == sorted(values, key=lambda item: item.encode("utf-8"))
        and len(set(values)) == len(values)
    )


def _trace_subject_errors(
    traces: list[TraceEntry], artifacts: tuple[ArtifactEntry, ...], profiles: list[CommandEntry],
    commands: tuple[CommandEntry, ...], rules: tuple[RuleEntry, ...], components: dict[tuple[object, object], StrategyEntry],
    errors: list[str],
) -> None:
    rule_ids = {entry.data["rule_id"] for entry in rules}
    artifact_ids = {entry.artifact_type for entry in artifacts}
    profile_ids = {(entry.data["command_id"], entry.data["command_version"]) for entry in profiles}
    transition_ids = {
        f"command:{entry.data['command_id']}@{entry.data['command_version']}#{entry.data['transition_id']}"
        if entry.entry_kind == "command_state_transition"
        else f"artifact:{entry.data['artifact_type']}#{entry.data['transition_id']}"
        for entry in commands if entry.entry_kind in {"command_state_transition", "artifact_state_transition"}
    }
    for trace in traces:
        data = trace.data
        kind, subject = data["subject_kind"], data["subject_id"]
        valid = (
            kind == "rule" and subject in rule_ids
            or kind == "artifact" and subject in artifact_ids
            or kind == "command" and isinstance(subject, str) and any(subject == f"command:{ident}@{version}" for ident, version in profile_ids)
            or kind == "state_transition" and subject in transition_ids
        )
        if not valid:
            errors.append("trace subject does not resolve to its declared kind")
        indeterminate = data["test_ids"]["indeterminate"]
        if indeterminate:
            matching_rule = next(
                (
                    rule
                    for rule in rules
                    if kind == "rule" and rule.data["rule_id"] == subject
                ),
                None,
            )
            if matching_rule is None or matching_rule.data["indeterminate_allowed"] is not True:
                errors.append(
                    "trace indeterminate tests require an exact indeterminate-allowed rule"
                )
        evaluator = data["evaluator"]
        if not isinstance(evaluator, dict) or (
            evaluator.get("component"), evaluator.get("version")
        ) not in components or (
            components[(evaluator.get("component"), evaluator.get("version"))].data["kind"]
            != "evaluator"
            or components[(evaluator.get("component"), evaluator.get("version"))].data[
                "implementation_contract_hash"
            ]
            != evaluator.get("contract_hash")
        ):
            errors.append("trace evaluator is not an exact component reference")


def _source_inventory_errors(
    packs: tuple["SourcePack", ...],
    snapshot: tuple["SourceSnapshot", ...],
    artifacts: tuple[ArtifactEntry, ...],
    commands: tuple[CommandEntry, ...],
    rules: tuple[RuleEntry, ...],
    strategies: tuple[StrategyEntry, ...],
    traces: tuple[TraceEntry, ...],
    errors: list[str],
) -> None:
    resolver = _SourceInventoryResolver(packs, snapshot)
    known = resolver.known
    pack_kinds = {pack.kind: pack for pack in packs}
    roots = {pack.pack_id: pack.root for pack in packs}

    def owner(data: dict[str, object], label: str) -> None:
        pack = pack_kinds.get(str(data.get("owner_pack")))
        if pack is None:
            errors.append(f"{label} owner pack is unknown")
            return
        owned = {entry.path: entry.file_hash for entry in pack.source_paths}
        source_path, source_hash = data.get("owner_source_path"), data.get("owner_source_hash")
        contract_path, contract_hash = data.get("owner_contract_path"), data.get("owner_contract_hash")
        if not isinstance(source_path, str) or owned.get(source_path) != source_hash:
            errors.append(f"{label} owner source is not a direct owner-pack inventory entry")
        elif resolver.read_raw_hash(source_path) != source_hash:
            errors.append(f"{label} owner source hash does not match raw bytes")
        if not isinstance(contract_path, str) or contract_path not in owned:
            errors.append(f"{label} owner contract is not a direct owner-pack inventory entry")
            return
        try:
            expected = resolver.contract_hash(contract_path)
        except RegistryValidationError:
            errors.append(f"{label} owner contract cannot be rehashed")
            return
        if expected != contract_hash:
            errors.append(f"{label} owner contract hash does not match exact bytes")

    def source(path: object, expected_hash: object, label: str, *, raw: bool = False) -> None:
        if not isinstance(path, str) or not isinstance(expected_hash, str) or path not in known:
            errors.append(f"{label} source is absent from manifest inventory")
            return
        try:
            actual = resolver.read_raw_hash(path) if raw else resolver.read_json_hash(path)
        except RegistryValidationError:
            errors.append(f"{label} source is not a valid JSON source")
            return
        if actual != expected_hash or (raw and known[path] != expected_hash):
            errors.append(f"{label} source hash does not match")

    for artifact in artifacts:
        owner({
            "owner_pack": artifact.owner_pack,
            "owner_source_path": artifact.owner_source_path,
            "owner_source_hash": artifact.owner_source_hash,
            "owner_contract_path": artifact.owner_contract_path,
            "owner_contract_hash": artifact.owner_contract_hash,
        }, "artifact")
        source(
            artifact.owner_contract_path,
            known.get(artifact.owner_contract_path),
            "artifact owner",
            raw=True,
        )
        source(
            artifact.payload_schema_path, artifact.payload_schema_hash, "artifact payload schema"
        )
        source(
            artifact.envelope_schema_path, artifact.envelope_schema_hash, "artifact envelope schema"
        )
    for command in commands:
        data = command.data
        owner(data, f"command {command.entry_kind}")
        if command.entry_kind == "command_profile":
            for stem in ("profile", "request", "parameter", "result"):
                source(
                    data[f"{stem}_schema_path"],
                    data[f"{stem}_schema_hash"],
                    f"command {stem} schema",
                )
        elif command.entry_kind == "artifact_set_profile":
            conditional = data.get("conditional_member_roles")
            if isinstance(conditional, list):
                for item in conditional:
                    if isinstance(item, dict):
                        source(
                            item.get("condition_schema_path"),
                            item.get("condition_schema_hash"),
                            "artifact-set conditional member schema",
                        )
        elif command.entry_kind in {"command_state_transition", "artifact_state_transition"}:
            source(
                data["state_machine_schema_path"],
                data["state_machine_schema_hash"],
                "state-machine schema",
            )
            source(
                data["transition_schema_path"], data["transition_schema_hash"], "transition schema"
            )
        elif command.entry_kind == "authority_operation":
            source(data["contract_path"], data["contract_hash"], "authority contract", raw=True)
    for rule in rules:
        owner(rule.data, "rule")
        source(
            rule.data["diagnostic_schema_path"],
            rule.data["diagnostic_schema_hash"],
            "rule diagnostic schema",
        )
    for strategy in strategies:
        owner(strategy.data, "strategy")
        source(
            strategy.data["implementation_contract_path"],
            strategy.data["implementation_contract_hash"],
            "strategy implementation",
            raw=True,
        )
        source(
            strategy.data["input_schema_path"],
            strategy.data["input_schema_hash"],
            "strategy input schema",
        )
        source(
            strategy.data["output_schema_path"],
            strategy.data["output_schema_hash"],
            "strategy output schema",
        )
    for trace in traces:
        data = trace.data
        owner(data, f"trace {trace.entry_kind}")
        if trace.entry_kind == "contract_trace":
            source(data["schema_path"], data["schema_hash"], "trace schema")
        else:
            pack = roots.get(str(data["pack_id"]))
            if pack is None or not str(data["test_path"]).startswith(f"{pack}/"):
                errors.append("test fixture path is outside its pack")
            source(data["test_path"], data["test_file_hash"], "test fixture", raw=True)
            for fixture in data["fixture_refs"]:
                fixture_pack = roots.get(fixture["pack_id"])
                if fixture_pack is None or not str(fixture["path"]).startswith(f"{fixture_pack}/"):
                    errors.append("fixture path is outside its pack")
                source(fixture["path"], fixture["file_hash"], "fixture", raw=True)


def _duplicates(values: list[object], label: str, errors: list[str]) -> None:
    if len(values) != len(set(values)):
        errors.append(f"duplicate {label}")


def verify_registry_closure(registry: object) -> None:
    """Raise the compiler error once a caller has built a RegistrySet."""
    from .errors import RegistryValidationError

    errors = getattr(registry, "incompleteness_reasons", ("invalid RegistrySet",))
    if errors:
        raise RegistryValidationError("RegistrySet closure failed: " + "; ".join(errors))
