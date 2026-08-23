"""Fail-closed promotion using descriptor-relative filesystem operations."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, cast
from uuid import UUID

from ..rendering import Recipe, RecipeValidationError, parse_recipe
from ..rendering.qc import LocalQC, QCReport
from ..store import Job, PostgresRuntimeStore, RecipeReference, RuntimeStoreError
from ..store.models import canonical_recipe_scope

_CHUNK_SIZE = 1024 * 1024
_SHA256_PREFIX = "sha256:"
_NAMESPACE_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class LocalPromotionError(Exception):
    """Raised when output cannot be safely promoted."""


@dataclass(frozen=True, slots=True)
class LocalPromotionRequest:
    """Inputs for a local promotion.

    A ``QCReport`` is diagnostic evidence, never an authorization credential.
    The boundary repeats every required QC observation against current staging
    bytes and the validated recipe/attempt retained by ``LocalQC.inspect``.
    """

    output_root: Path
    job: Job
    attempt_id: str
    staging_asset: Path
    asset_sha256: str
    qc_report: QCReport
    recipe_reference: RecipeReference


@dataclass(frozen=True, slots=True)
class PromotionResult:
    asset_path: Path
    manifest_path: Path
    current_path: Path
    asset_sha256: str
    manifest_sha256: str


class LocalPromotionService:
    """Trusted composition boundary for DB-backed local visibility.

    The service's Store is supplied by application composition, not a caller
    request.  This models application trust boundaries, not hostile Python
    code: code able to instantiate or monkey-patch this service already has
    process authority and is outside this API's authorization model.
    """

    def __init__(self, store: PostgresRuntimeStore) -> None:
        if type(store) is not PostgresRuntimeStore:
            raise LocalPromotionError("store must be an exact PostgresRuntimeStore")
        self._store = store

    def promote(self, request: LocalPromotionRequest) -> PromotionResult:
        return _promote_local_output(request, self._store)


def promote_local_output(request: LocalPromotionRequest) -> PromotionResult:
    """Deprecated unsafe-free compatibility entrypoint.

    It cannot make output visible because it has no injected Store.  Use
    :class:`LocalPromotionService` at application composition instead.
    """
    raise LocalPromotionError("local promotion requires a trusted LocalPromotionService")


def _promote_local_output(
    request: LocalPromotionRequest, store: PostgresRuntimeStore
) -> PromotionResult:
    """Independently re-QC a staging asset before making it current.

    Immutable files live in ``assets/sha256`` and
    ``results/<job>/manifests``; the job-scoped mutable pointer is
    ``results/<job>/current.json``. Attempts are diagnostic evidence only.
    """
    _validate_request_shape(request)
    recipe, store_job_id = _rehydrate_persisted_recipe(request, store)
    report = _reverify_promotion_inputs(request, recipe)
    asset_hex = request.asset_sha256[7:]
    asset_relative = PurePosixPath("assets") / "sha256" / asset_hex[:2] / f"{asset_hex}.mp4"
    manifest_value: dict[str, object] = {
        "asset": {"path": asset_relative.as_posix(), "sha256": request.asset_sha256},
        "qc_report": report.to_manifest(),
        "recipe_hash": recipe.canonical_hash,
        "recipe_provenance": {
            "content_hash": request.recipe_reference.content_hash,
            "logical_id": request.recipe_reference.logical_id,
            "revision": request.recipe_reference.revision,
            "scope": {
                "key": request.recipe_reference.scope.key,
                "kind": request.recipe_reference.scope.kind,
                "namespace": request.recipe_reference.scope.namespace,
            },
            "store_job_id": str(store_job_id),
            "type": request.recipe_reference.artifact_type,
        },
        "schema_version": 1,
    }
    manifest_bytes = _canonical_json_bytes(manifest_value, "promotion manifest")
    manifest_sha256 = _sha256(manifest_bytes)
    result_relative = PurePosixPath("results") / request.job.job_key
    manifest_relative = result_relative / "manifests" / f"{manifest_sha256[7:]}.json"
    current_relative = result_relative / "current.json"
    root_fd = _open_output_root(request.output_root)
    try:
        _install_in_directory(
            root_fd, asset_relative, request.staging_asset, request.asset_sha256, "asset"
        )
        _install_in_directory(
            root_fd, manifest_relative, manifest_bytes, _sha256(manifest_bytes), "manifest"
        )
        current_fd = _open_or_create_directory(root_fd, current_relative.parts[:-1])
        try:
            current_bytes = _canonical_json_bytes(
                {
                    "asset": {"path": asset_relative.as_posix(), "sha256": request.asset_sha256},
                    "manifest": {"path": manifest_relative.as_posix(), "sha256": manifest_sha256},
                    "schema_version": 1,
                },
                "current pointer",
            )
            _atomic_replace(current_fd, current_relative.name, current_bytes)
        finally:
            os.close(current_fd)
    finally:
        os.close(root_fd)
    root = request.output_root.absolute()
    return PromotionResult(
        root.joinpath(*asset_relative.parts),
        root.joinpath(*manifest_relative.parts),
        root.joinpath(*current_relative.parts),
        request.asset_sha256,
        manifest_sha256,
    )


def _validate_request_shape(request: LocalPromotionRequest) -> None:
    _require_secure_os_apis()
    if not isinstance(cast(object, request.job), Job):
        raise LocalPromotionError("job must be a Store Job")
    if not isinstance(cast(object, request.recipe_reference), RecipeReference):
        raise LocalPromotionError("recipe_reference must be a RecipeReference")
    if request.recipe_reference.scope != canonical_recipe_scope(request.job):
        raise LocalPromotionError("recipe_reference scope is not the canonical scope for job")
    _validate_digest(request.asset_sha256, "asset_sha256")
    _validate_namespace_component(request.job.job_key, "job.job_key")
    _validate_namespace_component(request.attempt_id, "attempt_id")
    if not isinstance(cast(object, request.qc_report), QCReport):
        raise LocalPromotionError("qc_report must be a QCReport observation")


def _rehydrate_persisted_recipe(
    request: LocalPromotionRequest, store: PostgresRuntimeStore
) -> tuple[Recipe, UUID]:
    """Resolve the exact artifact again at the visibility boundary.

    A prior renderer read is only staging evidence.  The pointer writer repeats
    the semantic Store read and parse so a caller cannot authorize visibility
    with a raw Recipe object, a supplied hash, or a look-alike reference.
    """
    try:
        persisted = store.read_recipe(request.job, request.recipe_reference)
        report_recipe = request.qc_report.recipe
        if report_recipe is None:
            raise LocalPromotionError("qc_report lacks validated recipe observation")
        recipe = parse_recipe(
            json.loads(persisted.payload_json),
            expected_source_sha256=report_recipe.source_sha256,
            profile=request.job.profile,
        )
    except LocalPromotionError:
        raise
    except (RuntimeStoreError, RecipeValidationError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise LocalPromotionError("persisted recipe provenance is unavailable or invalid") from error
    if recipe.canonical_hash != report_recipe.canonical_hash:
        raise LocalPromotionError("persisted recipe provenance does not match QC recipe")
    return recipe, persisted.job_id


def _reverify_promotion_inputs(request: LocalPromotionRequest, recipe: Recipe) -> QCReport:
    """Do not use a public report's pass flag to authorize promotion."""
    observed_recipe, attempt = request.qc_report.recipe, request.qc_report.attempt
    if observed_recipe is None or attempt is None:
        raise LocalPromotionError("qc_report lacks validated recipe and attempt observations")
    # _rehydrate_persisted_recipe returned a concrete Recipe after comparing it
    # with the observation above; retain this guard for static and defensive use.
    if recipe.canonical_hash != attempt.recipe_hash:
        raise LocalPromotionError("trusted recipe/attempt hash does not match persisted recipe")
    if attempt.output_sha256 != request.asset_sha256:
        raise LocalPromotionError("trusted attempt output digest does not match asset_sha256")
    if attempt.output_path.absolute() != request.staging_asset.absolute():
        raise LocalPromotionError("trusted attempt output path does not match staging_asset")
    report = LocalQC().inspect(recipe, attempt)
    if (
        report.recipe_hash != recipe.canonical_hash
        or report.output_sha256 != request.asset_sha256
        or not report.approved
    ):
        raise LocalPromotionError("independent QC verification rejected staging_asset")
    return report


