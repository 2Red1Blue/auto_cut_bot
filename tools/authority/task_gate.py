# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false
"""Task admission and staged-path scope verification."""

from __future__ import annotations

import fnmatch
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .common import (
    PLACEHOLDER_RE,
    canonical_hash,
    contained_path,
    git_bytes,
    git_index_paths,
    git_output,
    load_mapping,
    load_mapping_bytes,
    require_closed,
    require_commit,
    require_list,
    require_non_empty_string,
    require_sha256,
    sha256_bytes,
    sha256_file,
    validate_glob,
    validate_relative_path,
)
from .errors import GateViolation
from .lock import validate_authority_lock
from .receipts import make_typed_receipt

RISK_CLASSES = {"authority", "high", "bounded", "mechanical"}
TASK_TYPES = {"implementation", "authority_change"}
PURPOSES = {"inventory", "fixture", "offline_eval"}
PHASE_ORDER = {
    "phase_minus_1": -1,
    "phase_0": 0,
    "phase_1": 1,
    "phase_2": 2,
    "phase_3": 3,
    "phase_4": 4,
    "phase_5": 5,
    "phase_6": 6,
}

TASK_AUTHORIZATIONS_PATH = "governance/task-authorizations.yaml"
ACTIVATION_PROFILES_PATH = "governance/activation-profiles.yaml"
MODEL_ROLE_POLICY_PATH = "governance/model-role-policy.yaml"
PROTECTED_PATHS_PATH = "governance/protected-paths.yaml"


def _validate_repository_binding(manifest: Mapping[str, Any]) -> list[dict[str, str]]:
    refs = manifest["repository_refs"]
    if not isinstance(refs, dict):
        raise GateViolation("AUTH-TASK-REPOSITORY-REFS", "repository_refs must be an object")
    top_level = (
        manifest["repository"],
        manifest["branch"],
        manifest["base_branch"],
        manifest["worktree_path"],
        manifest["predecessor_commit"],
    )
    if refs:
        if any(value is not None for value in top_level):
            raise GateViolation(
                "AUTH-TASK-CROSS-REPO-TOPLEVEL",
                "cross-repository tasks require null top-level repository/branch/base/worktree",
            )
        if len(refs) < 2:
            raise GateViolation(
                "AUTH-TASK-REPOSITORY-REFS", "cross-repo task needs at least two refs"
            )
        normalized: list[dict[str, str]] = []
        for name, ref in refs.items():
            if not isinstance(ref, dict):
                raise GateViolation("AUTH-TASK-REPOSITORY-REF", f"repository_refs.{name} invalid")
            require_closed(
                ref,
                required=("branch", "base_branch", "worktree_path", "predecessor_commit"),
                where=f"repository_refs.{name}",
            )
            worktree = require_non_empty_string(
                ref["worktree_path"], where=f"repository_refs.{name}.worktree_path"
            )
            if not Path(worktree).is_absolute():
                raise GateViolation("AUTH-TASK-WORKTREE", "worktree_path must be absolute")
            normalized.append(
                {
                    "repository": require_non_empty_string(name, where="repository_refs key"),
                    "branch": require_non_empty_string(
                        ref["branch"], where=f"repository_refs.{name}.branch"
                    ),
                    "base_branch": require_non_empty_string(
                        ref["base_branch"], where=f"repository_refs.{name}.base_branch"
                    ),
                    "predecessor_commit": require_commit(
                        ref["predecessor_commit"],
                        where=f"repository_refs.{name}.predecessor_commit",
                    ),
                    "worktree_path": worktree,
                }
            )
        names = [item["repository"] for item in normalized]
        if len(names) != len(set(names)):
            raise GateViolation("AUTH-TASK-REPOSITORY-DUPLICATE", "repository refs repeat a name")
        return normalized

    if any(value is None for value in top_level):
        raise GateViolation(
            "AUTH-TASK-SINGLE-REPO-TOPLEVEL",
            "single-repository task requires repository/branch/base_commit/worktree",
        )
    worktree = require_non_empty_string(manifest["worktree_path"], where="worktree_path")
    if not Path(worktree).is_absolute():
        raise GateViolation("AUTH-TASK-WORKTREE", "worktree_path must be absolute")
    return [
        {
            "repository": require_non_empty_string(manifest["repository"], where="repository"),
            "branch": require_non_empty_string(manifest["branch"], where="branch"),
            "base_branch": require_non_empty_string(manifest["base_branch"], where="base_branch"),
            "predecessor_commit": require_commit(
                manifest["predecessor_commit"], where="predecessor_commit"
            ),
            "worktree_path": worktree,
        }
    ]


