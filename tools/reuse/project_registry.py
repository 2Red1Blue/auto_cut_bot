"""projects.json loader: closed registry of external producers and adapters.

The registry pins repo URL, commit, license path and adapter version per
producer. A spec that does not match the registry exactly is refused with
SOURCE_MISMATCH — the registry is the single source of execution identity.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools.reuse.models import ErrorCode, ExperimentError, load_json_strict

REGISTRY_SCHEMA = "ReuseProjects/v1"

_REGISTRY_KEYS = {"schema", "projects"}
_PROJECT_KEYS = {
    "project_id",
    "role",
    "repo_url",
    "local_dir",
    "pinned_commit",
    "license_path",
    "adapter_version",
    "adapter_module",
    "status",
}
_ROLES = ("reference", "builtin")
_STATUSES = ("registered", "in_evaluation", "evaluated", "not_feasible")


@dataclass(frozen=True)
class ProjectEntry:
    project_id: str
    role: str
    repo_url: str | None
    local_dir: str | None
    pinned_commit: str | None
    license_path: str | None
    adapter_version: str
    adapter_module: str
    status: str

    def resolve_repo_dir(self, reference_root: Path) -> Path | None:
        """Resolve the local checkout for reference projects, or None for builtin."""
        if self.role == "builtin" or not self.local_dir:
            return None
        return (reference_root / self.local_dir).resolve()

    def verify(self, spec_producer_commit: str, spec_adapter_version: str,
               reference_root: Path) -> None:
        """Exact-match the spec against the registry and (for references) the repo HEAD."""
        if spec_producer_commit != self.pinned_commit:
            raise ExperimentError(
                ErrorCode.SOURCE_MISMATCH,
                f"producer_commit {spec_producer_commit!r} != registry pinned "
                f"{self.pinned_commit!r} for {self.project_id}",
            )
        if spec_adapter_version != self.adapter_version:
            raise ExperimentError(
                ErrorCode.SOURCE_MISMATCH,
                f"adapter_version {spec_adapter_version!r} != registry "
                f"{self.adapter_version!r} for {self.project_id}",
            )
        repo_dir = self.resolve_repo_dir(reference_root)
        if repo_dir is None:
            return
        if not repo_dir.is_dir():
            raise ExperimentError(
                ErrorCode.SOURCE_MISMATCH,
                f"reference repo not found at {repo_dir}; clone it or pass --reference-root",
            )
        head = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=False,
        )
        actual = head.stdout.strip()
        if head.returncode != 0 or actual != self.pinned_commit:
            raise ExperimentError(
                ErrorCode.SOURCE_MISMATCH,
                f"repo HEAD {actual or '<unavailable>'} != pinned "
                f"{self.pinned_commit} for {self.project_id}",
            )
        dirty = subprocess.run(
            ["git", "-C", str(repo_dir), "status", "--porcelain"],
            capture_output=True, text=True, check=False,
        )
        if dirty.returncode != 0 or dirty.stdout.strip():
            raise ExperimentError(
                ErrorCode.SOURCE_MISMATCH,
                f"reference repo {self.project_id} has uncommitted changes; "
                "the pinned commit is not the code that would actually run",
            )


def _parse_project(obj: Any, index: int) -> ProjectEntry:
    what = f"projects[{index}]"
    data = obj if isinstance(obj, dict) else {}
    unknown = sorted(set(data) - _PROJECT_KEYS)
    if unknown:
        raise ExperimentError(ErrorCode.REGISTRY_INVALID, f"{what}: unknown keys {unknown}")
    for key in ("project_id", "role", "adapter_version", "adapter_module", "status"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise ExperimentError(ErrorCode.REGISTRY_INVALID, f"{what}.{key}: required string")
    role = data["role"]
    if role not in _ROLES:
        raise ExperimentError(ErrorCode.REGISTRY_INVALID, f"{what}.role: must be one of {_ROLES}")
    status = data["status"]
    if status not in _STATUSES:
        raise ExperimentError(ErrorCode.REGISTRY_INVALID, f"{what}.status: must be one of {_STATUSES}")
    for key in ("repo_url", "local_dir", "pinned_commit", "license_path"):
        if data.get(key) is not None and not isinstance(data[key], str):
            raise ExperimentError(ErrorCode.REGISTRY_INVALID, f"{what}.{key}: string or null")
    if role == "reference":
        for key in ("repo_url", "local_dir", "pinned_commit"):
            if data.get(key) is None:
                raise ExperimentError(ErrorCode.REGISTRY_INVALID, f"{what}.{key}: required for reference")
    else:
        if data.get("pinned_commit") is None:
            raise ExperimentError(ErrorCode.REGISTRY_INVALID, f"{what}.pinned_commit: required for builtin")
    return ProjectEntry(
        project_id=data["project_id"],
        role=role,
        repo_url=data.get("repo_url"),
        local_dir=data.get("local_dir"),
        pinned_commit=data.get("pinned_commit"),
        license_path=data.get("license_path"),
        adapter_version=data["adapter_version"],
        adapter_module=data["adapter_module"],
        status=status,
    )


@dataclass(frozen=True)
class ProjectRegistry:
    projects: dict[str, ProjectEntry]

    def get(self, producer_id: str) -> ProjectEntry | None:
        return self.projects.get(producer_id)

    def require(self, producer_id: str) -> ProjectEntry:
        entry = self.projects.get(producer_id)
        if entry is None:
            raise ExperimentError(
                ErrorCode.SOURCE_MISMATCH, f"producer {producer_id!r} not in registry"
            )
        return entry


def load_registry(path: Path) -> ProjectRegistry:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ExperimentError(ErrorCode.REGISTRY_INVALID, f"registry unreadable: {exc}") from exc
    obj = load_json_strict(text, context="projects.json")
    if not isinstance(obj, dict) or obj.get("schema") != REGISTRY_SCHEMA:
        raise ExperimentError(
            ErrorCode.REGISTRY_INVALID, f"projects.json schema must be {REGISTRY_SCHEMA}"
        )
    unknown = sorted(set(obj) - _REGISTRY_KEYS)
    if unknown:
        raise ExperimentError(ErrorCode.REGISTRY_INVALID, f"projects.json: unknown keys {unknown}")
    raw_projects = obj.get("projects")
    if not isinstance(raw_projects, list):
        raise ExperimentError(ErrorCode.REGISTRY_INVALID, "projects.json.projects: required list")
    projects: dict[str, ProjectEntry] = {}
    for i, raw in enumerate(raw_projects):
        entry = _parse_project(raw, i)
        if entry.project_id in projects:
            raise ExperimentError(
                ErrorCode.REGISTRY_INVALID, f"projects.json: duplicate project_id {entry.project_id!r}"
            )
        projects[entry.project_id] = entry
    return ProjectRegistry(projects=projects)


def find_reference_root(explicit: Path | None, repo_root: Path) -> Path:
    """Resolve the reference-projects directory: flag > env > upward search."""
    if explicit is not None:
        resolved = explicit.resolve()
        if not resolved.is_dir():
            raise ExperimentError(ErrorCode.SOURCE_MISMATCH, f"reference root missing: {resolved}")
        return resolved
    import os

    env = os.environ.get("AC_REUSE_REFERENCE_ROOT")
    if env:
        resolved = Path(env).resolve()
        if resolved.is_dir():
            return resolved
    for candidate in [repo_root, *repo_root.parents]:
        maybe = candidate / "reference-projects"
        if maybe.is_dir():
            return maybe
    raise ExperimentError(
        ErrorCode.SOURCE_MISMATCH,
        "reference-projects/ not found; pass --reference-root or set AC_REUSE_REFERENCE_ROOT",
    )
