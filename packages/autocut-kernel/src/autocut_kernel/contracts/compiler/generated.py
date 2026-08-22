"""Owned generated-tree writer and deterministic drift verifier."""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

from .canonical import canonical_json_bytes, load_canonical_json_bytes
from .errors import (
    ContractCompilerError,
    GeneratedTreeDriftError,
    GeneratedTreeOwnershipError,
)
from .manifest import HashManifest
from .source import SourceInput

GENERATED_TREE_OWNER = "autocut_kernel.contracts.compiler"
OWNER_MARKER = ".autocut-generated-owner.json"
MANIFEST_FILE = "manifest.json"
_RESERVED_PATHS = {OWNER_MARKER, MANIFEST_FILE}


def write_generated_tree(
    root: Path,
    *,
    generated_files: Mapping[str, bytes],
    sources: tuple[SourceInput, ...],
    compiler_version: str,
) -> HashManifest:
    """Replace an owned output tree with a complete deterministic snapshot.

    An existing directory must carry this compiler's ownership marker.  This
    prevents a compiler invocation from treating a hand-maintained directory as
    disposable generated output.
    """

    _assert_writable_root(root)
    normalized = _normalize_generated_files(generated_files)
    manifest = HashManifest.build(
        compiler_version=compiler_version,
        sources=sources,
        generated_files=normalized,
    )

    root.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{root.name}.tmp-", dir=root.parent))
    try:
        _write_snapshot(temporary, normalized, manifest)
        if root.exists():
            shutil.rmtree(root)
        os.replace(temporary, root)
    except OSError as error:
        raise ContractCompilerError(f"unable to write generated tree {root}: {error}") from error
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return manifest


def check_generated_tree(
    root: Path,
    *,
    generated_files: Mapping[str, bytes],
    sources: tuple[SourceInput, ...],
    compiler_version: str,
) -> HashManifest:
    """Verify an on-disk output tree is exactly the expected compiler snapshot."""

    expected_files = _normalize_generated_files(generated_files)
    expected = HashManifest.build(
        compiler_version=compiler_version,
        sources=sources,
        generated_files=expected_files,
    )
    _assert_owned_root(root, error_type=GeneratedTreeDriftError)

    manifest_path = root / MANIFEST_FILE
    try:
        raw_manifest = manifest_path.read_bytes()
        manifest_value, canonical_manifest = load_canonical_json_bytes(raw_manifest, origin=str(manifest_path))
    except (OSError, ContractCompilerError) as error:
        raise GeneratedTreeDriftError(f"{manifest_path}: missing or invalid generated manifest") from error
    if raw_manifest != canonical_manifest or manifest_value != expected.to_mapping():
        raise GeneratedTreeDriftError("generated manifest does not match the expected compiler snapshot")

    actual_files = _read_generated_files(root)
    if actual_files != expected_files:
        raise GeneratedTreeDriftError("generated tree content or file set has drifted")
    return expected


def _assert_writable_root(root: Path) -> None:
    if root.is_symlink():
        raise GeneratedTreeOwnershipError(f"{root}: generated root cannot be a symlink")
    if root.exists():
        _assert_owned_root(root, error_type=GeneratedTreeOwnershipError)


def _assert_owned_root(root: Path, *, error_type: type[ContractCompilerError]) -> None:
    if root.is_symlink() or not root.is_dir():
        raise error_type(f"{root}: generated root must be a regular directory")
    marker_path = root / OWNER_MARKER
    try:
        raw_marker = marker_path.read_bytes()
        marker, canonical_marker = load_canonical_json_bytes(raw_marker, origin=str(marker_path))
    except (OSError, ContractCompilerError) as error:
        raise error_type(f"{root}: generated tree has no valid ownership marker") from error
    if raw_marker != canonical_marker or marker != {"owner": GENERATED_TREE_OWNER}:
        raise error_type(f"{root}: generated tree belongs to a different owner")


def _normalize_generated_files(generated_files: Mapping[str, bytes]) -> dict[str, bytes]:
    normalized: dict[str, bytes] = {}
    for path, content in generated_files.items():
        _validate_generated_path(path)
        if type(content) is not bytes:  # noqa: E721 - compiler output is byte-exact.
            raise ContractCompilerError(f"generated file {path!r} must be bytes")
        normalized[path] = content
    return dict(sorted(normalized.items()))


def _validate_generated_path(value: str) -> None:
    if not value or "\\" in value:
        raise ContractCompilerError("generated paths must be non-empty POSIX paths")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ContractCompilerError(f"unsafe generated path {value!r}")
    if value in _RESERVED_PATHS or path.parts[0].startswith("."):
        raise ContractCompilerError(f"generated path {value!r} is reserved")


def _write_snapshot(root: Path, generated_files: Mapping[str, bytes], manifest: HashManifest) -> None:
    for relative_path, content in generated_files.items():
        target = root.joinpath(*PurePosixPath(relative_path).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    (root / OWNER_MARKER).write_bytes(canonical_json_bytes({"owner": GENERATED_TREE_OWNER}))
    (root / MANIFEST_FILE).write_bytes(manifest.to_bytes())


def _read_generated_files(root: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise GeneratedTreeDriftError(f"{path}: generated tree must not contain symlinks")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative in _RESERVED_PATHS:
            continue
        files[relative] = path.read_bytes()
    return files
