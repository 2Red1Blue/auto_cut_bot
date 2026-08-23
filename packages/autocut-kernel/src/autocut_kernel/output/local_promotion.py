"""Fail-closed, atomic promotion of a locally rendered, QC-approved asset.

The immutable asset and its canonical manifest are installed before ``current.json``
is replaced.  Consumers that use only ``current.json`` therefore observe either the
previous complete output or the new complete output, never a partially promoted one.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, cast

from ..rendering.qc import QCReport

_CHUNK_SIZE = 1024 * 1024
_SHA256_PREFIX = "sha256:"
_NAMESPACE_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")


class LocalPromotionError(Exception):
    """Raised when an output cannot be safely promoted."""


@dataclass(frozen=True, slots=True)
class LocalPromotionRequest:
    """Verified inputs needed to promote one approved local render.

    ``qc_report`` is a derived :class:`~autocut_kernel.rendering.qc.QCReport`, not
    caller-controlled status data.  Its approved outcome and two identity bindings
    must agree with the recipe and staging asset being promoted.
    """

    output_root: Path
    job_id: str
    attempt_id: str
    staging_asset: Path
    recipe_hash: str
    asset_sha256: str
    qc_report: QCReport


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

    ``assets/sha256/<prefix>/<hex>.mp4`` contains the bytes and
    ``results/<job_id>/<attempt_id>/manifests/<hex>.json`` contains its canonical
    promotion manifest.  The mutable pointer is scoped to that same attempt.
    Existing immutable entries are accepted only when their complete contents are
    identical.  The mutable ``current.json`` pointer is written last using a
    fsynced temporary file and ``os.replace``.
    """

    _validate_request(request)
    output_root = request.output_root
    _ensure_output_root(output_root)

    asset_hex = request.asset_sha256.removeprefix(_SHA256_PREFIX)
    asset_relative = PurePosixPath("assets") / "sha256" / asset_hex[:2] / f"{asset_hex}.mp4"
    asset_path = _resolve_generated_path(output_root, asset_relative)
    _install_asset(output_root, request.staging_asset, asset_path, request.asset_sha256)

    result_relative = PurePosixPath("results") / request.job_id / request.attempt_id

    manifest_value: dict[str, object] = {
        "asset": {"path": asset_relative.as_posix(), "sha256": request.asset_sha256},
        "qc_report": request.qc_report.to_manifest(),
        "recipe_hash": request.recipe_hash,
        "schema_version": 1,
    }
    manifest_bytes = _canonical_json_bytes(manifest_value, "promotion manifest")
    manifest_sha256 = _sha256(manifest_bytes)
    manifest_relative = result_relative / "manifests" / f"{manifest_sha256[7:]}.json"
    manifest_path = _resolve_generated_path(output_root, manifest_relative)
    _install_bytes(output_root, manifest_path, manifest_bytes, "manifest")

    current_bytes = _canonical_json_bytes(
        {
            "asset": {"path": asset_relative.as_posix(), "sha256": request.asset_sha256},
            "manifest": {"path": manifest_relative.as_posix(), "sha256": manifest_sha256},
            "schema_version": 1,
        },
        "current pointer",
    )
    current_path = _resolve_generated_path(output_root, result_relative / "current.json")
    _ensure_real_directory(output_root, current_path.parent)
    _atomic_replace(current_path, current_bytes)
    return PromotionResult(asset_path, manifest_path, current_path, request.asset_sha256, manifest_sha256)


def _validate_request(request: LocalPromotionRequest) -> None:
    _validate_digest(request.asset_sha256, "asset_sha256")
    _validate_digest(request.recipe_hash, "recipe_hash")
    _validate_namespace_component(request.job_id, "job_id")
    _validate_namespace_component(request.attempt_id, "attempt_id")
    if not isinstance(cast(object, request.qc_report), QCReport):
        raise LocalPromotionError("qc_report must be a derived QCReport")
    if not request.qc_report.approved:
        raise LocalPromotionError("qc_report must be approved")
    if request.qc_report.recipe_hash != request.recipe_hash:
        raise LocalPromotionError("qc_report.recipe_hash does not match recipe_hash")
    if request.qc_report.output_sha256 != request.asset_sha256:
        raise LocalPromotionError("qc_report.output_sha256 does not match asset_sha256")


