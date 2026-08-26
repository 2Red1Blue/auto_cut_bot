"""Resolve local-run source inheritance through two independently checked chains.

This source-only context leaves calibration references unresolved. It grants no
bootstrap, publication or runtime capability, even when all source hashes close.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from autocut_kernel.registry.authority_profiles import (
    AuthorityProfileSourceError,
    LocalRunProfileSource,
    Stage1NarrativeProfileSource,
    decode_local_run_profile_source,
    decode_stage1_narrative_profile_source,
)
from autocut_kernel.registry.timed_speech_contract import (
    TimedSpeechContractError,
    timed_speech_registry_contract_sha256,
)

from .common import load_mapping_bytes, sha256_bytes, validate_relative_path
from .errors import GateViolation
from .locked_registry import LockedRegistryCompilation, compile_locked_registry, read_locked_blob
from .shadow_context import LockedShadowSourceContext, build_locked_shadow_context

LOCAL_RUN_PROFILE_SCHEMA_PATH = "governance/schemas/local-run-profile.schema.json"


@dataclass(frozen=True, slots=True)
class ShadowSourceSelection:
    lock_repository: str
    lock_commit: str
    lock_path: str
    registry_repository: str
    registry_root: str
    profile_repository: str
    narrative_path: str
    shadow_path: str


@dataclass(frozen=True, slots=True)
class LockedLocalRunSourceContext:
    compilation: LockedRegistryCompilation
    predecessor: LockedShadowSourceContext
    narrative: Stage1NarrativeProfileSource
    local_run: LocalRunProfileSource
    profile_repository: str
    profile_source_commit: str
    narrative_path: str
    local_run_path: str
    authority_lock_sha256: str


def build_locked_local_run_context(
    *,
    repository_roots: Mapping[str, Path],
    lock_repository: str,
    lock_commit: str,
    lock_path: str,
    registry_repository: str,
    registry_root: str,
    profile_repository: str,
    narrative_path: str,
    local_run_path: str,
    predecessor: ShadowSourceSelection,
) -> LockedLocalRunSourceContext:
    """Build both source contexts from Git selectors, never injected contexts."""
    if type(predecessor) is not ShadowSourceSelection:  # noqa: E721
        raise GateViolation("AUTH-LOCAL-RUN-SELECTION", "predecessor must be an exact shadow source selection")
    narrative_path = validate_relative_path(narrative_path, where="narrative profile path")
    local_run_path = validate_relative_path(local_run_path, where="local-run profile path")
    if narrative_path == local_run_path or any(
        not path.startswith("governance/") for path in (narrative_path, local_run_path)
    ):
        raise GateViolation("AUTH-LOCAL-RUN-SOURCE-PATH", "profiles require distinct governed paths")
    old = build_locked_shadow_context(
        repository_roots=repository_roots, lock_repository=predecessor.lock_repository,
        lock_commit=predecessor.lock_commit, lock_path=predecessor.lock_path,
        registry_repository=predecessor.registry_repository, registry_root=predecessor.registry_root,
        profile_repository=predecessor.profile_repository, narrative_path=predecessor.narrative_path,
        shadow_path=predecessor.shadow_path,
    )
    compilation = compile_locked_registry(
        repository_roots=repository_roots, lock_repository=lock_repository, lock_commit=lock_commit,
        lock_path=lock_path, registry_repository=registry_repository, registry_root=registry_root,
    )
    lock = load_mapping_bytes(compilation.lock_raw, where="verified local-run authority lock")
    narrative_raw = read_locked_blob(
        lock=lock, repository_roots=repository_roots, repository=profile_repository,
        path=narrative_path, expected_class="registry_source",
    )
    local_run_raw = read_locked_blob(
        lock=lock, repository_roots=repository_roots, repository=profile_repository,
        path=local_run_path, expected_class="registry_source",
    )
    schema_raw = read_locked_blob(
        lock=lock, repository_roots=repository_roots, repository=profile_repository,
        path=LOCAL_RUN_PROFILE_SCHEMA_PATH, expected_class="schema_source",
    )
    try:
        narrative = decode_stage1_narrative_profile_source(narrative_raw)
        local_run = decode_local_run_profile_source(
            local_run_raw, narrative=narrative, shadow=old.profiles.shadow,
            expected_profile_contract_sha256=sha256_bytes(schema_raw),
        )
    except AuthorityProfileSourceError as error:
        raise GateViolation("AUTH-LOCAL-RUN-PROFILE", "locked local-run profile grammar does not close") from error
    try:
        registry_contract_hash = timed_speech_registry_contract_sha256(schema_raw)
    except TimedSpeechContractError as error:
        raise GateViolation("AUTH-LOCAL-RUN-CONTRACT", "locked Registry schema closure is invalid") from error
    if local_run.timed_speech_registry_entry.registry_contract_sha256 != registry_contract_hash:
        raise GateViolation("AUTH-LOCAL-RUN-CONTRACT", "Registry entry does not bind its locked schema closure")
    reference = local_run.predecessor_shadow_profile
    if (
        reference.profile_version, reference.source_sha256,
        reference.registry_set_sha256, reference.authority_lock_sha256,
    ) != (
        old.profiles.shadow.profile_version, old.profiles.shadow.source_sha256,
        old.compilation.registry_set.source_hash, old.authority_lock_sha256,
    ):
        raise GateViolation("AUTH-LOCAL-RUN-PREDECESSOR", "local-run does not name the independently verified shadow chain")
    return LockedLocalRunSourceContext(
        compilation, old, narrative, local_run, profile_repository,
        str(lock["repositories"][profile_repository]["source_commit"]),
        narrative_path, local_run_path, str(lock["bundle_hash"]),
    )
