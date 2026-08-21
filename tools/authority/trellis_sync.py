# pyright: reportUnknownArgumentType=false
"""One-way tracked Trellis authority synchronization.

The tracked tree is the only source.  Drift checks never read operational
content as a source and sync refuses symlinks in either managed tree.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .common import (
    contained_path,
    load_mapping,
    require_closed,
    require_list,
    require_sha256,
    sha256_file,
    validate_relative_path,
)
from .errors import GateViolation


def validate_sync_manifest(manifest: Mapping[str, Any]) -> tuple[list[dict[str, str]], list[str]]:
    require_closed(
        manifest,
        required=("schema_version", "source_root", "destination_root", "managed_roots", "files"),
        where="Trellis sync manifest",
    )
    if manifest["schema_version"] != "1.0.0":
        raise GateViolation("AUTH-SYNC-VERSION", "unsupported sync manifest version")
    if manifest["source_root"] != "governance/trellis-spec":
        raise GateViolation("AUTH-SYNC-SOURCE-IDENTITY", "unexpected tracked source identity")
    if manifest["destination_root"] != ".trellis/spec":
        raise GateViolation("AUTH-SYNC-DESTINATION-IDENTITY", "unexpected operational identity")
    managed_roots = [
        validate_relative_path(item, where="managed_roots")
        for item in require_list(manifest["managed_roots"], where="managed_roots", non_empty=True)
    ]
    if len(managed_roots) != len(set(managed_roots)):
        raise GateViolation("AUTH-SYNC-ROOT-DUPLICATE", "managed_roots contains duplicates")

    files: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(require_list(manifest["files"], where="files", non_empty=True)):
        if not isinstance(item, dict):
            raise GateViolation("AUTH-SYNC-FILE", f"files[{index}] must be an object")
        require_closed(item, required=("path", "sha256"), where=f"files[{index}]")
        path = validate_relative_path(item["path"], where=f"files[{index}].path")
        if path in seen:
            raise GateViolation("AUTH-SYNC-FILE-DUPLICATE", f"duplicate managed file: {path}")
        if not any(path == root or path.startswith(f"{root}/") for root in managed_roots):
            raise GateViolation(
                "AUTH-SYNC-UNMANAGED-PATH", f"file not beneath managed root: {path}"
            )
        seen.add(path)
        files.append({"path": path, "sha256": require_sha256(item["sha256"], where="sha256")})
    return files, managed_roots


def _actual_files(root: Path, managed_roots: list[str]) -> set[str]:
    result: set[str] = set()
    for relative_root in managed_roots:
        managed_root = contained_path(root, relative_root)
        if not managed_root.exists():
            continue
        if not managed_root.is_dir():
            raise GateViolation(
                "AUTH-SYNC-NOT-DIRECTORY", f"managed root is not a dir: {managed_root}"
            )
        for path in managed_root.rglob("*"):
            if path.is_symlink():
                raise GateViolation("AUTH-SYNC-SYMLINK", f"symlink is forbidden: {path}")
            if path.is_file():
                result.add(path.relative_to(root).as_posix())
    return result


def _resolve_sync_roots(
    *, source_root: Path, destination_root: Path, manifest_path: Path
) -> tuple[Path, Path]:
    source = source_root.resolve(strict=True)
    destination = destination_root.resolve(strict=True)
    manifest = manifest_path.resolve(strict=True)
    if manifest.parent != source:
        raise GateViolation(
            "AUTH-SYNC-MANIFEST-OWNER",
            "sync manifest must be owned by the tracked source root",
        )
    if source == destination or source in destination.parents or destination in source.parents:
        raise GateViolation("AUTH-SYNC-DIRECTION", "source and destination must not overlap")
    return source, destination


def check_trellis_drift(*, source_root: Path, destination_root: Path, manifest_path: Path) -> None:
    manifest = load_mapping(manifest_path)
    files, managed_roots = validate_sync_manifest(manifest)
    source_root, destination_root = _resolve_sync_roots(
        source_root=source_root,
        destination_root=destination_root,
        manifest_path=manifest_path,
    )
    expected_paths = {item["path"] for item in files}
    if _actual_files(source_root, managed_roots) != expected_paths:
        raise GateViolation(
            "AUTH-SYNC-SOURCE-MANIFEST", "tracked source differs from exact manifest"
        )
    if _actual_files(destination_root, managed_roots) != expected_paths:
        raise GateViolation("AUTH-SYNC-DESTINATION-FILESET", "operational file set has drift")
    for item in files:
        source = contained_path(source_root, item["path"], allow_missing=False)
        destination = contained_path(destination_root, item["path"], allow_missing=False)
        if sha256_file(source) != item["sha256"]:
            raise GateViolation("AUTH-SYNC-SOURCE-HASH", f"source hash mismatch: {item['path']}")
        if sha256_file(destination) != item["sha256"]:
            raise GateViolation("AUTH-SYNC-DESTINATION-HASH", f"drift: {item['path']}")


def sync_trellis_authority(
    *, source_root: Path, destination_root: Path, manifest_path: Path
) -> None:
    """Render the exact tracked tree into an operational destination."""

    manifest = load_mapping(manifest_path)
    files, managed_roots = validate_sync_manifest(manifest)
    source_root, destination_root = _resolve_sync_roots(
        source_root=source_root,
        destination_root=destination_root,
        manifest_path=manifest_path,
    )
    expected_paths = {item["path"] for item in files}
    if _actual_files(source_root, managed_roots) != expected_paths:
        raise GateViolation(
            "AUTH-SYNC-SOURCE-MANIFEST", "tracked source differs from exact manifest"
        )

    for item in files:
        source = contained_path(source_root, item["path"], allow_missing=False)
        if sha256_file(source) != item["sha256"]:
            raise GateViolation("AUTH-SYNC-SOURCE-HASH", f"source hash mismatch: {item['path']}")
        destination = contained_path(destination_root, item["path"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and destination.is_dir():
            raise GateViolation(
                "AUTH-SYNC-TARGET-TYPE", f"destination is a directory: {destination}"
            )
        fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(source.read_bytes())
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, destination)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    extras = sorted(_actual_files(destination_root, managed_roots) - expected_paths, reverse=True)
    for relative in extras:
        path = contained_path(destination_root, relative, allow_missing=False)
        path.unlink()
    for relative_root in sorted(managed_roots, reverse=True):
        root = contained_path(destination_root, relative_root)
        for directory in sorted(
            (path for path in root.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            try:
                directory.rmdir()
            except OSError:
                pass
    check_trellis_drift(
        source_root=source_root,
        destination_root=destination_root,
        manifest_path=manifest_path,
    )
