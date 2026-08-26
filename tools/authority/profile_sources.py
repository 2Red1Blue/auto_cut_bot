"""Compile one closed local-profile source triple from an A/B/C authority lock.

This build-side helper deliberately has no generic RegistrySet input or ready
state.  It verifies only the exact narrative/profile/schema blobs selected by
the dedicated local authority profile grammar.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from autocut_kernel.registry.installed_local_run import (
    LocalRunResourceError,
    compute_local_profile_registry_sha256,
)

from .common import load_mapping_bytes, validate_relative_path
from .errors import GateViolation
from .locked_registry import read_locked_blob, verify_locked_authority_sources


@dataclass(frozen=True, slots=True)
class LockedProfileCompilation:
    """Three exact source blobs plus their non-generic local identity."""

    registry_sha256: str
    lock_raw: bytes
    narrative_raw: bytes
    profile_raw: bytes
    schema_raw: bytes
    lock_repository: str
    lock_commit: str
    lock_path: str
    profile_repository: str
    narrative_path: str
    profile_path: str
    schema_path: str


def compile_locked_profile_sources(
    *,
    repository_roots: Mapping[str, Path],
    lock_repository: str,
    lock_commit: str,
    lock_path: str,
    profile_repository: str,
    narrative_path: str,
    profile_path: str,
    schema_path: str,
    profile_kind: str,
) -> LockedProfileCompilation:
    """Verify C→B→A and read the exact local profile source triple.

    ``profile_kind`` is included in the derived identity so shadow-calibration
    and successor local-run sources cannot share a registry hash merely by
    reusing source bytes.  Semantic grammar and predecessor closure are owned
    by the callers that know the respective profile type.
    """
    narrative_path = validate_relative_path(narrative_path, where="narrative profile path")
    profile_path = validate_relative_path(profile_path, where="profile path")
    schema_path = validate_relative_path(schema_path, where="profile schema path")
    if len({narrative_path, profile_path, schema_path}) != 3:
        raise GateViolation("AUTH-PROFILE-SOURCE-PATH", "profile source roles require distinct paths")
    if any(not path.startswith("governance/") for path in (narrative_path, profile_path, schema_path)):
        raise GateViolation("AUTH-PROFILE-SOURCE-PATH", "profile source roles require governed paths")
    lock_raw = verify_locked_authority_sources(
        repository_roots=repository_roots,
        lock_repository=lock_repository,
        lock_commit=lock_commit,
        lock_path=lock_path,
    )
    lock = load_mapping_bytes(lock_raw, where="verified committed authority lock")
    narrative_raw = read_locked_blob(
        lock=lock,
        repository_roots=repository_roots,
        repository=profile_repository,
        path=narrative_path,
        expected_class="registry_source",
    )
    profile_raw = read_locked_blob(
        lock=lock,
        repository_roots=repository_roots,
        repository=profile_repository,
        path=profile_path,
        expected_class="registry_source",
    )
    schema_raw = read_locked_blob(
        lock=lock,
        repository_roots=repository_roots,
        repository=profile_repository,
        path=schema_path,
        expected_class="schema_source",
    )
    try:
        registry_sha256 = compute_local_profile_registry_sha256(
            profile_kind=profile_kind,
            narrative_raw=narrative_raw,
            profile_raw=profile_raw,
            schema_raw=schema_raw,
        )
    except LocalRunResourceError as error:
        raise GateViolation("AUTH-PROFILE-KIND", "unsupported local authority profile kind") from error
    return LockedProfileCompilation(
        registry_sha256,
        lock_raw,
        narrative_raw,
        profile_raw,
        schema_raw,
        lock_repository,
        lock_commit,
        lock_path,
        profile_repository,
        narrative_path,
        profile_path,
        schema_path,
    )