def _validate_registry_binding(
    binding: Any,
    authority_hash: str,
    *,
    activation_profile: str,
    activation_policy: Mapping[str, Any],
) -> None:
    require_closed(
        activation_policy, required=("schema_version", "profiles"), where="activation policy"
    )
    profiles = activation_policy["profiles"]
    if not isinstance(profiles, dict) or activation_profile not in profiles:
        raise GateViolation("AUTH-TASK-ACTIVATION-MISSING", "profile is absent from locked policy")
    selected = profiles[activation_profile]
    if not isinstance(selected, dict):
        raise GateViolation("AUTH-TASK-ACTIVATION-POLICY", "locked profile must be an object")
    require_closed(
        selected,
        required=("current_phase", "predicates"),
        where=f"activation profile {activation_profile}",
    )
    predicates = selected["predicates"]
    if not isinstance(predicates, dict) or not isinstance(
        predicates.get("registry_conformance"), dict
    ):
        raise GateViolation("AUTH-TASK-ACTIVATION-PREDICATE", "locked registry predicate is absent")
    predicate = predicates["registry_conformance"]
    require_closed(
        predicate,
        required=("minimum_phase",),
        where="activation predicate registry_conformance",
    )
    locked_minimum = predicate["minimum_phase"]
    locked_current = selected["current_phase"]
    if locked_minimum not in PHASE_ORDER or locked_current not in PHASE_ORDER:
        raise GateViolation("AUTH-TASK-PHASE", "locked activation policy has unknown phase")
    if not isinstance(binding, dict):
        raise GateViolation("AUTH-TASK-REGISTRY", "registry_binding must be an object")
    kind = binding.get("kind")
    if kind == "present":
        require_closed(
            binding,
            required=("kind", "registry_set_hash", "authority_hash"),
            where="registry_binding",
        )
        require_sha256(binding["registry_set_hash"], where="registry_set_hash")
        if PHASE_ORDER[locked_current] < PHASE_ORDER[locked_minimum]:
            raise GateViolation(
                "AUTH-TASK-REGISTRY-PREMATURE",
                "registry cannot be present before the locked activation phase",
            )
    elif kind == "not_applicable":
        require_closed(
            binding,
            required=(
                "kind",
                "profile",
                "minimum_phase",
                "current_phase",
                "reason",
                "authority_hash",
            ),
            where="registry_binding",
        )
        minimum = binding["minimum_phase"]
        current = binding["current_phase"]
        if binding["profile"] != activation_profile:
            raise GateViolation(
                "AUTH-TASK-ACTIVATION-MISMATCH",
                "registry predicate profile must match the task activation profile",
            )
        if minimum != locked_minimum or current != locked_current:
            raise GateViolation(
                "AUTH-TASK-ACTIVATION-MISMATCH",
                "registry N/A fields differ from authority-locked activation policy",
            )
        if PHASE_ORDER[current] >= PHASE_ORDER[minimum]:
            raise GateViolation(
                "AUTH-TASK-NOT-APPLICABLE-ACTIVE",
                "an activated registry predicate cannot be not_applicable",
            )
        require_non_empty_string(binding["reason"], where="registry_binding.reason")
    else:
        raise GateViolation("AUTH-TASK-REGISTRY-KIND", "unknown registry_binding kind")
    if binding["authority_hash"] != authority_hash:
        raise GateViolation("AUTH-TASK-REGISTRY-AUTHORITY", "registry authority hash is stale")


