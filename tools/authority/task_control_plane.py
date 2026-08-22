"""Global, hash-bound Trellis task control-plane helpers.

The shared ``.trellis/tasks`` root is deliberately not a business repository.
It can freeze planning inputs, but it can never grant a business write path.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from .common import (
    canonical_hash,
    contained_path,
    require_closed,
    require_list,
    require_non_empty_string,
    require_sha256,
    sha256_bytes,
    sha256_file,
    validate_relative_path,
)
from .errors import GateViolation

LOCK_FILENAME = "task-control-plane.lock.json"


def _validate_context_content(raw: bytes, *, where: str) -> None:
    """Reject malformed or placeholder task documents before they reach an agent."""

    from .common import PLACEHOLDER_RE

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GateViolation("AUTH-TASK-CONTEXT-ENCODING", f"{where} must be UTF-8") from exc
    if PLACEHOLDER_RE.search(text):
        raise GateViolation("AUTH-TASK-CONTEXT-PLACEHOLDER", f"{where} contains a placeholder")


def _root_is_inside_git_worktree(root: Path) -> bool:
    """Return whether *root* is contained in a Git checkout.

    The result is intentionally conservative: inability to establish a clean
    boundary is a denial, not permission to treat a business checkout as the
    global task root.
    """

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return False
    try:
        root.relative_to(Path(result.stdout.strip()).resolve(strict=True))
    except (OSError, ValueError):
        return False
    return True


def resolve_trellis_tasks_root(root: Path, *, repository_roots: Mapping[str, Path]) -> Path:
    """Require a caller-supplied non-repository root outside business roots."""

    if not root.is_absolute() or root.is_symlink():
        raise GateViolation("AUTH-TASK-CONTROL-ROOT", "trellis tasks root must be absolute, real")
    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise GateViolation("AUTH-TASK-CONTROL-ROOT", "trellis tasks root is unreadable") from exc
    if not resolved.is_dir() or _root_is_inside_git_worktree(resolved):
        raise GateViolation(
            "AUTH-TASK-CONTROL-ROOT", "trellis tasks root must not be a business Git checkout"
        )
    for repository_root in repository_roots.values():
        try:
            resolved.relative_to(repository_root.resolve(strict=True))
        except ValueError:
            continue
        except OSError as exc:
            raise GateViolation("AUTH-TASK-CONTROL-ROOT", "repository root is unreadable") from exc
        raise GateViolation(
            "AUTH-TASK-CONTROL-ROOT", "trellis tasks root cannot be inside a business checkout"
        )
    return resolved


def _control_plane_binding(manifest: Mapping[str, Any]) -> Mapping[str, Any] | None:
    value = manifest.get("task_control_plane")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise GateViolation("AUTH-TASK-CONTROL-BINDING", "task_control_plane must be an object")
    binding = cast(dict[str, Any], value)
    require_closed(
        binding,
        required=("source", "task_directory", "lock_path"),
        where="task_control_plane",
    )
    if binding["source"] != "trellis_tasks":
        raise GateViolation("AUTH-TASK-CONTROL-BINDING", "unknown task control-plane source")
    task_directory = validate_relative_path(binding["task_directory"], where="task_directory")
    lock_path = validate_relative_path(binding["lock_path"], where="lock_path")
    if lock_path != f"{task_directory}/{LOCK_FILENAME}":
        raise GateViolation("AUTH-TASK-CONTROL-LOCK-PATH", "lock path must be beside task manifest")
    return binding


def _context_source(item: Mapping[str, Any], *, where: str) -> str:
    source = item.get("source", "repository")
    if source not in {"repository", "trellis_tasks"}:
        raise GateViolation("AUTH-TASK-CONTEXT-SOURCE", f"{where} has unknown context source")
    return source


def _ensure_control_context_path(*, relative: str, task_directory: str, where: str) -> None:
    if not relative.startswith(f"{task_directory}/"):
        raise GateViolation(
            "AUTH-TASK-CONTROL-PATH", f"{where} must remain under its declared task directory"
        )


def resolve_context_file(
    item: Mapping[str, Any],
    *,
    repository_roots: Mapping[str, Path],
    trellis_tasks_root: Path | None,
    task_directory: str | None,
    where: str,
) -> tuple[bytes, dict[str, Any]]:
    """Read one declared context file and return normalized immutable evidence."""

    source = _context_source(item, where=where)
    base_fields = {"source", "path", "sha256", "byte_length"}
    if source == "repository":
        require_closed(
            item,
            required=("repository", "path", "sha256", "byte_length"),
            optional=("source",),
            where=where,
        )
        repository = require_non_empty_string(item["repository"], where=f"{where}.repository")
        if repository not in repository_roots:
            raise GateViolation("AUTH-TASK-CONTEXT-REPOSITORY", f"{where} unknown repository")
        root = repository_roots[repository].resolve(strict=True)
        identity: dict[str, Any] = {"source": source, "repository": repository}
    else:
        require_closed(item, required=base_fields, where=where)
        if trellis_tasks_root is None or task_directory is None:
            raise GateViolation(
                "AUTH-TASK-CONTROL-ROOT", "global task context needs an explicit trellis tasks root"
            )
        root = trellis_tasks_root
        identity = {"source": source}
    relative = validate_relative_path(item["path"], where=f"{where}.path")
    if source == "trellis_tasks":
        if task_directory is None:  # guarded above; narrows the type for static checking.
            raise GateViolation(
                "AUTH-TASK-CONTROL-ROOT", "global task context lacks task directory"
            )
        _ensure_control_context_path(relative=relative, task_directory=task_directory, where=where)
    expected = require_sha256(item["sha256"], where=f"{where}.sha256")
    length = item["byte_length"]
    if isinstance(length, bool) or not isinstance(length, int) or length < 1:
        raise GateViolation("AUTH-TASK-CONTEXT-BYTES", f"{where}.byte_length must be positive")
    path = contained_path(root, relative, allow_missing=False)
    raw = path.read_bytes()
    if len(raw) != length or sha256_bytes(raw) != expected:
        raise GateViolation("AUTH-TASK-CONTEXT-HASH", f"{where} bytes/hash mismatch")
    _validate_context_content(raw, where=where)
    return raw, {**identity, "path": relative, "sha256": expected, "byte_length": length}


def control_plane_contexts(manifest: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    contexts: list[tuple[str, Mapping[str, Any]]] = []
    for index, artifact in enumerate(
        require_list(manifest.get("planning_artifacts"), where="planning_artifacts")
    ):
        if not isinstance(artifact, dict):
            raise GateViolation("AUTH-TASK-PLANNING", f"planning_artifacts[{index}] invalid")
        artifact_mapping = cast(dict[str, Any], artifact)
        context: dict[str, Any] = {
            key: value for key, value in artifact_mapping.items() if key != "kind"
        }
        contexts.append((f"planning_artifacts[{index}]", context))
    for field in ("implementation_context", "check_context"):
        declared_context = manifest.get(field)
        if not isinstance(declared_context, dict):
            raise GateViolation("AUTH-TASK-CONTEXT", f"{field} must be an object")
        contexts.append((field, cast(dict[str, Any], declared_context)))
    return contexts


def _lock_payload(
    *,
    manifest_path: Path,
    manifest: Mapping[str, Any],
    trellis_tasks_root: Path,
    repository_roots: Mapping[str, Path],
) -> dict[str, Any]:
    binding = _control_plane_binding(manifest)
    if binding is None:
        raise GateViolation("AUTH-TASK-CONTROL-BINDING", "global control-plane binding is required")
    task_directory = validate_relative_path(binding["task_directory"], where="task_directory")
    try:
        relative_manifest = (
            manifest_path.resolve(strict=True).relative_to(trellis_tasks_root).as_posix()
        )
    except (OSError, ValueError) as exc:
        raise GateViolation(
            "AUTH-TASK-CONTROL-MANIFEST", "manifest must live under trellis tasks root"
        ) from exc
    _ensure_control_context_path(
        relative=relative_manifest, task_directory=task_directory, where="manifest"
    )
    snapshot: list[dict[str, Any]] = []
    for where, context in control_plane_contexts(manifest):
        _raw, evidence = resolve_context_file(
            context,
            repository_roots=repository_roots,
            trellis_tasks_root=trellis_tasks_root,
            task_directory=task_directory,
            where=where,
        )
        if evidence["source"] != "trellis_tasks":
            continue
        snapshot.append({"where": where, **evidence})
    if not snapshot:
        raise GateViolation("AUTH-TASK-CONTROL-CONTEXT", "global control plane needs task contexts")
    return {
        "schema_version": "1.0.0",
        "task_id": require_non_empty_string(manifest["task_id"], where="task_id"),
        "manifest_path": relative_manifest,
        "manifest_sha256": sha256_file(manifest_path),
        "task_directory": task_directory,
        "context_snapshot": snapshot,
    }


def freeze_task_control_plane(
    *,
    manifest_path: Path,
    manifest: Mapping[str, Any],
    trellis_tasks_root: Path,
    repository_roots: Mapping[str, Path],
) -> dict[str, Any]:
    """Build the exact closed lock payload; caller persists it atomically."""

    body = _lock_payload(
        manifest_path=manifest_path,
        manifest=manifest,
        trellis_tasks_root=trellis_tasks_root,
        repository_roots=repository_roots,
    )
    return {**body, "lock_hash": canonical_hash(body)}


def replay_task_control_plane(
    *,
    manifest_path: Path,
    manifest: Mapping[str, Any],
    trellis_tasks_root: Path | None,
    repository_roots: Mapping[str, Path],
    control_plane_lock_path: Path | None,
) -> str | None:
    """Reread every frozen global byte and return the stable lock hash.

    Repository-only manifests preserve their prior behavior. A manifest with a
    global binding is fail-closed: both the explicit root and lock are needed.
    """

    binding = _control_plane_binding(manifest)
    has_global = any(
        _context_source(context, where=where) == "trellis_tasks"
        for where, context in control_plane_contexts(manifest)
    )
    if not has_global and binding is None:
        return None
    if binding is None or trellis_tasks_root is None or control_plane_lock_path is None:
        raise GateViolation("AUTH-TASK-CONTROL-LOCK", "global task control-plane lock is required")
    root = resolve_trellis_tasks_root(trellis_tasks_root, repository_roots=repository_roots)
    expected_lock_path = contained_path(root, str(binding["lock_path"]), allow_missing=False)
    try:
        supplied = control_plane_lock_path.resolve(strict=True)
    except OSError as exc:
        raise GateViolation(
            "AUTH-TASK-CONTROL-LOCK", "task control-plane lock is unreadable"
        ) from exc
    if supplied != expected_lock_path:
        raise GateViolation("AUTH-TASK-CONTROL-LOCK", "caller lock path is not task-bound")
    try:
        import json

        lock = json.loads(supplied.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise GateViolation("AUTH-TASK-CONTROL-LOCK", "task control-plane lock is invalid") from exc
    if not isinstance(lock, dict):
        raise GateViolation("AUTH-TASK-CONTROL-LOCK", "task control-plane lock must be an object")
    lock_mapping = cast(dict[str, Any], lock)
    expected = freeze_task_control_plane(
        manifest_path=manifest_path,
        manifest=manifest,
        trellis_tasks_root=root,
        repository_roots=repository_roots,
    )
    require_closed(
        lock_mapping,
        required=(
            "schema_version",
            "task_id",
            "manifest_path",
            "manifest_sha256",
            "task_directory",
            "context_snapshot",
            "lock_hash",
        ),
        where="task control-plane lock",
    )
    if lock_mapping != expected:
        raise GateViolation(
            "AUTH-TASK-CONTROL-DRIFT", "task control-plane lock or frozen bytes changed"
        )
    return require_sha256(lock_mapping["lock_hash"], where="task control-plane lock hash")