def _require_secure_os_apis() -> None:
    if not all(hasattr(os, item) for item in ("O_NOFOLLOW", "O_DIRECTORY")) or not all(
        fn in os.supports_dir_fd for fn in (os.open, os.mkdir, os.link, os.rename, os.unlink)
    ):
        raise LocalPromotionError("secure descriptor-relative filesystem APIs are unavailable")


def _open_output_root(root: Path) -> int:
    """Create/open a lexical absolute root without following any symlink."""
    absolute = root.absolute()
    if not absolute.is_absolute() or any(part in {"", ".", ".."} for part in absolute.parts[1:]):
        raise LocalPromotionError("output_root must be an absolute-safe directory path")
    fd = os.open("/", _DIRECTORY_FLAGS)
    try:
        for part in absolute.parts[1:]:
            try:
                os.mkdir(part, mode=0o755, dir_fd=fd)
                os.fsync(fd)
            except FileExistsError:
                pass
            try:
                child = os.open(part, _DIRECTORY_FLAGS, dir_fd=fd)
            except OSError as error:
                raise LocalPromotionError("output_root must be a real directory") from error
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_or_create_directory(root_fd: int, components: Sequence[str]) -> int:
    fd = os.dup(root_fd)
    try:
        for part in components:
            if part in {"", ".", ".."}:
                raise LocalPromotionError("generated directory escapes output_root")
            try:
                os.mkdir(part, mode=0o755, dir_fd=fd)
                os.fsync(fd)
            except FileExistsError:
                pass
            try:
                child = os.open(part, _DIRECTORY_FLAGS, dir_fd=fd)
            except OSError as error:
                raise LocalPromotionError(
                    "generated directory component must be a real directory"
                ) from error
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _install_in_directory(
    root_fd: int, relative: PurePosixPath, source: Path | bytes, expected: str, label: str
) -> None:
    directory_fd = _open_or_create_directory(root_fd, relative.parts[:-1])
    try:
        if isinstance(source, Path):
            _install_asset(directory_fd, relative.name, source, expected)
        else:
            _install_bytes(directory_fd, relative.name, source, expected, label)
    finally:
        os.close(directory_fd)