def _validate_path_rules(manifest: Mapping[str, Any], repository_names: set[str]) -> None:
    for field in ("allowed_write_paths", "forbidden_runtime_import_roots"):
        values = require_list(
            manifest[field], where=field, non_empty=field == "allowed_write_paths"
        )
        for index, item in enumerate(values):
            if not isinstance(item, dict):
                raise GateViolation("AUTH-TASK-PATH-RULE", f"{field}[{index}] must be an object")
            require_closed(item, required=("repository", "pattern"), where=f"{field}[{index}]")
            if item["repository"] not in repository_names:
                raise GateViolation(
                    "AUTH-TASK-PATH-REPOSITORY", f"{field}[{index}] unknown repository"
                )
            validate_glob(item["pattern"], where=f"{field}[{index}].pattern")

    for index, item in enumerate(
        require_list(manifest["permitted_legacy_read_roots"], where="permitted_legacy_read_roots")
    ):
        if not isinstance(item, dict):
            raise GateViolation("AUTH-TASK-LEGACY-READ", f"legacy read [{index}] must be object")
        require_closed(
            item,
            required=("repository", "pattern", "purpose", "audit_required"),
            where=f"permitted_legacy_read_roots[{index}]",
        )
        if item["repository"] not in repository_names:
            raise GateViolation("AUTH-TASK-PATH-REPOSITORY", "legacy read has unknown repository")
        validate_glob(item["pattern"], where=f"permitted_legacy_read_roots[{index}].pattern")
        if item["purpose"] not in PURPOSES or item["audit_required"] is not True:
            raise GateViolation(
                "AUTH-TASK-LEGACY-READ-PURPOSE",
                "legacy read requires inventory|fixture|offline_eval and audit_required=true",
            )


def _validate_context_file(
    item: Mapping[str, Any], *, repository_roots: Mapping[str, Path], where: str
) -> None:
    require_closed(item, required=("repository", "path", "sha256", "byte_length"), where=where)
    repository = require_non_empty_string(item["repository"], where=f"{where}.repository")
    if repository not in repository_roots:
        raise GateViolation("AUTH-TASK-CONTEXT-REPOSITORY", f"{where} unknown repository")
    relative = validate_relative_path(item["path"], where=f"{where}.path")
    expected = require_sha256(item["sha256"], where=f"{where}.sha256")
    if not isinstance(item["byte_length"], int) or item["byte_length"] < 1:
        raise GateViolation("AUTH-TASK-CONTEXT-BYTES", f"{where}.byte_length must be positive")
    path = contained_path(repository_roots[repository], relative, allow_missing=False)
    raw = path.read_bytes()
    if len(raw) != item["byte_length"] or sha256_file(path) != expected:
        raise GateViolation("AUTH-TASK-CONTEXT-HASH", f"{where} bytes/hash mismatch")
    validate_context_content(raw, where=where)


def validate_context_content(raw: bytes, *, where: str) -> None:
    """Reject placeholder planning content through the same production gate."""

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GateViolation("AUTH-TASK-CONTEXT-ENCODING", f"{where} must be UTF-8") from exc
    if PLACEHOLDER_RE.search(text):
        raise GateViolation("AUTH-TASK-CONTEXT-PLACEHOLDER", f"{where} contains a placeholder")


def validate_model_role(manifest: Mapping[str, Any], model_policy: Mapping[str, Any]) -> None:
    require_closed(
        model_policy,
        required=("schema_version", "risk_assignments", "independence"),
        where="model-role policy",
    )
    implementer = manifest["implementer"]
    if not isinstance(implementer, dict):
        raise GateViolation("AUTH-TASK-IMPLEMENTER", "implementer must be an object")
    require_closed(
        implementer,
        required=("run_identity", "model_family", "reasoning_class"),
        where="implementer",
    )
    risk = manifest["risk_class"]
    assignment = model_policy["risk_assignments"].get(risk)
    if not isinstance(assignment, dict):
        raise GateViolation("AUTH-MODEL-RISK", f"risk class has no policy: {risk}")
    if implementer["model_family"] not in assignment.get("allowed_model_families", []):
        raise GateViolation("AUTH-MODEL-FAMILY", "model family is not allowed for task risk")
    if implementer["reasoning_class"] not in assignment.get("allowed_reasoning_classes", []):
        raise GateViolation("AUTH-MODEL-REASONING", "reasoning class is not allowed for task risk")
    require_non_empty_string(implementer["run_identity"], where="implementer.run_identity")