def _validate_digest(value: object, field_name: str) -> None:
    if (
        type(value) is not str
        or len(value) != len(_SHA256_PREFIX) + 64
        or not value.startswith(_SHA256_PREFIX)
        or any(character not in "0123456789abcdef" for character in value[len(_SHA256_PREFIX) :])
    ):
        raise LocalPromotionError(f"{field_name} must be a lowercase sha256 digest")


def _validate_namespace_component(value: object, field_name: str) -> None:
    if type(value) is not str or _NAMESPACE_COMPONENT.fullmatch(value) is None:  # noqa: E721
        raise LocalPromotionError(f"{field_name} must be a safe non-empty namespace component")


def _resolve_generated_path(root: Path, relative: PurePosixPath) -> Path:
    if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise LocalPromotionError("generated path must be a non-empty relative path")
    target = root.joinpath(*relative.parts)
    try:
        target.relative_to(root)
    except ValueError as error:  # Defensive: PurePosixPath validation above is authoritative.
        raise LocalPromotionError("generated path escapes output_root") from error
    return target


def _ensure_output_root(root: Path) -> None:
    """Create the selected root, refusing a symlink at the boundary."""

    existed = root.exists()
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise LocalPromotionError("output_root must be a real directory")
    _fsync_directory(root)
    if not existed:
        _fsync_directory(root.parent)


def _ensure_real_directory(root: Path, directory: Path) -> None:
    """Create a generated directory one component at a time without symlinks."""

    try:
        relative = directory.relative_to(root)
    except ValueError as error:
        raise LocalPromotionError("generated directory escapes output_root") from error
    probe = root
    if probe.is_symlink() or not probe.is_dir():
        raise LocalPromotionError("output_root must be a real directory")
    missing: list[Path] = []
    for part in relative.parts:
        probe = probe / part
        if probe.exists():
            if probe.is_symlink() or not probe.is_dir():
                raise LocalPromotionError("generated directory component must be a real directory")
        else:
            missing.append(probe)
    for item in missing:
        if item.parent.is_symlink():
            raise LocalPromotionError("generated directory component must not be a symlink")
        try:
            item.mkdir()
        except FileExistsError:
            pass
        if item.is_symlink() or not item.is_dir():
            raise LocalPromotionError("generated directory component must be a real directory")
        _fsync_directory(item)
        _fsync_directory(item.parent)


def _install_asset(root: Path, source: Path, target: Path, expected_sha256: str) -> None:
    """Copy source to a private temporary file, then link it into its CAS path."""

    _ensure_real_directory(root, target.parent)
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


def _install_bytes(root: Path, target: Path, value: bytes, label: str) -> None:
    _ensure_real_directory(root, target.parent)
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
        if (
            not target.is_file()
            or target.is_symlink()
            or os.stat(target).st_nlink != 1
            or not _files_equal(temp, target)
        ):
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


def _files_equal(left: Path, right: Path) -> bool:
    """Compare existing immutable bytes rather than trusting its path digest."""

    try:
        if left.stat().st_size != right.stat().st_size:
            return False
    except OSError:
        return False
    left_descriptor = _open_regular_source(left)
    right_descriptor = _open_regular_source(right)
    try:
        with os.fdopen(left_descriptor, "rb", closefd=True) as left_stream, os.fdopen(
            right_descriptor, "rb", closefd=True
        ) as right_stream:
            left_descriptor = right_descriptor = -1
            while True:
                left_chunk = left_stream.read(_CHUNK_SIZE)
                right_chunk = right_stream.read(_CHUNK_SIZE)
                if left_chunk != right_chunk:
                    return False
                if not left_chunk:
                    return True
    finally:
        if left_descriptor >= 0:
            os.close(left_descriptor)
        if right_descriptor >= 0:
            os.close(right_descriptor)


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
