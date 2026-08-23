"""Fail-closed, atomic promotion of a locally rendered, QC-approved asset.

The immutable asset and its canonical manifest are installed before ``current.json``
is replaced.  Consumers that use only ``current.json`` therefore observe either the
previous complete output or the new complete output, never a partially promoted one.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, cast

_CHUNK_SIZE = 1024 * 1024
_SHA256_PREFIX = "sha256:"


class LocalPromotionError(Exception):
    """Raised when an output cannot be safely promoted."""


@dataclass(frozen=True, slots=True)
class LocalPromotionRequest:
    """Verified inputs needed to promote one approved local render.

    ``qc_manifest`` is the approval authority.  It must be immutable JSON data
    whose top-level ``status`` is exactly ``"approved"``.  ``report_manifest`` is
    retained verbatim (after canonical JSON validation) as the accompanying report.
    """

    output_root: Path
    staging_asset: Path
    asset_sha256: str
    qc_manifest: Mapping[str, object]
    report_manifest: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class PromotionResult:
    """Paths and digests of the promoted immutable output."""

    asset_path: Path
    manifest_path: Path
    current_path: Path
    asset_sha256: str
    manifest_sha256: str


def promote_local_output(request: LocalPromotionRequest) -> PromotionResult:
    """Atomically make a verified, QC-approved staging asset current.

    The output layout is deliberately content-addressed:

    ``assets/sha256/<hex>`` contains the bytes and
    ``manifests/sha256/<hex>.json`` contains its canonical promotion manifest.
    Existing immutable entries are accepted only when their complete contents are
    identical.  The mutable ``current.json`` pointer is written last using a
    fsynced temporary file and ``os.replace``.
    """

    _validate_request(request)
    output_root = request.output_root
    output_root.mkdir(parents=True, exist_ok=True)
    if not output_root.is_dir() or output_root.is_symlink():
        raise LocalPromotionError("output_root must be a real directory")

    asset_hex = request.asset_sha256.removeprefix(_SHA256_PREFIX)
    asset_relative = PurePosixPath("assets") / "sha256" / asset_hex
    asset_path = _resolve_generated_path(output_root, asset_relative)
    _install_asset(request.staging_asset, asset_path, request.asset_sha256)

    manifest_value: dict[str, object] = {
        "asset": {"path": asset_relative.as_posix(), "sha256": request.asset_sha256},
        "qc_manifest": _json_copy(request.qc_manifest, "qc_manifest"),
        "report_manifest": _json_copy(request.report_manifest, "report_manifest"),
        "schema_version": 1,
    }
    manifest_bytes = _canonical_json_bytes(manifest_value, "promotion manifest")
    manifest_sha256 = _sha256(manifest_bytes)
    manifest_relative = PurePosixPath("manifests") / "sha256" / f"{manifest_sha256[7:]}.json"
    manifest_path = _resolve_generated_path(output_root, manifest_relative)
    _install_bytes(manifest_path, manifest_bytes, "manifest")

    current_bytes = _canonical_json_bytes(
        {
            "asset": {"path": asset_relative.as_posix(), "sha256": request.asset_sha256},
            "manifest": {"path": manifest_relative.as_posix(), "sha256": manifest_sha256},
            "schema_version": 1,
        },
        "current pointer",
    )
    current_path = output_root / "current.json"
    _atomic_replace(current_path, current_bytes)
    return PromotionResult(asset_path, manifest_path, current_path, request.asset_sha256, manifest_sha256)


def _validate_request(request: LocalPromotionRequest) -> None:
    _validate_digest(request.asset_sha256, "asset_sha256")
    qc_manifest = _json_copy(request.qc_manifest, "qc_manifest")
    _json_copy(request.report_manifest, "report_manifest")
    if qc_manifest.get("status") != "approved":
        raise LocalPromotionError("qc_manifest.status must be 'approved'")


def _validate_digest(value: object, field_name: str) -> None:
    if (
        type(value) is not str
        or len(value) != len(_SHA256_PREFIX) + 64
        or not value.startswith(_SHA256_PREFIX)
        or any(character not in "0123456789abcdef" for character in value[len(_SHA256_PREFIX) :])
    ):
        raise LocalPromotionError(f"{field_name} must be a lowercase sha256 digest")


def _resolve_generated_path(root: Path, relative: PurePosixPath) -> Path:
    if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise LocalPromotionError("generated path must be a non-empty relative path")
    target = root.joinpath(*relative.parts)
    try:
        target.relative_to(root)
    except ValueError as error:  # Defensive: PurePosixPath validation above is authoritative.
        raise LocalPromotionError("generated path escapes output_root") from error
    return target


def _install_asset(source: Path, target: Path, expected_sha256: str) -> None:
    """Copy source to a private temporary file, then link it into its CAS path."""

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.parent.is_symlink():
        raise LocalPromotionError("generated asset directory must not be a symlink")
    descriptor = _open_regular_source(source)
    temporary: Path | None = None
    try:
        temporary_descriptor, temporary_name = tempfile.mkstemp(prefix=".asset-", dir=target.parent)
        temporary = Path(temporary_name)
        digest = hashlib.sha256()
        byte_count = 0
        try:
            with os.fdopen(descriptor, "rb", closefd=True) as input_stream, os.fdopen(
                temporary_descriptor, "wb", closefd=True
            ) as output_stream:
                descriptor = -1
                for chunk in iter(lambda: input_stream.read(_CHUNK_SIZE), b""):
                    digest.update(chunk)
                    byte_count += len(chunk)
                    output_stream.write(chunk)
                output_stream.flush()
                os.fsync(output_stream.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        actual_sha256 = f"{_SHA256_PREFIX}{digest.hexdigest()}"
        if byte_count == 0:
            raise LocalPromotionError("staging_asset must be non-empty")
        if actual_sha256 != expected_sha256:
            raise LocalPromotionError("staging_asset digest does not match asset_sha256")
        _install_temp_exclusively(temporary, target, expected_sha256, "asset")
        temporary = None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _open_regular_source(path: Path) -> int:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as error:
        raise LocalPromotionError("staging_asset must be a readable regular non-symlink file") from error
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise LocalPromotionError("staging_asset must be a regular file")
    return descriptor


def _install_bytes(target: Path, value: bytes, label: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_descriptor, temporary_name = tempfile.mkstemp(prefix=f".{label}-", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(temporary_descriptor, "wb", closefd=True) as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        _install_temp_exclusively(temporary, target, _sha256(value), label)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _install_temp_exclusively(temp: Path, target: Path, expected_sha256: str, label: str) -> None:
    """Publish a completed file without overwriting a pre-existing immutable path."""

    try:
        os.link(temp, target)
    except FileExistsError:
        if not target.is_file() or target.is_symlink() or _sha256_file(target) != expected_sha256:
            raise LocalPromotionError(f"conflicting immutable {label} already exists") from None
    else:
        os.chmod(target, 0o444)
        _fsync_directory(target.parent)
    finally:
        temp.unlink(missing_ok=True)


def _atomic_replace(target: Path, value: bytes) -> None:
    """Replace a mutable pointer only after its complete new value is durable."""

    temporary_descriptor, temporary_name = tempfile.mkstemp(prefix=".current-", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(temporary_descriptor, "wb", closefd=True) as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        temporary = None
        _fsync_directory(target.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sha256_file(path: Path) -> str:
    descriptor = _open_regular_source(path)
    try:
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = -1
            for chunk in iter(lambda: stream.read(_CHUNK_SIZE), b""):
                digest.update(chunk)
        return f"{_SHA256_PREFIX}{digest.hexdigest()}"
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _sha256(value: bytes) -> str:
    return f"{_SHA256_PREFIX}{hashlib.sha256(value).hexdigest()}"


def _canonical_json_bytes(value: object, field_name: str) -> bytes:
    return json.dumps(
        _json_copy(value, field_name), ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False
    ).encode("utf-8")


def _json_copy(value: object, field_name: str) -> Any:
    """Validate the precise JSON subset this persistence boundary accepts."""

    if value is None or type(value) is bool or type(value) is int or type(value) is str:  # noqa: E721
        return value
    if type(value) is float:  # noqa: E721
        raise LocalPromotionError(f"{field_name} must not contain floats")
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        mapping = cast(Mapping[object, object], value)
        for key, item in mapping.items():
            if type(key) is not str:  # noqa: E721
                raise LocalPromotionError(f"{field_name} object keys must be strings")
            result[key] = _json_copy(item, f"{field_name}.{key}")
        return result
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        sequence = cast(Sequence[object], value)
        return [_json_copy(item, f"{field_name}[]") for item in sequence]
    raise LocalPromotionError(f"{field_name} must contain only JSON values")