def _install_asset(directory_fd: int, name: str, source: Path, expected: str) -> None:
    source_fd = _open_regular_source(source)
    temporary_name: str | None = None
    try:
        temporary_fd, temporary_name = _new_temporary(directory_fd, ".asset-")
        digest, size = hashlib.sha256(), 0
        with (
            os.fdopen(source_fd, "rb", closefd=True) as input_stream,
            os.fdopen(temporary_fd, "wb", closefd=True) as output_stream,
        ):
            source_fd = -1
            for chunk in iter(lambda: input_stream.read(_CHUNK_SIZE), b""):
                digest.update(chunk)
                size += len(chunk)
                output_stream.write(chunk)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        if size == 0 or f"sha256:{digest.hexdigest()}" != expected:
            raise LocalPromotionError("staging_asset digest does not match asset_sha256")
        _install_temp_exclusively(directory_fd, temporary_name, name, "asset")
        temporary_name = None
    finally:
        if source_fd >= 0:
            os.close(source_fd)
        if temporary_name is not None:
            _unlink_if_present(directory_fd, temporary_name)


def _open_regular_source(path: Path) -> int:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise LocalPromotionError(
            "staging_asset must be a readable regular non-symlink file"
        ) from error
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise LocalPromotionError("staging_asset must be a regular file")
    return fd


def _install_bytes(directory_fd: int, name: str, value: bytes, expected: str, label: str) -> None:
    if _sha256(value) != expected:
        raise LocalPromotionError(f"invalid immutable {label} digest")
    temporary_fd, temporary_name = _new_temporary(directory_fd, f".{label}-")
    try:
        with os.fdopen(temporary_fd, "wb", closefd=True) as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        _install_temp_exclusively(directory_fd, temporary_name, name, label)
        temporary_name = ""
    finally:
        if temporary_name:
            _unlink_if_present(directory_fd, temporary_name)


