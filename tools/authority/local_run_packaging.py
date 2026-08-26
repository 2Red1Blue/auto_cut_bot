"""Publish one verified local-run resource into a controlled build staging tree.

This is an explicit build/admin step, never an ordinary wheel-build hook.  The
caller supplies the same immutable Git selectors and accepted-record Store as
the emitter; raw source/context/snapshot injection is deliberately unavailable.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from tempfile import mkdtemp

from autocut_kernel.contracts.compiler.canonical import sha256_bytes
from autocut_kernel.registry.calibration_binding import CalibrationRecordAnchorReader

from .errors import GateViolation
from .local_run_context import ShadowSourceSelection
from .local_run_resource import emit_locked_local_run_resource

_AUTHORITY_DIRECTORY = "_authority"
_RESOURCE_NAME = "local-run.json"
_DIGEST_NAME = "local-run.sha256"
_RESERVATION_NAME = ".local-run-authority-publish.lock"


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _controlled_kernel_package(value: object) -> Path:
    if not isinstance(value, Path) or not value.is_absolute():
        raise GateViolation("AUTH-PACKAGE-DESTINATION", "destination must be an absolute Path")
    if value.is_symlink() or not value.is_dir():
        raise GateViolation("AUTH-PACKAGE-DESTINATION", "destination must be an existing non-symlink directory")
    try:
        resolved = value.resolve(strict=True)
    except OSError as error:
        raise GateViolation("AUTH-PACKAGE-DESTINATION", "destination cannot be resolved") from error
    if value != resolved or value.parent.is_symlink() or not value.parent.is_dir():
        raise GateViolation("AUTH-PACKAGE-DESTINATION", "destination parent is not an exact controlled directory")
    return resolved


def _remove_private_stage(stage: Path) -> None:
    """Remove only the two files and directory this module created."""
    for name in (_RESOURCE_NAME, _DIGEST_NAME):
        candidate = stage / name
        if candidate.exists() and not candidate.is_symlink():
            candidate.unlink()
    if stage.exists() and not stage.is_symlink():
        stage.rmdir()


def prepare_locked_local_run_package(
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
    destination_kernel_package: Path,
) -> Path:
    """Emit and atomically publish the two fixed resource files once.

    The destination is a build-owned staging package tree.  The short-lived
    reservation coordinates cooperating build writers for that exact target;
    it is not a hostile-filesystem locking protocol.  Existing authority output
    always fails closed and is never replaced.
    """
    destination = _controlled_kernel_package(destination_kernel_package)
    output = destination / _AUTHORITY_DIRECTORY
    reservation = destination / _RESERVATION_NAME
    if _lexists(output):
        raise GateViolation("AUTH-PACKAGE-EXISTS", "authority resource output already exists")
    try:
        reservation.mkdir(mode=0o700)
    except FileExistsError as error:
        raise GateViolation("AUTH-PACKAGE-RESERVED", "authority resource output is already being prepared") from error
    except OSError as error:
        raise GateViolation("AUTH-PACKAGE-RESERVED", "cannot reserve authority resource output") from error
    stage: Path | None = None
    try:
        if _lexists(output):
            raise GateViolation("AUTH-PACKAGE-EXISTS", "authority resource output already exists")
        raw = emit_locked_local_run_resource(
            repository_roots=repository_roots,
            lock_repository=lock_repository,
            lock_commit=lock_commit,
            lock_path=lock_path,
            profile_repository=profile_repository,
            narrative_path=narrative_path,
            local_run_path=local_run_path,
            predecessor=predecessor,
            store=store,
        )
        if type(raw) is not bytes or not raw:  # noqa: E721
            raise GateViolation("AUTH-PACKAGE-RESOURCE", "emitter did not return resource bytes")
        stage = Path(mkdtemp(prefix=".local-run-authority-", dir=destination))
        (stage / _RESOURCE_NAME).write_bytes(raw)
        (stage / _DIGEST_NAME).write_bytes(sha256_bytes(raw).encode("ascii") + b"\n")
        if _lexists(output):
            raise GateViolation("AUTH-PACKAGE-EXISTS", "authority resource output already exists")
        try:
            os.rename(stage, output)
        except FileExistsError as error:
            raise GateViolation("AUTH-PACKAGE-EXISTS", "authority resource output already exists") from error
        stage = None
        return output
    finally:
        if stage is not None:
            _remove_private_stage(stage)
        try:
            reservation.rmdir()
        except OSError:
            # A failed cleanup must not cause a future build to overwrite a
            # possibly incomplete target; the stale reservation remains fail-closed.
            pass
