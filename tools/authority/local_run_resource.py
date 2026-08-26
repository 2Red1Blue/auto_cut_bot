"""Explicit build-side resource emission; ordinary wheel builds never call it.

The caller selects immutable Git sources and supplies the authority Store. The
emitter itself resolves both chains and the accepted record, never an injected
snapshot/context. Returned bytes belong in a controlled build, not runtime config.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from pathlib import Path

from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes, sha256_bytes
from autocut_kernel.registry.calibration_binding import CalibrationRecordAnchorReader
from autocut_kernel.registry.installed_local_run import decode_local_run_resource

from .local_run_calibration import bind_local_run_calibration
from .local_run_context import ShadowSourceSelection, build_locked_local_run_context
from .profile_sources import LockedProfileCompilation


def _chain(compilation: LockedProfileCompilation, lock_sha256: str) -> dict[str, object]:
    return {
        "registry_set_sha256": compilation.registry_sha256,
        "authority_lock_sha256": lock_sha256,
        "narrative_raw_base64": base64.b64encode(compilation.narrative_raw).decode("ascii"),
        "profile_raw_base64": base64.b64encode(compilation.profile_raw).decode("ascii"),
        "schema_raw_base64": base64.b64encode(compilation.schema_raw).decode("ascii"),
    }


def emit_locked_local_run_resource(
    *,
    repository_roots: Mapping[str, Path],
    lock_repository: str,
    lock_commit: str,
    lock_path: str,
    profile_repository: str,
    narrative_path: str,
    local_run_path: str,
    predecessor: ShadowSourceSelection,
    store: CalibrationRecordAnchorReader,
) -> bytes:
    """Emit only after exact source and accepted-calibration binding succeeds.

    No file, anchor, bootstrap or native effect is written here. The controlled
    packaging step stores these exact bytes and their raw SHA-256 together.
    """
    context = build_locked_local_run_context(
        repository_roots=repository_roots, lock_repository=lock_repository,
        lock_commit=lock_commit, lock_path=lock_path,
        profile_repository=profile_repository, narrative_path=narrative_path,
        local_run_path=local_run_path, predecessor=predecessor,
    )
    bind_local_run_calibration(context=context, store=store)
    raw = canonical_json_bytes({
        "schema_version": "installed-local-run-authority-v1",
        "current": _chain(context.compilation, context.authority_lock_sha256),
        "predecessor": _chain(context.predecessor.compilation, context.predecessor.authority_lock_sha256),
    })
    # Exercise the installed consumer grammar before any build can publish bytes.
    decode_local_run_resource(raw, expected_sha256=sha256_bytes(raw))
    return raw
