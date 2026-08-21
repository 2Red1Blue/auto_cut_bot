# pyright: reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnknownMemberType=false
"""Live-bound remote-protection collection and verification."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .common import (
    canonical_hash,
    git_output,
    load_mapping,
    require_closed,
    require_commit,
    require_list,
    require_non_empty_string,
    sha256_bytes,
)
from .errors import GateViolation
from .receipts import make_typed_receipt, validate_typed_receipt


def _parse_timestamp(value: Any, *, where: str) -> datetime:
    text = require_non_empty_string(value, where=where)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GateViolation("AUTH-REMOTE-TIME", f"invalid timestamp: {where}") from exc
    if parsed.tzinfo is None:
        raise GateViolation("AUTH-REMOTE-TIME", f"timestamp lacks timezone: {where}")
    return parsed.astimezone(UTC)


def canonical_remote_url(value: Any) -> str:
    text = require_non_empty_string(value, where="remote URL").strip()
    if text.startswith("git@") and ":" in text:
        host, path = text[4:].split(":", 1)
        text = f"ssh://git@{host}/{path}"
    parts = urlsplit(text)
    if parts.scheme in {"http", "https", "ssh"}:
        path = parts.path.removesuffix(".git").rstrip("/")
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))
    return str(Path(text).resolve())


def _validate_policy(policy: dict[str, Any]) -> None:
    require_closed(
        policy,
        required=(
            "schema_version",
            "canonical_remote_url",
            "target_ref",
            "allowed_collector_ids",
            "required_check_ids",
            "require_codeowners",
            "deny_direct_push",
            "max_attestation_ttl_seconds",
        ),
        where="remote protection policy",
    )
    canonical_remote_url(policy["canonical_remote_url"])
    require_non_empty_string(policy["target_ref"], where="target_ref")
    require_list(policy["required_check_ids"], where="required_check_ids", non_empty=True)
    require_list(policy["allowed_collector_ids"], where="allowed_collector_ids", non_empty=True)
    maximum = policy["max_attestation_ttl_seconds"]
    if not isinstance(maximum, int) or maximum < 1:
        raise GateViolation("AUTH-REMOTE-TTL", "max_attestation_ttl_seconds must be positive")


def _validate_collector(evidence: dict[str, Any]) -> None:
    require_closed(
        evidence,
        required=(
            "schema_version",
            "collector_id",
            "canonical_remote_url",
            "target_ref",
            "observed_remote_oid",
            "protection_enabled",
            "direct_push_disabled",
            "codeowners_required",
            "required_check_ids",
            "bypass_test_passed",
            "collected_at",
        ),
        where="remote collector evidence",
    )
    require_non_empty_string(evidence["collector_id"], where="collector_id")
    require_commit(evidence["observed_remote_oid"], where="observed_remote_oid")
    _parse_timestamp(evidence["collected_at"], where="collected_at")


@dataclass(frozen=True)
class _LiveProviderObservation:
    normalized: dict[str, Any]
    raw_evidence: bytes


LiveCollector = Callable[[Path, dict[str, Any]], _LiveProviderObservation]


def _run_provider_command(repository_root: Path, argv: list[str]) -> bytes:
    try:
        return subprocess.run(argv, cwd=repository_root, check=True, capture_output=True).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise GateViolation(
            "AUTH-PUSH-PROVIDER-COLLECTOR", f"live provider command failed: {argv[0]}"
        ) from exc


def _github_identity(remote_url: str) -> tuple[str, str]:
    parts = urlsplit(canonical_remote_url(remote_url))
    if parts.hostname != "github.com":
        raise GateViolation("AUTH-PUSH-PROVIDER", "GitHub collector requires github.com")
    path = parts.path.strip("/").split("/")
    if len(path) != 2 or not all(path):
        raise GateViolation("AUTH-PUSH-PROVIDER", "cannot derive GitHub repository identity")
    return path[0], path[1]


def _collect_github_rulesets_live_v1(
    repository_root: Path, policy: dict[str, Any]
) -> _LiveProviderObservation:
    """Collect branch protection from GitHub, never from a caller-owned file."""

    owner, repository = _github_identity(str(policy["canonical_remote_url"]))
    target_ref = str(policy["target_ref"])
    branch = target_ref.removeprefix("refs/heads/")
    protection_raw = _run_provider_command(
        repository_root,
        ["gh", "api", f"repos/{owner}/{repository}/branches/{branch}/protection"],
    )
    rulesets_raw = _run_provider_command(
        repository_root,
        ["gh", "api", f"repos/{owner}/{repository}/rulesets?includes_parents=true"],
    )
    try:
        protection = json.loads(protection_raw)
        rulesets = json.loads(rulesets_raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise GateViolation("AUTH-PUSH-PROVIDER-PAYLOAD", "provider returned invalid JSON") from exc
    if not isinstance(protection, dict) or not isinstance(rulesets, list):
        raise GateViolation("AUTH-PUSH-PROVIDER-PAYLOAD", "provider payload shape is invalid")
    checks: set[str] = set()
    required_status = protection.get("required_status_checks")
    if isinstance(required_status, dict):
        for context in required_status.get("contexts", []):
            if isinstance(context, str) and context:
                checks.add(context)
        for check in required_status.get("checks", []):
            if isinstance(check, dict) and isinstance(check.get("context"), str):
                checks.add(check["context"])
    pull_reviews = protection.get("required_pull_request_reviews")
    codeowners = (
        isinstance(pull_reviews, dict) and pull_reviews.get("require_code_owner_reviews") is True
    )
    enforcement_active = any(
        isinstance(item, dict) and item.get("enforcement") == "active" for item in rulesets
    )
    bypass_empty = all(
        not item.get("bypass_actors")
        for item in rulesets
        if isinstance(item, dict) and item.get("enforcement") == "active"
    )
    observed_oid = git_output(repository_root, "ls-remote", "origin", target_ref).split(maxsplit=1)[
        0
    ]
    require_commit(observed_oid, where="live remote oid")
    collected_at = datetime.now(tz=UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    normalized = {
        "schema_version": "1.0.0",
        "collector_id": "github-rulesets-live-v1",
        "canonical_remote_url": policy["canonical_remote_url"],
        "target_ref": target_ref,
        "observed_remote_oid": observed_oid,
        "protection_enabled": enforcement_active,
        "direct_push_disabled": isinstance(pull_reviews, dict) and enforcement_active,
        "codeowners_required": codeowners,
        "required_check_ids": sorted(checks),
        "bypass_test_passed": enforcement_active and bypass_empty,
        "collected_at": collected_at,
    }
    raw = (
        b"github-branch-protection\0"
        + protection_raw
        + b"\0github-repository-rulesets\0"
        + rulesets_raw
    )
    return _LiveProviderObservation(normalized=normalized, raw_evidence=raw)


_LIVE_PROVIDER_COLLECTORS: dict[str, LiveCollector] = {
    "github-rulesets-live-v1": _collect_github_rulesets_live_v1,
}


def collect_remote_protection_attestation(
    *,
    repository_root: Path,
    policy: dict[str, Any],
    collector_id: str,
    candidate_commit: str,
    authority_lock_hash: str,
    task_id: str | None,
    expires_at: str,
) -> dict[str, Any]:
    """Bind provider evidence to live Git identity and current remote OID."""

    _validate_policy(policy)
    if collector_id not in policy["allowed_collector_ids"]:
        raise GateViolation("AUTH-PUSH-COLLECTOR", "collector is not authority-approved")
    collector = _LIVE_PROVIDER_COLLECTORS.get(collector_id)
    if collector is None:
        raise GateViolation(
            "AUTH-PUSH-COLLECTOR-UNAVAILABLE",
            "approved collector has no live provider implementation",
        )
    observation = collector(repository_root, policy)
    collector_evidence = observation.normalized
    _validate_collector(collector_evidence)
    if collector_evidence["collector_id"] != collector_id:
        raise GateViolation("AUTH-PUSH-COLLECTOR", "collector identity mismatch")
    require_commit(candidate_commit, where="candidate_commit")
    live_url = canonical_remote_url(git_output(repository_root, "remote", "get-url", "origin"))
    expected_url = canonical_remote_url(policy["canonical_remote_url"])
    observed_url = canonical_remote_url(collector_evidence["canonical_remote_url"])
    if live_url != expected_url or observed_url != expected_url:
        raise GateViolation("AUTH-PUSH-REMOTE-IDENTITY", "collector/live remote URL mismatch")
    target_ref = str(policy["target_ref"])
    if collector_evidence["target_ref"] != target_ref:
        raise GateViolation("AUTH-PUSH-REMOTE-REF", "collector target ref mismatch")
    remote_line = git_output(repository_root, "ls-remote", "origin", target_ref)
    expected_oid = remote_line.split(maxsplit=1)[0] if remote_line else ""
    require_commit(expected_oid, where="live remote oid")
    if collector_evidence["observed_remote_oid"] != expected_oid:
        raise GateViolation("AUTH-PUSH-REMOTE-OID", "collector remote OID is stale")
    required = sorted(set(require_list(policy["required_check_ids"], where="required checks")))
    observed = sorted(
        set(require_list(collector_evidence["required_check_ids"], where="observed checks"))
    )
    protected = (
        collector_evidence["protection_enabled"] is True
        and (not policy["deny_direct_push"] or collector_evidence["direct_push_disabled"] is True)
        and (not policy["require_codeowners"] or collector_evidence["codeowners_required"] is True)
        and set(required).issubset(observed)
        and collector_evidence["bypass_test_passed"] is True
    )
    if not protected:
        raise GateViolation("AUTH-PUSH-REMOTE-UNPROTECTED", "remote protection is not verified")
    return make_typed_receipt(
        "remote_protection",
        authority_lock_hash=authority_lock_hash,
        decision="allow",
        reason_codes=[],
        task_id=task_id,
        remote_canonical_url=expected_url,
        target_ref=target_ref,
        expected_remote_oid=expected_oid,
        candidate_commit=candidate_commit,
        policy_hash=canonical_hash(policy),
        collector_evidence_hash=sha256_bytes(observation.raw_evidence),
        collector_id=collector_id,
        protection_enabled=True,
        required_checks_hash=canonical_hash(required),
        fetched_at=collector_evidence["collected_at"],
        expires_at=expires_at,
    )


def verify_remote_protection(
    *,
    attestation_path: Path,
    policy_path: Path,
    repository_root: Path,
    candidate_commit: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    attestation = load_mapping(attestation_path)
    policy = load_mapping(policy_path)
    validate_typed_receipt(attestation, expected_type="remote_protection")
    expected = collect_remote_protection_attestation(
        repository_root=repository_root,
        policy=policy,
        collector_id=str(attestation["collector_id"]),
        candidate_commit=candidate_commit,
        authority_lock_hash=str(attestation["authority_lock_hash"]),
        task_id=attestation["task_id"],
        expires_at=str(attestation["expires_at"]),
    )
    bound_fields = set(attestation) - {"receipt_id", "produced_at"}
    if any(attestation[field] != expected[field] for field in bound_fields):
        raise GateViolation(
            "AUTH-PUSH-ATTESTATION-BINDING", "attestation does not match live evidence"
        )
    instant = now or datetime.now(tz=UTC)
    fetched_at = _parse_timestamp(attestation["fetched_at"], where="fetched_at")
    expires_at = _parse_timestamp(attestation["expires_at"], where="expires_at")
    maximum = int(policy["max_attestation_ttl_seconds"])
    if (
        fetched_at > instant
        or instant >= expires_at
        or (expires_at - fetched_at).total_seconds() > maximum
    ):
        raise GateViolation("AUTH-PUSH-ATTESTATION-STALE", "remote attestation is stale")
    return attestation


def reject_offline_remote_evidence(_evidence_path: Path) -> None:
    """An offline snapshot may explain a denial but can never authorize push."""

    raise GateViolation(
        "AUTH-PUSH-OFFLINE-EVIDENCE",
        "offline/self-reported remote protection evidence cannot issue allow",
    )