def validate_task_manifest(manifest: Mapping[str, Any]) -> list[dict[str, str]]:
    require_closed(
        manifest,
        required=(
            "schema_version",
            "task_id",
            "task_type",
            "risk_class",
            "activation_profile",
            "authority_lock_hash",
            "registry_binding",
            "repository",
            "branch",
            "base_branch",
            "worktree_path",
            "predecessor_commit",
            "repository_refs",
            "allowed_write_paths",
            "forbidden_runtime_import_roots",
            "permitted_legacy_read_roots",
            "planning_artifacts",
            "implementation_context",
            "check_context",
            "validation_commands",
            "implementer",
            "checker_requirements",
            "authority_change",
        ),
        where="task manifest",
    )
    if manifest["schema_version"] != "1.0.0":
        raise GateViolation("AUTH-TASK-VERSION", "unsupported task manifest version")
    require_non_empty_string(manifest["task_id"], where="task_id")
    if manifest["task_type"] not in TASK_TYPES or manifest["risk_class"] not in RISK_CLASSES:
        raise GateViolation("AUTH-TASK-CLASS", "unknown task_type or risk_class")
    require_sha256(manifest["authority_lock_hash"], where="authority_lock_hash")
    if manifest["activation_profile"] not in {
        "authority_bootstrap",
        "contract_foundation",
        "kernel_execution",
        "stage_business",
        "cutover",
    }:
        raise GateViolation("AUTH-TASK-ACTIVATION", "unknown activation profile")
    checker = manifest["checker_requirements"]
    if not isinstance(checker, dict):
        raise GateViolation("AUTH-TASK-CHECKER", "checker_requirements must be an object")
    require_closed(
        checker,
        required=("independent_run_required", "independent_context_required", "required_class"),
        where="checker_requirements",
    )
    if (
        checker["independent_run_required"] is not True
        or checker["independent_context_required"] is not True
    ):
        raise GateViolation("AUTH-TASK-CHECKER-INDEPENDENCE", "independent checker is mandatory")
    if checker["required_class"] not in {"authority", "high", "bounded"}:
        raise GateViolation("AUTH-TASK-CHECKER-CLASS", "unknown checker class")
    refs = _validate_repository_binding(manifest)
    repository_names = {item["repository"] for item in refs}
    _validate_path_rules(manifest, repository_names)
    if manifest["task_type"] == "authority_change":
        change = manifest["authority_change"]
        if not isinstance(change, dict):
            raise GateViolation("AUTH-TASK-AUTHORITY-CHANGE", "authority_change details required")
        require_closed(
            change,
            required=(
                "old_lock_hash",
                "new_lock_hash",
                "compatibility_impact",
                "invalidated_tasks",
            ),
            where="authority_change",
        )
        require_sha256(change["old_lock_hash"], where="authority_change.old_lock_hash")
        require_sha256(change["new_lock_hash"], where="authority_change.new_lock_hash")
        require_non_empty_string(change["compatibility_impact"], where="compatibility_impact")
        require_list(change["invalidated_tasks"], where="invalidated_tasks")
    elif manifest["authority_change"] is not None:
        raise GateViolation(
            "AUTH-TASK-AUTHORITY-CHANGE", "ordinary task must use null authority_change"
        )
    return refs


