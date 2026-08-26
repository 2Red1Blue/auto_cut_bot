"""Build-side shadow sources from an immutable, verified authority chain.

This proves source origin and local-profile compilation, not calibration acceptance,
installed model identity, or permission to start a Pipeline. Calibration input
resolution still validates the native service projection before any dispatch.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from autocut_kernel.contracts.compiler.canonical import sha256_bytes
from autocut_kernel.registry.authority_profiles import (
    AuthorityProfileSourceError,
    UnresolvedAuthorityProfileSourceSet,
    decode_authority_profile_source_grammar,
)

from .common import load_mapping_bytes, validate_relative_path
from .errors import GateViolation
from .profile_sources import LockedProfileCompilation, compile_locked_profile_sources

SHADOW_PROFILE_SCHEMA_PATH = "governance/schemas/shadow-calibration-profile.schema.json"


@dataclass(frozen=True, slots=True)
class LockedShadowSourceContext:
    compilation: LockedProfileCompilation
    profiles: UnresolvedAuthorityProfileSourceSet
    profile_repository: str
    profile_source_commit: str
    narrative_path: str
    shadow_path: str
    authority_lock_sha256: str


def build_locked_shadow_context(
    *,
    repository_roots: Mapping[str, Path],
    lock_repository: str,
    lock_commit: str,
    lock_path: str,
    profile_repository: str,
    narrative_path: str,
    shadow_path: str,
) -> LockedShadowSourceContext:
    """Resolve profiles from exact Git entries, never caller bytes or hashes.

The admin/build caller selects immutable sources. The result has no bootstrap
method or runtime snapshot: its calibration/member references are unresolved.
Local-run requires a separately verified predecessor chain and accepted Store
anchor, neither of which this shadow-only entry can manufacture.
"""
    narrative_path = validate_relative_path(narrative_path, where="narrative profile path")
    shadow_path = validate_relative_path(shadow_path, where="shadow profile path")
    if narrative_path == shadow_path or any(
        not path.startswith("governance/") for path in (narrative_path, shadow_path)
    ):
        raise GateViolation("AUTH-SHADOW-SOURCE-PATH", "profiles require distinct governed paths")
    compilation = compile_locked_profile_sources(
        repository_roots=repository_roots,
        lock_repository=lock_repository,
        lock_commit=lock_commit,
        lock_path=lock_path,
        profile_repository=profile_repository,
        narrative_path=narrative_path,
        profile_path=shadow_path,
        schema_path=SHADOW_PROFILE_SCHEMA_PATH,
        profile_kind="shadow_calibration_v1",
    )
    lock = load_mapping_bytes(compilation.lock_raw, where="verified committed authority lock")
    try:
        profiles = decode_authority_profile_source_grammar(
            narrative_raw=compilation.narrative_raw,
            shadow_raw=compilation.profile_raw,
            expected_shadow_profile_contract_sha256=sha256_bytes(compilation.schema_raw),
        )
    except AuthorityProfileSourceError as error:
        raise GateViolation("AUTH-SHADOW-PROFILE", "locked shadow profile grammar does not close") from error
    return LockedShadowSourceContext(
        compilation, profiles, profile_repository,
        str(lock["repositories"][profile_repository]["source_commit"]),
        narrative_path, shadow_path, str(lock["bundle_hash"]),
    )