def _new_temporary(directory_fd: int, prefix: str) -> tuple[int, str]:
    for _ in range(100):
        name = f"{prefix}{secrets.token_hex(16)}"
        try:
            return os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            ), name
        except FileExistsError:
            continue
        except OSError as error:
            raise LocalPromotionError("could not create promotion temporary file") from error
    raise LocalPromotionError("could not create unique promotion temporary file")


def _install_temp_exclusively(directory_fd: int, temporary: str, target: str, label: str) -> None:
    try:
        os.link(
            temporary,
            target,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
    except FileExistsError:
        if not _existing_matches(directory_fd, temporary, target):
            raise LocalPromotionError(f"conflicting immutable {label} already exists") from None
    except OSError as error:
        raise LocalPromotionError(f"could not install immutable {label}") from error
    else:
        try:
            os.chmod(target, 0o444, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as error:
            raise LocalPromotionError(f"could not seal immutable {label}") from error
        os.fsync(directory_fd)
    _unlink_if_present(directory_fd, temporary)


def _existing_matches(directory_fd: int, temporary: str, target: str) -> bool:
    try:
        target_fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
        temp_fd = os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    except OSError:
        return False
    try:
        status = os.fstat(target_fd)
        return (
            stat.S_ISREG(status.st_mode)
            and status.st_nlink == 1
            and _descriptors_equal(temp_fd, target_fd)
        )
    finally:
        os.close(target_fd)
        os.close(temp_fd)


def _descriptors_equal(left: int, right: int) -> bool:
    while True:
        left_chunk, right_chunk = os.read(left, _CHUNK_SIZE), os.read(right, _CHUNK_SIZE)
        if left_chunk != right_chunk:
            return False
        if not left_chunk:
            return True


def _atomic_replace(directory_fd: int, target: str, value: bytes) -> None:
    temporary_fd, temporary = _new_temporary(directory_fd, ".current-")
    try:
        with os.fdopen(temporary_fd, "wb", closefd=True) as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.rename(temporary, target, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        temporary = ""
        os.fsync(directory_fd)
    except OSError as error:
        raise LocalPromotionError("could not atomically update current pointer") from error
    finally:
        if temporary:
            _unlink_if_present(directory_fd, temporary)


def _unlink_if_present(directory_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=directory_fd)
    except FileNotFoundError:
        pass
    except OSError as error:
        raise LocalPromotionError("could not clean promotion temporary file") from error


def _validate_digest(value: object, field_name: str) -> None:
    if (
        type(value) is not str
        or len(value) != 71
        or not value.startswith(_SHA256_PREFIX)
        or any(c not in "0123456789abcdef" for c in value[7:])
    ):
        raise LocalPromotionError(f"{field_name} must be a lowercase sha256 digest")


def _validate_namespace_component(value: object, field_name: str) -> None:
    if type(value) is not str or _NAMESPACE_COMPONENT.fullmatch(value) is None:
        raise LocalPromotionError(f"{field_name} must be a safe non-empty namespace component")


def _sha256(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _canonical_json_bytes(value: object, field_name: str) -> bytes:
    return json.dumps(
        _json_copy(value, field_name),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")


def _json_copy(value: object, field_name: str) -> Any:
    if value is None or type(value) is bool or type(value) is int or type(value) is str:
        return value  # noqa: E721
    if type(value) is float:
        raise LocalPromotionError(f"{field_name} must not contain floats")  # noqa: E721
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in cast(Mapping[object, object], value).items():
            if type(key) is not str:
                raise LocalPromotionError(f"{field_name} object keys must be strings")
            result[key] = _json_copy(item, f"{field_name}.{key}")
        return result
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return [_json_copy(item, f"{field_name}[]") for item in cast(Sequence[object], value)]
    raise LocalPromotionError(f"{field_name} must contain only JSON values")
