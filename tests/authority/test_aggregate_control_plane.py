from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from authority.aggregate_gate import context_snapshot, verify_push
from authority.cli import _parser
from authority.common import canonical_hash, sha256_file
from authority.errors import GateViolation
from authority.receipts import make_typed_receipt
from authority.task_control_plane import freeze_task_control_plane


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


def _global_context(path: Path, *, task_directory: str) -> dict[str, Any]:
    return {
        "source": "trellis_tasks",
        "path": f"{task_directory}/{path.name}",
        "sha256": sha256_file(path),
        "byte_length": path.stat().st_size,
    }


def _frozen_global_task(tmp_path: Path) -> tuple[Path, Path, dict[str, Any], Path]:
    repository = tmp_path / "business-repository"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    (repository / "tracked.txt").write_text("business\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    _git(
        repository,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@test",
        "commit",
        "-m",
        "base",
    )

    root = tmp_path / "global-trellis-tasks"
    task_directory = "08-21-task"
    task = root / task_directory
    task.mkdir(parents=True)
    filenames = ("prd.md", "design.md", "implement.md", "implement.jsonl", "check.jsonl")
    for name in filenames:
        (task / name).write_text(f"complete {name}\n", encoding="utf-8")
    contexts = {
        name: _global_context(task / name, task_directory=task_directory) for name in filenames
    }
    manifest: dict[str, Any] = {
        "task_id": "global-context-task",
        "task_control_plane": {
            "source": "trellis_tasks",
            "task_directory": task_directory,
            "lock_path": f"{task_directory}/task-control-plane.lock.json",
        },
        "planning_artifacts": [
            {"kind": kind, **contexts[name]}
            for kind, name in (
                ("prd", "prd.md"),
                ("design", "design.md"),
                ("implement", "implement.md"),
                ("implement_context", "implement.jsonl"),
                ("check_context", "check.jsonl"),
            )
        ],
        "implementation_context": contexts["implement.jsonl"],
        "check_context": contexts["check.jsonl"],
    }
    manifest_path = task / "task-manifest.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    frozen = freeze_task_control_plane(
        manifest_path=manifest_path,
        manifest=manifest,
        trellis_tasks_root=root,
        repository_roots={"business": repository},
    )
    lock_path = root / manifest["task_control_plane"]["lock_path"]
    lock_path.write_text(json.dumps(frozen, sort_keys=True), encoding="utf-8")
    return repository, root, manifest, manifest_path


def test_aggregate_snapshot_binds_and_rereads_global_task_context(tmp_path: Path) -> None:
    repository, root, manifest, manifest_path = _frozen_global_task(tmp_path)
    roots = {"business": repository}
    snapshot = context_snapshot(
        manifest,
        roots,
        manifest_path=manifest_path,
        control_plane_roots={"trellis_tasks": root},
    )
    assert snapshot.startswith("sha256:")

    (root / "08-21-task/prd.md").write_text("mutated planning input\n", encoding="utf-8")
    with pytest.raises(GateViolation, match="AUTH-TASK-CONTEXT-HASH"):
        context_snapshot(
            manifest,
            roots,
            manifest_path=manifest_path,
            control_plane_roots={"trellis_tasks": root},
        )


def test_aggregate_snapshot_denies_global_context_without_explicit_root(tmp_path: Path) -> None:
    repository, _root, manifest, manifest_path = _frozen_global_task(tmp_path)
    with pytest.raises(GateViolation, match="AUTH-TASK-CONTROL-ROOT"):
        context_snapshot(
            manifest,
            {"business": repository},
            manifest_path=manifest_path,
        )


def test_push_rejects_resealed_global_control_plane_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A post-review rewrite plus a new lock cannot reuse the old allow."""

    repository, root, manifest, manifest_path = _frozen_global_task(tmp_path)
    roots = {"business": repository}
    old_snapshot = context_snapshot(
        manifest, roots, manifest_path=manifest_path, control_plane_roots={"trellis_tasks": root}
    )
    task_directory = manifest["task_control_plane"]["task_directory"]
    prd = root / task_directory / "prd.md"
    prd.write_text("replanned prd.md\n", encoding="utf-8")
    for artifact in manifest["planning_artifacts"]:
        if artifact["kind"] == "prd":
            artifact.update(_global_context(prd, task_directory=task_directory))
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    new_lock = freeze_task_control_plane(
        manifest_path=manifest_path,
        manifest=manifest,
        trellis_tasks_root=root,
        repository_roots=roots,
    )
    (root / manifest["task_control_plane"]["lock_path"]).write_text(
        json.dumps(new_lock, sort_keys=True), encoding="utf-8"
    )
    authority_hash = "sha256:" + "1" * 64
    authority_lock_path = tmp_path / "authority-lock.yaml"
    authority_lock_path.write_text(
        yaml.safe_dump({"bundle_hash": authority_hash}), encoding="utf-8"
    )
    receipt = make_typed_receipt(
        "change_verification",
        authority_lock_hash=authority_hash,
        decision="allow",
        reason_codes=[],
        task_id="global-context-task",
        base_commits_hash="sha256:" + "2" * 64,
        repository_tree_oids_hash="sha256:" + "3" * 64,
        control_plane_context_hash=old_snapshot,
        receipt_closure_hash=canonical_hash([old_snapshot]),
    )
    monkeypatch.setattr("authority.aggregate_gate.verify_authority_lock_data", lambda *_a, **_k: {})
    monkeypatch.setattr("authority.aggregate_gate.admit_task", lambda **_k: [])
    with pytest.raises(GateViolation, match="AUTH-PUSH-CONTEXT-DRIFT"):
        verify_push(
            root=repository,
            repository="business",
            task_id="global-context-task",
            authority_lock_path=authority_lock_path,
            repository_roots=roots,
            change_bundle={"receipt": receipt, "repository_trees": []},
            candidate_commit="0" * 40,
            remote_attestation_path=tmp_path / "remote.json",
            remote_policy_path=tmp_path / "policy.yaml",
            task_manifest_path=manifest_path,
            model_policy_path=tmp_path / "model.yaml",
            protected_paths_path=tmp_path / "protected.yaml",
            control_plane_roots={"trellis_tasks": root},
        )


def test_cli_exposes_explicit_control_plane_root_for_change_and_push() -> None:
    parser = _parser()
    change = parser.parse_args(
        [
            "verify-change",
            "--manifest",
            "task.yaml",
            "--lock",
            "lock.yaml",
            "--model-policy",
            "model.yaml",
            "--protected-paths",
            "protected.yaml",
            "--repository-root",
            "business=/repo",
            "--control-plane-root",
            "trellis_tasks=/tasks",
            "--reuse-ledger",
            "governance/reuse.yaml",
        ]
    )
    assert change.control_plane_root == ["trellis_tasks=/tasks"]
    push = parser.parse_args(
        [
            "verify-push",
            "--repository",
            "business",
            "--repository-root",
            "business=/repo",
            "--task-id",
            "task",
            "--manifest",
            "task.yaml",
            "--lock",
            "lock.yaml",
            "--model-policy",
            "model.yaml",
            "--protected-paths",
            "protected.yaml",
            "--control-plane-root",
            "trellis_tasks=/tasks",
            "--change-bundle",
            "bundle.json",
            "--candidate-commit",
            "0" * 40,
            "--remote-attestation",
            "remote.json",
            "--remote-policy",
            "policy.yaml",
        ]
    )
    assert push.control_plane_root == ["trellis_tasks=/tasks"]