def admit_task(
    *,
    manifest_path: Path,
    authority_lock_path: Path,
    model_policy_path: Path,
    protected_paths_path: Path,
    repository_roots: Mapping[str, Path],
) -> list[dict[str, str]]:
    """Validate a task against exact authority, context, repository and model state."""

    manifest = load_mapping(manifest_path)
    refs = validate_task_manifest(manifest)
    lock = load_mapping(authority_lock_path)
    validate_authority_lock(lock)
    if manifest["authority_lock_hash"] != lock["bundle_hash"]:
        raise GateViolation("AUTH-TASK-AUTHORITY-HASH", "task binds a stale authority lock")
    expected_names = {item["repository"] for item in refs}
    if set(repository_roots) != expected_names:
        raise GateViolation(
            "AUTH-TASK-REPOSITORY-ROOTS", "repository roots do not match task binding"
        )
    resolved_roots = {name: path.resolve(strict=True) for name, path in repository_roots.items()}

    activation_policy, _activation_hash = _locked_mapping(
        lock=lock,
        repository_roots=resolved_roots,
        path=ACTIVATION_PROFILES_PATH,
    )
    _validate_registry_binding(
        manifest["registry_binding"],
        lock["bundle_hash"],
        activation_profile=manifest["activation_profile"],
        activation_policy=activation_policy,
    )
    model_policy, model_policy_hash = _locked_mapping(
        lock=lock,
        repository_roots=resolved_roots,
        path=MODEL_ROLE_POLICY_PATH,
    )
    if sha256_file(model_policy_path) != model_policy_hash:
        raise GateViolation(
            "AUTH-TASK-MODEL-POLICY-UNLOCKED",
            "caller model policy does not match the authority-locked Git blob",
        )
    validate_model_role(manifest, model_policy)

    for ref in refs:
        root = resolved_roots[ref["repository"]]
        if git_output(root, "rev-parse", "HEAD") != ref["predecessor_commit"]:
            raise GateViolation("AUTH-TASK-PREDECESSOR", f"{ref['repository']} HEAD mismatch")
        if git_output(root, "branch", "--show-current") != ref["branch"]:
            raise GateViolation("AUTH-TASK-BRANCH", f"{ref['repository']} branch mismatch")
        expected_worktree = Path(ref["worktree_path"]).resolve(strict=True)
        if root != expected_worktree:
            raise GateViolation("AUTH-TASK-WORKTREE", f"{ref['repository']} worktree mismatch")

    artifacts = require_list(
        manifest["planning_artifacts"], where="planning_artifacts", non_empty=True
    )
    required_kinds = {"prd", "design", "implement", "implement_context", "check_context"}
    seen_kinds: set[str] = set()
    for index, artifact in enumerate(artifacts):
        if not isinstance(artifact, dict):
            raise GateViolation("AUTH-TASK-PLANNING", f"planning_artifacts[{index}] invalid")
        require_closed(
            artifact,
            required=("kind", "repository", "path", "sha256", "byte_length"),
            where=f"planning_artifacts[{index}]",
        )
        kind = require_non_empty_string(artifact["kind"], where=f"planning_artifacts[{index}].kind")
        if kind in seen_kinds:
            raise GateViolation("AUTH-TASK-PLANNING-DUPLICATE", f"duplicate planning kind: {kind}")
        seen_kinds.add(kind)
        _validate_context_file(
            {key: value for key, value in artifact.items() if key != "kind"},
            repository_roots=resolved_roots,
            where=f"planning_artifacts[{index}]",
        )
    if not required_kinds.issubset(seen_kinds):
        raise GateViolation(
            "AUTH-TASK-PLANNING-INCOMPLETE", "required planning artifacts are absent"
        )

    for field in ("implementation_context", "check_context"):
        context = manifest[field]
        if not isinstance(context, dict):
            raise GateViolation("AUTH-TASK-CONTEXT", f"{field} must be an object")
        _validate_context_file(context, repository_roots=resolved_roots, where=field)
    if manifest["implementation_context"]["sha256"] == manifest["check_context"]["sha256"]:
        raise GateViolation(
            "AUTH-TASK-CONTEXT-INDEPENDENCE", "implementation/check context must differ"
        )

    commands = require_list(
        manifest["validation_commands"], where="validation_commands", non_empty=True
    )
    for index, command in enumerate(commands):
        if not isinstance(command, dict):
            raise GateViolation("AUTH-TASK-VALIDATION", f"validation_commands[{index}] invalid")
        require_closed(
            command,
            required=("command_id", "repository", "argv"),
            where=f"validation_commands[{index}]",
        )
        if command["repository"] not in expected_names:
            raise GateViolation("AUTH-TASK-VALIDATION-REPOSITORY", "unknown command repository")
        argv = require_list(
            command["argv"], where=f"validation_commands[{index}].argv", non_empty=True
        )
        if not all(isinstance(arg, str) and arg for arg in argv):
            raise GateViolation("AUTH-TASK-VALIDATION-ARGV", "argv must contain non-empty strings")

    protected, protected_hash = _locked_mapping(
        lock=lock,
        repository_roots=resolved_roots,
        path=PROTECTED_PATHS_PATH,
    )
    if sha256_file(protected_paths_path) != protected_hash:
        raise GateViolation(
            "AUTH-TASK-PROTECTED-POLICY-UNLOCKED",
            "caller protected-path policy does not match the authority-locked Git blob",
        )
    protected_by_repo = _protected_patterns(protected)
    if manifest["task_type"] != "authority_change":
        for allowed in manifest["allowed_write_paths"]:
            if _pattern_overlaps_any(
                allowed["pattern"], protected_by_repo.get(allowed["repository"], [])
            ):
                raise GateViolation(
                    "AUTH-TASK-PROTECTED-ALLOWLIST",
                    "ordinary task allowlist overlaps a protected path",
                )
    else:
        _validate_locked_authority_authorization(
            task_id=str(manifest["task_id"]),
            allowed_write_paths=manifest["allowed_write_paths"],
            lock=lock,
            repository_roots=resolved_roots,
        )
    return refs


