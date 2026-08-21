# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportMissingModuleSource=false
"""Shared primitives for authority verification.

The bootstrap tools intentionally use only the Python standard library plus
PyYAML, which is a declared project dependency.  They must be runnable before
the future kernel package or generated Registry exists.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, cast

import yaml

from .errors import GateViolation

SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
PLACEHOLDER_RE = re.compile(r"(?im)(?:^|\b)(?:TBD|TODO|_example)(?:\b|$)")


def load_mapping(path: Path) -> dict[str, Any]:
    """Load a JSON/YAML object without accepting an empty or scalar document."""

    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise GateViolation("AUTH-FILE-UNREADABLE", f"cannot read {path}: {exc}") from exc
    try:
        if path.suffix == ".json":
            value = json.loads(raw)
        else:
            value = yaml.safe_load(raw)
    except (json.JSONDecodeError, yaml.YAMLError, UnicodeDecodeError) as exc:
        raise GateViolation("AUTH-DOCUMENT-INVALID", f"cannot parse {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise GateViolation("AUTH-DOCUMENT-NOT-OBJECT", f"{path} must contain an object")
    return cast(dict[str, Any], value)


def require_closed(
    value: Mapping[str, Any],
    *,
    required: Iterable[str],
    optional: Iterable[str] = (),
    where: str,
) -> None:
    """Enforce a closed object and its required keys."""

    required_set = set(required)
    allowed = required_set | set(optional)
    missing = sorted(required_set - value.keys())
    extra = sorted(value.keys() - allowed)
    if missing:
        raise GateViolation("AUTH-SCHEMA-MISSING", f"{where} missing fields: {missing}")
    if extra:
        raise GateViolation("AUTH-SCHEMA-EXTRA", f"{where} has unknown fields: {extra}")


def require_non_empty_string(value: Any, *, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GateViolation("AUTH-SCHEMA-TYPE", f"{where} must be a non-empty string")
    return value


def require_sha256(value: Any, *, where: str) -> str:
    text = require_non_empty_string(value, where=where)
    if not SHA256_RE.fullmatch(text):
        raise GateViolation("AUTH-SCHEMA-HASH", f"{where} must be sha256:<64 lowercase hex>")
    return text


def require_commit(value: Any, *, where: str) -> str:
    text = require_non_empty_string(value, where=where)
    if not COMMIT_RE.fullmatch(text):
        raise GateViolation("AUTH-SCHEMA-COMMIT", f"{where} must be a 40-char commit OID")
    return text


def require_git_object_oid(root: Path, value: Any, *, object_type: str, where: str) -> str:
    """Require an OID to resolve to an actual object of the requested Git type."""

    oid = require_commit(value, where=where)
    if object_type not in {"blob", "tree", "commit"}:
        raise AssertionError(f"unsupported Git object type: {object_type}")
    try:
        actual_type = subprocess.run(
            ["git", "cat-file", "-t", oid],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise GateViolation("AUTH-GIT-OID-MISSING", f"{where} is not a Git object") from exc
    if actual_type != object_type:
        raise GateViolation(
            "AUTH-GIT-OID-TYPE", f"{where} is {actual_type}, expected {object_type}"
        )
    return oid


def require_list(value: Any, *, where: str, non_empty: bool = False) -> list[Any]:
    if not isinstance(value, list) or (non_empty and not value):
        suffix = " non-empty" if non_empty else ""
        raise GateViolation("AUTH-SCHEMA-TYPE", f"{where} must be a{suffix} array")
    return cast(list[Any], value)


def sha256_bytes(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def sha256_file(path: Path) -> str:
    try:
        return sha256_bytes(path.read_bytes())
    except OSError as exc:
        raise GateViolation("AUTH-FILE-UNREADABLE", f"cannot hash {path}: {exc}") from exc


def canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256_bytes(payload)


def validate_relative_path(value: Any, *, where: str) -> str:
    text = require_non_empty_string(value, where=where)
    if "\\" in text or "\x00" in text or os.path.isabs(text):
        raise GateViolation("AUTH-PATH-INVALID", f"{where} is not a portable relative path")
    path = PurePosixPath(text)
    if text in {".", ".."} or any(part in {"", ".", ".."} for part in path.parts):
        raise GateViolation("AUTH-PATH-ESCAPE", f"{where} escapes or aliases the repository")
    if path.as_posix() != text:
        raise GateViolation("AUTH-PATH-ALIAS", f"{where} is not in canonical POSIX form")
    return text


def validate_glob(value: Any, *, where: str) -> str:
    pattern = validate_relative_path(value, where=where)
    if any(token in pattern for token in ("$", "{", "}")):
        raise GateViolation("AUTH-GLOB-UNRESOLVED", f"{where} contains a variable/template")
    if pattern in {"*", "**", "**/*"} or pattern.startswith("*/"):
        raise GateViolation("AUTH-GLOB-BROAD", f"{where} grants repository-root scope")
    first = PurePosixPath(pattern).parts[0]
    if any(char in first for char in "*?["):
        raise GateViolation("AUTH-GLOB-BROAD", f"{where} needs a concrete top-level component")
    return pattern


def contained_path(root: Path, relative: str, *, allow_missing: bool = True) -> Path:
    """Resolve a repository path while rejecting symlink traversal."""

    relative = validate_relative_path(relative, where="path")
    root_resolved = root.resolve(strict=True)
    current = root_resolved
    for part in PurePosixPath(relative).parts:
        current = current / part
        if current.is_symlink():
            raise GateViolation("AUTH-PATH-SYMLINK", f"symlink component is forbidden: {current}")
        if not current.exists():
            if not allow_missing:
                raise GateViolation("AUTH-PATH-MISSING", f"path does not exist: {current}")
            continue
        try:
            current.resolve(strict=True).relative_to(root_resolved)
        except (OSError, ValueError) as exc:
            raise GateViolation("AUTH-PATH-ESCAPE", f"path escapes repository: {current}") from exc
    return current


def git_output(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        stderr = getattr(exc, "stderr", "")
        raise GateViolation(
            "AUTH-GIT-INSPECTION", f"git {' '.join(args)} failed: {stderr}"
        ) from exc
    return result.stdout.strip()


def git_bytes(root: Path, revision: str, relative_path: str) -> bytes:
    """Read an exact blob from a committed Git tree.

    Authority verification must never fall back to the checkout because the
    checkout and index are mutable inputs.  ``git show`` also rejects a path
    that was not present in the bound revision.
    """

    require_commit(revision, where="git revision")
    relative = validate_relative_path(relative_path, where="git blob path")
    try:
        result = subprocess.run(
            ["git", "show", f"{revision}:{relative}"],
            cwd=root,
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        stderr = getattr(exc, "stderr", b"")
        detail = stderr.decode("utf-8", errors="replace") if isinstance(stderr, bytes) else stderr
        raise GateViolation(
            "AUTH-GIT-BLOB-MISSING",
            f"cannot read {revision}:{relative}: {detail}",
        ) from exc
    return result.stdout


def load_mapping_bytes(raw: bytes, *, where: str, suffix: str = ".yaml") -> dict[str, Any]:
    """Parse a mapping already obtained from an immutable evidence source."""

    try:
        value = json.loads(raw) if suffix == ".json" else yaml.safe_load(raw)
    except (json.JSONDecodeError, yaml.YAMLError, UnicodeDecodeError) as exc:
        raise GateViolation("AUTH-DOCUMENT-INVALID", f"cannot parse {where}: {exc}") from exc
    if not isinstance(value, dict):
        raise GateViolation("AUTH-DOCUMENT-NOT-OBJECT", f"{where} must contain an object")
    return cast(dict[str, Any], value)


def git_index_paths(root: Path, predecessor_commit: str) -> list[str]:
    """Derive the candidate scope from Git's index, never caller testimony."""

    require_commit(predecessor_commit, where="predecessor_commit")
    output = git_output(root, "diff", "--cached", "--name-only", "-z", predecessor_commit, "--")
    if not output:
        return []
    paths = output.split("\0")
    return [validate_relative_path(path, where="staged path") for path in paths if path]


def git_index_bytes(root: Path, relative_path: str) -> bytes:
    """Read a candidate blob from the index, excluding worktree-only bytes."""

    relative = validate_relative_path(relative_path, where="index blob path")
    try:
        result = subprocess.run(
            ["git", "show", f":{relative}"], cwd=root, check=True, capture_output=True
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise GateViolation("AUTH-GIT-INDEX-BLOB", f"cannot read staged blob: {relative}") from exc
    return result.stdout


def write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    """Write a receipt without exposing a partially written JSON document."""

    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def utc_now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def make_receipt(
    *,
    receipt_type: str,
    task_id: str | None,
    authority_lock_hash: str,
    decision: str,
    reason_codes: Sequence[str],
    evidence: Sequence[Mapping[str, str]],
) -> dict[str, Any]:
    body = {
        "schema_version": "1.0.0",
        "receipt_type": receipt_type,
        "task_id": task_id,
        "authority_lock_hash": authority_lock_hash,
        "decision": decision,
        "reason_codes": list(reason_codes),
        "evidence": [dict(item) for item in evidence],
    }
    return {
        **body,
        "receipt_id": canonical_hash(body),
        "produced_at": utc_now(),
    }
