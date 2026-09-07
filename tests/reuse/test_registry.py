"""projects.json registry loader tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.reuse.models import ErrorCode, ExperimentError
from tools.reuse.project_registry import find_reference_root, load_registry


def _write(path: Path, doc: object) -> Path:
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _valid_project() -> dict[str, object]:
    return {
        "project_id": "X",
        "role": "reference",
        "repo_url": "https://example.com/x",
        "local_dir": "x",
        "pinned_commit": "a" * 40,
        "license_path": "LICENSE",
        "adapter_version": "x-1",
        "adapter_module": "tools.reuse.adapters.PendingAdapter",
        "status": "registered",
    }


class TestLoader:
    def test_rejects_unknown_top_level_key(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "p.json", {"schema": "ReuseProjects/v1", "projects": [], "extra": 1})
        with pytest.raises(ExperimentError) as exc:
            load_registry(path)
        assert exc.value.code == ErrorCode.REGISTRY_INVALID

    def test_rejects_wrong_schema(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "p.json", {"schema": "Other/v1", "projects": []})
        with pytest.raises(ExperimentError):
            load_registry(path)

    def test_rejects_duplicate_project_id(self, tmp_path: Path) -> None:
        project = _valid_project()
        path = _write(tmp_path / "p.json", {"schema": "ReuseProjects/v1", "projects": [project, project]})
        with pytest.raises(ExperimentError):
            load_registry(path)

    def test_reference_requires_commit_and_repo(self, tmp_path: Path) -> None:
        project = _valid_project()
        project["pinned_commit"] = None
        path = _write(tmp_path / "p.json", {"schema": "ReuseProjects/v1", "projects": [project]})
        with pytest.raises(ExperimentError):
            load_registry(path)

    def test_real_registry_loads(self, repo_root: Path) -> None:
        registry = load_registry(repo_root / "tools" / "reuse" / "projects.json")
        assert registry.get("fake") is not None
        assert registry.get("VideoAgent") is not None
        assert registry.get("nonexistent") is None
        # every reference entry pins a 40-char commit
        for entry in registry.projects.values():
            if entry.role == "reference":
                assert len(entry.pinned_commit) == 40  # type: ignore[arg-type]


class TestFindReferenceRoot:
    def test_explicit_flag(self, tmp_path: Path) -> None:
        root = tmp_path / "refs"
        root.mkdir()
        assert find_reference_root(root, tmp_path) == root.resolve()

    def test_missing_root_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ExperimentError) as exc:
            find_reference_root(tmp_path / "nope", tmp_path)
        assert exc.value.code == ErrorCode.SOURCE_MISMATCH

    def test_upward_search_finds_repo_root(self, repo_root: Path) -> None:
        # the real repo layout has reference-projects/ somewhere above the worktree
        root = find_reference_root(None, repo_root)
        assert root.name == "reference-projects"