def _locked_mapping(
    *,
    lock: Mapping[str, Any],
    repository_roots: Mapping[str, Path],
    path: str,
) -> tuple[dict[str, Any], str]:
    matches = [entry for entry in lock["entries"] if entry["path"] == path]
    if len(matches) != 1:
        raise GateViolation("AUTH-LOCK-REQUIRED-SOURCE", f"lock must contain exactly one {path}")
    entry = matches[0]
    repository = str(entry["repository"])
    if repository not in repository_roots:
        raise GateViolation("AUTH-LOCK-REPOSITORY", f"repository is not bound: {repository}")
    raw = git_bytes(
        repository_roots[repository],
        lock["repositories"][repository]["source_commit"],
        path,
    )
    actual = sha256_bytes(raw)
    if actual != entry["sha256"]:
        raise GateViolation("AUTH-LOCK-FILE-HASH", f"locked source mismatch: {path}")
    return load_mapping_bytes(raw, where=path, suffix=Path(path).suffix), actual


def _validate_locked_authority_authorization(
    *,
    task_id: str,
    allowed_write_paths: Any,
    lock: Mapping[str, Any],
    repository_roots: Mapping[str, Path],
) -> str:
    """Authorize protected writes from an independent, locked source."""

    source, _authorization_source_hash = _locked_mapping(
        lock=lock,
        repository_roots=repository_roots,
        path=TASK_AUTHORIZATIONS_PATH,
    )
    require_closed(
        source,
        required=("schema_version", "authority_revision", "authorizations"),
        where="task authorizations",
    )
    matches: list[Mapping[str, Any]] = []
    for index, item in enumerate(require_list(source["authorizations"], where="authorizations")):
        if not isinstance(item, dict):
            raise GateViolation("AUTH-TASK-AUTHORIZATION", f"authorizations[{index}] invalid")
        require_closed(
            item,
            required=(
                "authorization_id",
                "task_id",
                "task_type",
                "allowed_protected_paths",
                "approved_by",
            ),
            where=f"authorizations[{index}]",
        )
        if item["task_id"] == task_id:
            matches.append(item)
    if len(matches) != 1:
        raise GateViolation("AUTH-TASK-AUTHORIZATION-MISSING", "authority task has no unique grant")
    grant = matches[0]
    if grant["task_type"] != "authority_change":
        raise GateViolation("AUTH-TASK-AUTHORIZATION-TYPE", "grant is not for authority_change")
    require_non_empty_string(grant["authorization_id"], where="authorization_id")
    require_non_empty_string(grant["approved_by"], where="approved_by")
    granted = require_list(grant["allowed_protected_paths"], where="allowed_protected_paths")
    normalized_grants: set[tuple[str, str]] = set()
    for index, item in enumerate(granted):
        if not isinstance(item, dict):
            raise GateViolation("AUTH-TASK-AUTHORIZATION-PATH", f"grant path {index} invalid")
        require_closed(item, required=("repository", "pattern"), where=f"grant path {index}")
        normalized_grants.add(
            (
                require_non_empty_string(item["repository"], where="grant repository"),
                validate_glob(item["pattern"], where="grant pattern"),
            )
        )
    requested = {(str(item["repository"]), str(item["pattern"])) for item in allowed_write_paths}
    if not requested.issubset(normalized_grants):
        raise GateViolation(
            "AUTH-TASK-AUTHORIZATION-SCOPE", "task requests paths outside locked grant"
        )
    # The grant binds the inventory that was independently reviewed, not this
    # task manifest's self-declared authority_change payload.
    return str(grant["authorization_id"])


