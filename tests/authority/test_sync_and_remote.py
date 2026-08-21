from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from authority.common import canonical_hash
from authority.errors import GateViolation
from authority.fixture_runner import run_blocking_fixtures
from authority.remote_gate import (
    _LIVE_PROVIDER_COLLECTORS,
    _LiveProviderObservation,
    collect_remote_protection_attestation,
    reject_offline_remote_evidence,
    verify_remote_protection,
)
from authority.trellis_sync import check_trellis_drift, sync_trellis_authority

REPO_ROOT = Path(__file__).parents[2]
AUTHORITY_HASH = "sha256:" + "a" * 64


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_trellis_sync_is_one_way_exact_and_rejects_extra_files(tmp_path: Path) -> None:
    destination = tmp_path / "operational-spec"
    destination.mkdir()
    manifest = REPO_ROOT / "governance/trellis-spec/managed-files.json"
    source = REPO_ROOT / "governance/trellis-spec"
    sync_trellis_authority(source_root=source, destination_root=destination, manifest_path=manifest)
    check_trellis_drift(source_root=source, destination_root=destination, manifest_path=manifest)
    extra = destination / "auto_cut_bot/backend/old-v5.md"
    extra.write_text("legacy permission", encoding="utf-8")
    with pytest.raises(GateViolation, match="AUTH-SYNC-DESTINATION-FILESET"):
        check_trellis_drift(
            source_root=source, destination_root=destination, manifest_path=manifest
        )


def _remote_fixture(tmp_path: Path) -> tuple[Path, Path, str]:
    remote = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", remote], check=True, capture_output=True)
    local = tmp_path / "local"
    local.mkdir()
    _git(local, "init", "-b", "main")
    (local / "README.md").write_text("fixture\n", encoding="utf-8")
    _git(local, "add", "README.md")
    _git(
        local, "-c", "user.name=Fixture", "-c", "user.email=f@example.test", "commit", "-m", "seed"
    )
    candidate = _git(local, "rev-parse", "HEAD")
    _git(local, "remote", "add", "origin", str(remote))
    _git(local, "push", "origin", "main")
    return local, remote, candidate


def _policy(remote: Path) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "canonical_remote_url": str(remote.resolve()),
        "target_ref": "refs/heads/main",
        "allowed_collector_ids": ["fixture-live-v1"],
        "required_check_ids": ["authority"],
        "require_codeowners": True,
        "deny_direct_push": True,
        "max_attestation_ttl_seconds": 300,
    }


def _collector(remote: Path, oid: str, *, protected: bool = True) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "collector_id": "fixture-live-v1",
        "canonical_remote_url": str(remote.resolve()),
        "target_ref": "refs/heads/main",
        "observed_remote_oid": oid,
        "protection_enabled": protected,
        "direct_push_disabled": protected,
        "codeowners_required": protected,
        "required_check_ids": ["authority"] if protected else [],
        "bypass_test_passed": protected,
        "collected_at": "2026-08-21T00:00:00.000Z",
    }


def test_live_remote_attestation_binds_url_oid_candidate_policy_and_ttl(tmp_path: Path) -> None:
    local, remote, candidate = _remote_fixture(tmp_path)
    policy = _policy(remote)
    collector = _collector(remote, candidate)
    _LIVE_PROVIDER_COLLECTORS["fixture-live-v1"] = lambda _root, _policy: _LiveProviderObservation(
        normalized=dict(collector), raw_evidence=b"provider raw response v1"
    )
    attestation = collect_remote_protection_attestation(
        repository_root=local,
        policy=policy,
        collector_id="fixture-live-v1",
        candidate_commit=candidate,
        authority_lock_hash=AUTHORITY_HASH,
        task_id="task",
        expires_at="2026-08-21T00:05:00.000Z",
    )
    policy_path, attestation_path = (
        tmp_path / name for name in ("policy.yaml", "attestation.yaml")
    )
    policy_path.write_text(yaml.safe_dump(policy), encoding="utf-8")
    attestation_path.write_text(yaml.safe_dump(attestation), encoding="utf-8")
    verified = verify_remote_protection(
        attestation_path=attestation_path,
        policy_path=policy_path,
        repository_root=local,
        candidate_commit=candidate,
        now=datetime(2026, 8, 21, 0, 1, tzinfo=UTC),
    )
    assert verified["policy_hash"] == canonical_hash(policy)
    collector["observed_remote_oid"] = "b" * 40
    with pytest.raises(GateViolation, match="AUTH-PUSH-REMOTE-OID"):
        verify_remote_protection(
            attestation_path=attestation_path,
            policy_path=policy_path,
            repository_root=local,
            candidate_commit=candidate,
            now=datetime(2026, 8, 21, 0, 1, tzinfo=UTC),
        )


def test_unprotected_remote_is_always_denied(tmp_path: Path) -> None:
    local, remote, candidate = _remote_fixture(tmp_path)
    collector = _collector(remote, candidate, protected=False)
    _LIVE_PROVIDER_COLLECTORS["fixture-live-v1"] = lambda _root, _policy: _LiveProviderObservation(
        normalized=dict(collector), raw_evidence=b"provider unprotected response"
    )
    with pytest.raises(GateViolation, match="AUTH-PUSH-REMOTE-UNPROTECTED"):
        collect_remote_protection_attestation(
            repository_root=local,
            policy=_policy(remote),
            collector_id="fixture-live-v1",
            candidate_commit=candidate,
            authority_lock_hash=AUTHORITY_HASH,
            task_id="task",
            expires_at="2026-08-21T00:05:00.000Z",
        )


def test_offline_remote_snapshot_can_never_authorize_push(tmp_path: Path) -> None:
    evidence = tmp_path / "offline.yaml"
    evidence.write_text("protection_enabled: true\n", encoding="utf-8")
    with pytest.raises(GateViolation, match="AUTH-PUSH-OFFLINE-EVIDENCE"):
        reject_offline_remote_evidence(evidence)


def test_every_protected_fixture_is_executed_by_independent_runner() -> None:
    executed = run_blocking_fixtures(
        manifest_path=REPO_ROOT / "governance/blocking-fixtures.manifest.yaml",
        repository_root=REPO_ROOT,
        model_policy_path=REPO_ROOT / "governance/model-role-policy.yaml",
    )
    manifest = yaml.safe_load(
        (REPO_ROOT / "governance/blocking-fixtures.manifest.yaml").read_text()
    )
    assert set(executed) == {item["fixture_id"] for item in manifest["fixtures"]}