def _protected_patterns(policy: Mapping[str, Any]) -> dict[str, list[str]]:
    require_closed(policy, required=("schema_version", "repositories"), where="protected paths")
    repositories = policy["repositories"]
    if not isinstance(repositories, dict):
        raise GateViolation("AUTH-PROTECTED-REPOSITORIES", "repositories must be an object")
    result: dict[str, list[str]] = {}
    for repository, entry in repositories.items():
        if not isinstance(entry, dict):
            raise GateViolation("AUTH-PROTECTED-ENTRY", f"{repository} policy must be an object")
        require_closed(entry, required=("patterns",), where=f"protected {repository}")
        patterns = require_list(entry["patterns"], where=f"protected {repository}.patterns")
        result[repository] = [
            validate_glob(pattern, where=f"protected {repository}.patterns") for pattern in patterns
        ]
    return result


def _pattern_overlaps_any(pattern: str, protected: Sequence[str]) -> bool:
    # Fail closed for identical/nested fixed prefixes.  Actual changed paths are checked exactly below.
    prefix = pattern.split("*", 1)[0].rstrip("/")
    for candidate in protected:
        protected_prefix = candidate.split("*", 1)[0].rstrip("/")
        if prefix == protected_prefix or prefix.startswith(f"{protected_prefix}/"):
            return True
        if protected_prefix.startswith(f"{prefix}/"):
            return True
    return False


def check_change_scopes(
    *,
    manifest_path: Path,
    protected_paths_path: Path,
    repository_roots: Mapping[str, Path],
    authority_lock_hash: str = "sha256:" + "0" * 64,
) -> list[dict[str, Any]]:
    """Check the actual staged index scope for every bound repository."""

    manifest = load_mapping(manifest_path)
    refs = validate_task_manifest(manifest)
    expected_repositories = {item["repository"] for item in refs}
    if set(repository_roots) != expected_repositories:
        raise GateViolation("AUTH-TASK-REPOSITORY-ROOTS", "repository roots do not match task")
    allowed = manifest["allowed_write_paths"]
    protected = _protected_patterns(load_mapping(protected_paths_path))

    receipts: list[dict[str, Any]] = []
    for ref in refs:
        repository = ref["repository"]
        root = repository_roots[repository]
        predecessor = ref["predecessor_commit"]
        index_tree = git_output(root, "write-tree")
        changed: list[str] = []
        for path in git_index_paths(root, predecessor):
            changed.append(path)
            if not any(
                rule["repository"] == repository and fnmatch.fnmatchcase(path, rule["pattern"])
                for rule in allowed
            ):
                raise GateViolation(
                    "AUTH-SCOPE-OUTSIDE-ALLOWLIST", f"path is outside allowlist: {path}"
                )
            is_protected = any(
                fnmatch.fnmatchcase(path, pattern) for pattern in protected.get(repository, [])
            )
            if is_protected and manifest["task_type"] != "authority_change":
                raise GateViolation(
                    "AUTH-SCOPE-PROTECTED", f"ordinary task changed protected path: {path}"
                )
        receipts.append(
            make_typed_receipt(
                "change_scope",
                authority_lock_hash=authority_lock_hash,
                decision="allow",
                reason_codes=[],
                task_id=str(manifest["task_id"]),
                repository=repository,
                base_commit=predecessor,
                staged_tree_hash=index_tree,
                index_tree_hash=index_tree,
                changed_paths_hash=canonical_hash(changed),
                worktree_policy_hash=canonical_hash(
                    [rule for rule in allowed if rule["repository"] == repository]
                ),
            )
        )
    return receipts


def check_change_scope(
    *,
    manifest_path: Path,
    protected_paths_path: Path,
    repository_roots: Mapping[str, Path],
    authority_lock_hash: str = "sha256:" + "0" * 64,
) -> dict[str, Any]:
    """Compatibility wrapper for a single-repository scope receipt.

    Cross-repository tasks must retain one real Git tree OID per repository and
    therefore use :func:`check_change_scopes`; synthesizing an OID from a
    SHA-256 digest is forbidden.
    """

    receipts = check_change_scopes(
        manifest_path=manifest_path,
        protected_paths_path=protected_paths_path,
        repository_roots=repository_roots,
        authority_lock_hash=authority_lock_hash,
    )
    if len(receipts) != 1:
        raise GateViolation(
            "AUTH-SCOPE-MULTI-REPOSITORY",
            "cross-repository task requires one ChangeScopeReceipt per repository",
        )
    return receipts[0]
