"""Trusted execution boundary for a production A/V render plan.

The logical plan remains host independent.  This module obtains exact source
bytes from the immutable Store, binds private host paths only for one FFmpeg
invocation, and returns path-free attempt facts plus a private output lease.
It deliberately performs no QC or publication.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, Protocol
from uuid import UUID

from ..media.types import canonical_sha256, sha256_prefixed
from ..pipeline.production_recipe import ProductionRecipe
from ..store.models import (
    BlobRef,
    Job,
    MaterializationError,
    MaterializationLimits,
    VerifiedMaterializedBlob,
)
from .production_render_plan import (
    PRODUCTION_AV_H264_AAC_PROFILE,
    ProductionAvRenderProfile,
    ProductionRenderPlan,
    ProductionRenderPlanError,
    bind_production_render_invocation,
    build_production_render_plan,
)

PRODUCTION_RENDER_ATTEMPT_SCHEMA_VERSION: Final = "production-render-attempt-v1"
PRODUCTION_RENDER_EXECUTION_SCHEMA_VERSION: Final = "production-ffmpeg-execution-v1"
_OUTPUT_MEDIA_TYPE: Final = "video/mp4"
_TOOL_VERSION_MAX_BYTES: Final = 64 * 1024
_HASH_CHUNK_BYTES: Final = 1024 * 1024


class ProductionRenderExecutionError(RuntimeError):
    """Closed failure at the private production-render execution boundary."""

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        outcome: Literal["denied", "failed"],
    ) -> None:
        if code not in {
            "PRODUCTION_RENDER_REQUEST_INVALID",
            "PRODUCTION_RENDER_TOOL_UNAVAILABLE",
            "PRODUCTION_RENDER_TOOL_IDENTITY_FAILED",
            "PRODUCTION_RENDER_SOURCE_MATERIALIZATION_DENIED",
            "PRODUCTION_RENDER_SOURCE_MATERIALIZATION_FAILED",
            "PRODUCTION_RENDER_SOURCE_INTEGRITY_FAILED",
            "PRODUCTION_RENDER_EXECUTION_FAILED",
            "PRODUCTION_RENDER_TIMEOUT",
            "PRODUCTION_RENDER_STDERR_LIMIT_EXCEEDED",
            "PRODUCTION_RENDER_OUTPUT_INVALID",
            "PRODUCTION_RENDER_OUTPUT_LIMIT_EXCEEDED",
            "PRODUCTION_RENDER_CLEANUP_FAILED",
        }:
            raise ValueError("production render failure code is unsupported")
        self.code = code
        self.detail = detail
        self.outcome: Literal["denied", "failed"] = outcome
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class ProductionRenderExecutionLimits:
    """Explicit host-resource ceilings for one private render attempt."""

    max_source_bytes: int
    copy_chunk_bytes: int
    staging_quota_bytes: int
    max_output_bytes: int
    max_input_count: int
    max_segment_count: int
    stderr_max_bytes: int
    timeout_milliseconds: int

    def __post_init__(self) -> None:
        for name in (
            "max_source_bytes",
            "copy_chunk_bytes",
            "staging_quota_bytes",
            "max_output_bytes",
            "max_input_count",
            "max_segment_count",
            "stderr_max_bytes",
            "timeout_milliseconds",
        ):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:  # noqa: E721
                raise ValueError(f"production render {name} must be a positive integer")
        if self.copy_chunk_bytes > self.max_source_bytes:
            raise ValueError("production render copy_chunk_bytes exceeds max_source_bytes")

    @property
    def materialization(self) -> MaterializationLimits:
        """Project generic render ceilings onto the Store's bounded blob lease API."""

        return MaterializationLimits(
            max_source_bytes=self.max_source_bytes,
            timed_speech_max_request_bytes=self.max_source_bytes,
            copy_chunk_bytes=self.copy_chunk_bytes,
            staging_quota_bytes=self.staging_quota_bytes,
        )

    def to_mapping(self) -> dict[str, int]:
        return {
            "copy_chunk_bytes": self.copy_chunk_bytes,
            "max_input_count": self.max_input_count,
            "max_output_bytes": self.max_output_bytes,
            "max_segment_count": self.max_segment_count,
            "max_source_bytes": self.max_source_bytes,
            "staging_quota_bytes": self.staging_quota_bytes,
            "stderr_max_bytes": self.stderr_max_bytes,
            "timeout_milliseconds": self.timeout_milliseconds,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


class ProductionRenderSourceStore(Protocol):
    """Smallest Store port needed by the trusted production executor."""

    def materialize_immutable_blob(
        self,
        job: Job,
        reference: BlobRef,
        limits: MaterializationLimits,
    ) -> VerifiedMaterializedBlob: ...


@dataclass(frozen=True, slots=True)
class ProductionFfmpegIdentity:
    """Content identity of the executable and its exact version response."""

    executable_sha256: str
    executable_byte_length: int
    version_output_sha256: str

    def __post_init__(self) -> None:
        sha256_prefixed(self.executable_sha256, "production ffmpeg executable_sha256")
        if type(self.executable_byte_length) is not int or self.executable_byte_length <= 0:  # noqa: E721
            raise ValueError("production ffmpeg executable_byte_length must be positive")
        sha256_prefixed(
            self.version_output_sha256,
            "production ffmpeg version_output_sha256",
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "executable_byte_length": self.executable_byte_length,
            "executable_sha256": self.executable_sha256,
            "version_output_sha256": self.version_output_sha256,
        }


@dataclass(frozen=True, slots=True)
class ProductionRenderAttemptFacts:
    """Persistable render facts; host paths and approval state are excluded."""

    attempt_id: UUID
    job: Job
    story_id: str
    recipe_sha256: str
    plan_sha256: str
    profile_sha256: str
    execution_limits_sha256: str
    input_authority_sha256: str
    input_count: int
    segment_count: int
    ffmpeg: ProductionFfmpegIdentity
    stderr_sha256: str
    output_sha256: str
    output_byte_length: int
    output_media_type: str = _OUTPUT_MEDIA_TYPE

    def __post_init__(self) -> None:
        if type(self.attempt_id) is not UUID:  # noqa: E721
            raise ValueError("production render attempt_id must be a UUID")
        if type(self.job) is not Job:  # noqa: E721
            raise ValueError("production render attempt requires an exact Job")
        if type(self.story_id) is not str or not self.story_id:
            raise ValueError("production render story_id must be non-empty text")
        for name in (
            "recipe_sha256",
            "plan_sha256",
            "profile_sha256",
            "execution_limits_sha256",
            "input_authority_sha256",
            "stderr_sha256",
            "output_sha256",
        ):
            sha256_prefixed(getattr(self, name), f"production render attempt {name}")
        for name in ("input_count", "segment_count", "output_byte_length"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:  # noqa: E721
                raise ValueError(f"production render attempt {name} must be positive")
        if type(self.ffmpeg) is not ProductionFfmpegIdentity:  # noqa: E721
            raise ValueError("production render attempt requires exact FFmpeg identity")
        if self.output_media_type != _OUTPUT_MEDIA_TYPE:
            raise ValueError("production render attempt output media type is unsupported")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": PRODUCTION_RENDER_ATTEMPT_SCHEMA_VERSION,
            "execution_schema_version": PRODUCTION_RENDER_EXECUTION_SCHEMA_VERSION,
            "attempt_id": str(self.attempt_id),
            "job": {"job_key": self.job.job_key, "profile": self.job.profile},
            "story_id": self.story_id,
            "recipe_sha256": self.recipe_sha256,
            "plan_sha256": self.plan_sha256,
            "profile_sha256": self.profile_sha256,
            "execution_limits_sha256": self.execution_limits_sha256,
            "input_authority_sha256": self.input_authority_sha256,
            "input_count": self.input_count,
            "segment_count": self.segment_count,
            "ffmpeg": self.ffmpeg.to_mapping(),
            "stderr_sha256": self.stderr_sha256,
            "output": {
                "content_hash": self.output_sha256,
                "byte_length": self.output_byte_length,
                "media_type": self.output_media_type,
            },
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(slots=True)
class VerifiedProductionRender:
    """Private output lease returned only after a complete trusted execution."""

    facts: ProductionRenderAttemptFacts
    output_path: Path
    _directory: Path
    _closed: bool = False

    def close(self) -> None:
        if self._closed:
            return
        try:
            _discard_attempt_directory(self._directory)
        except OSError as error:
            raise ProductionRenderExecutionError(
                "PRODUCTION_RENDER_CLEANUP_FAILED",
                "private production render cleanup failed",
                outcome="failed",
            ) from error
        self._closed = True


@dataclass(frozen=True, slots=True)
class ProductionProcessResult:
    """Bounded subprocess result returned by the execution adapter."""

    returncode: int
    stdout: bytes
    stderr: bytes
    stdout_limit_exceeded: bool = False
    stderr_limit_exceeded: bool = False

    def __post_init__(self) -> None:
        if type(self.returncode) is not int:  # noqa: E721
            raise ValueError("production process returncode must be an integer")
        if type(self.stdout) is not bytes or type(self.stderr) is not bytes:  # noqa: E721
            raise ValueError("production process output must be bytes")
        if (  # noqa: E721
            type(self.stdout_limit_exceeded) is not bool
            or type(self.stderr_limit_exceeded) is not bool
        ):
            raise ValueError("production process limit state must be boolean")


class ProductionProcessRunner(Protocol):
    def __call__(
        self,
        argv: tuple[str, ...],
        *,
        timeout_milliseconds: int,
        stdout_max_bytes: int,
        stderr_max_bytes: int,
        pass_fds: tuple[int, ...],
    ) -> ProductionProcessResult: ...


class ProductionFFmpegRenderer:
    """Materialize, verify, and execute one closed production render plan."""

    def __init__(
        self,
        executable: str | None = None,
        *,
        runner: ProductionProcessRunner | None = None,
    ) -> None:
        self._executable = _resolve_ffmpeg_path(executable)
        if runner is None:
            runner = _run_bounded_process
        if not callable(runner):
            raise ValueError("production render runner must be callable")
        self._runner = runner

    @property
    def executable(self) -> Path:
        return self._executable

    def execute(
        self,
        *,
        attempt_id: UUID,
        job: Job,
        recipe: ProductionRecipe,
        plan: ProductionRenderPlan,
        store: ProductionRenderSourceStore,
        staging_root: Path,
        limits: ProductionRenderExecutionLimits,
        profile: ProductionAvRenderProfile = PRODUCTION_AV_H264_AAC_PROFILE,
    ) -> VerifiedProductionRender:
        """Execute only an independently reproducible plan over exact Store bytes."""

        _require_request(attempt_id, job, recipe, plan, limits, profile)
        try:
            trusted_plan = build_production_render_plan(recipe, profile=profile)
        except ProductionRenderPlanError as error:
            raise ProductionRenderExecutionError(
                "PRODUCTION_RENDER_REQUEST_INVALID",
                "production Recipe cannot be projected through the closed render profile",
                outcome="denied",
            ) from error
        if plan != trusted_plan:
            raise ProductionRenderExecutionError(
                "PRODUCTION_RENDER_REQUEST_INVALID",
                "production render plan differs from independent Recipe projection",
                outcome="denied",
            )
        if (
            len(plan.inputs) > limits.max_input_count
            or len(plan.segments) > limits.max_segment_count
        ):
            raise ProductionRenderExecutionError(
                "PRODUCTION_RENDER_REQUEST_INVALID",
                "production render plan exceeds explicit input or segment limits",
                outcome="denied",
            )
        root = _staging_root(staging_root)
        attempt_directory: Path | None = None
        try:
            attempt_directory = _create_attempt_directory(root, attempt_id)
            output_path = attempt_directory / "asset.mp4"
            pinned_executable, tool_identity = _pin_ffmpeg_executable(
                self._executable,
                attempt_directory / "ffmpeg",
                self._runner,
            )
            with ExitStack() as leases:
                source_paths: dict[BlobRef, Path] = {}
                source_inodes: set[tuple[int, int]] = set()
                source_descriptors: dict[BlobRef, int] = {}
                for item in plan.inputs:
                    try:
                        lease = store.materialize_immutable_blob(
                            job,
                            item.source_blob,
                            limits.materialization,
                        )
                    except MaterializationError as error:
                        code = (
                            "PRODUCTION_RENDER_SOURCE_MATERIALIZATION_DENIED"
                            if error.outcome == "denied"
                            else "PRODUCTION_RENDER_SOURCE_MATERIALIZATION_FAILED"
                        )
                        raise ProductionRenderExecutionError(
                            code,
                            f"source materialization failed ({error.code})",
                            outcome=error.outcome,
                        ) from error
                    except Exception as error:
                        raise ProductionRenderExecutionError(
                            "PRODUCTION_RENDER_SOURCE_MATERIALIZATION_FAILED",
                            "source materialization failed before FFmpeg execution",
                            outcome="failed",
                        ) from error
                    if not callable(getattr(lease, "close", None)):
                        raise ProductionRenderExecutionError(
                            "PRODUCTION_RENDER_SOURCE_MATERIALIZATION_FAILED",
                            "source materialization returned an invalid lease",
                            outcome="failed",
                        )
                    leases.callback(lease.close)
                    if lease.reference != item.source_blob:
                        raise ProductionRenderExecutionError(
                            "PRODUCTION_RENDER_SOURCE_INTEGRITY_FAILED",
                            "materialized source reference differs from the requested BlobRef",
                            outcome="failed",
                        )
                    descriptor, inode = _open_verified_source(
                        lease.path,
                        item.source_blob.content_hash,
                        item.source_blob.byte_length,
                        label="materialized source",
                    )
                    leases.callback(os.close, descriptor)
                    if inode in source_inodes:
                        raise ProductionRenderExecutionError(
                            "PRODUCTION_RENDER_SOURCE_INTEGRITY_FAILED",
                            "distinct production BlobRefs alias one materialized file",
                            outcome="failed",
                        )
                    source_inodes.add(inode)
                    source_descriptors[item.source_blob] = descriptor
                    source_paths[item.source_blob] = Path(f"/dev/fd/{descriptor}")

                try:
                    invocation = bind_production_render_invocation(
                        plan,
                        source_paths=source_paths,
                        output_path=output_path,
                        profile=profile,
                    )
                except ProductionRenderPlanError as error:
                    raise ProductionRenderExecutionError(
                        "PRODUCTION_RENDER_REQUEST_INVALID",
                        "production render invocation cannot be bound to verified inputs",
                        outcome="denied",
                    ) from error
                bounded_argv = (
                    *invocation.argv[:-1],
                    "-fs",
                    str(limits.max_output_bytes),
                    invocation.argv[-1],
                )
                try:
                    completed = self._runner(
                        (str(pinned_executable), *bounded_argv[1:]),
                        timeout_milliseconds=limits.timeout_milliseconds,
                        stdout_max_bytes=0,
                        stderr_max_bytes=limits.stderr_max_bytes,
                        pass_fds=tuple(source_descriptors.values()),
                    )
                except subprocess.TimeoutExpired as error:
                    raise ProductionRenderExecutionError(
                        "PRODUCTION_RENDER_TIMEOUT",
                        "production FFmpeg execution exceeded its explicit timeout",
                        outcome="failed",
                    ) from error
                except OSError as error:
                    raise ProductionRenderExecutionError(
                        "PRODUCTION_RENDER_EXECUTION_FAILED",
                        "production FFmpeg process could not be executed",
                        outcome="failed",
                    ) from error
                except Exception as error:
                    raise ProductionRenderExecutionError(
                        "PRODUCTION_RENDER_EXECUTION_FAILED",
                        "production FFmpeg runner failed without a valid result",
                        outcome="failed",
                    ) from error
                if type(completed) is not ProductionProcessResult:  # noqa: E721
                    raise ProductionRenderExecutionError(
                        "PRODUCTION_RENDER_EXECUTION_FAILED",
                        "production FFmpeg runner returned an invalid result",
                        outcome="failed",
                    )
                stderr = completed.stderr
                if completed.stdout_limit_exceeded or completed.stderr_limit_exceeded:
                    raise ProductionRenderExecutionError(
                        "PRODUCTION_RENDER_STDERR_LIMIT_EXCEEDED",
                        "production FFmpeg stderr exceeded its explicit byte limit",
                        outcome="failed",
                    )
                if completed.returncode != 0:
                    raise ProductionRenderExecutionError(
                        "PRODUCTION_RENDER_EXECUTION_FAILED",
                        "production FFmpeg returned a non-zero exit status",
                        outcome="failed",
                    )
                output_inode = _verify_output(output_path, limits.max_output_bytes)
                if output_inode in source_inodes:
                    raise ProductionRenderExecutionError(
                        "PRODUCTION_RENDER_OUTPUT_INVALID",
                        "production render output aliases an immutable source",
                        outcome="failed",
                    )
                try:
                    output_sha256, output_length = _sha256_file(output_path)
                    final_output_status = output_path.lstat()
                except OSError as error:
                    raise ProductionRenderExecutionError(
                        "PRODUCTION_RENDER_OUTPUT_INVALID",
                        "production render output could not be hashed",
                        outcome="failed",
                    ) from error
                if (
                    not stat.S_ISREG(final_output_status.st_mode)
                    or final_output_status.st_nlink != 1
                    or (final_output_status.st_dev, final_output_status.st_ino) != output_inode
                    or final_output_status.st_size != output_length
                    or output_length > limits.max_output_bytes
                ):
                    raise ProductionRenderExecutionError(
                        "PRODUCTION_RENDER_OUTPUT_INVALID",
                        "production render output changed while its identity was computed",
                        outcome="failed",
                    )
                for item in plan.inputs:
                    _verify_exact_descriptor(
                        source_descriptors[item.source_blob],
                        item.source_blob.content_hash,
                        item.source_blob.byte_length,
                        label="materialized source after rendering",
                    )
                _verify_pinned_executable(
                    pinned_executable,
                    tool_identity.executable_sha256,
                    tool_identity.executable_byte_length,
                )
                try:
                    output_path.chmod(0o400)
                    pinned_executable.unlink()
                except OSError as error:
                    raise ProductionRenderExecutionError(
                        "PRODUCTION_RENDER_OUTPUT_INVALID",
                        "production render output could not be sealed",
                        outcome="failed",
                    ) from error
                facts = ProductionRenderAttemptFacts(
                    attempt_id=attempt_id,
                    job=job,
                    story_id=recipe.story.story_id,
                    recipe_sha256=recipe.canonical_hash,
                    plan_sha256=plan.canonical_hash,
                    profile_sha256=profile.canonical_hash,
                    execution_limits_sha256=limits.canonical_hash,
                    input_authority_sha256=canonical_sha256(
                        [item.to_mapping() for item in plan.inputs]
                    ),
                    input_count=len(plan.inputs),
                    segment_count=len(plan.segments),
                    ffmpeg=tool_identity,
                    stderr_sha256=_sha256_bytes(stderr),
                    output_sha256=output_sha256,
                    output_byte_length=output_length,
                )
            return VerifiedProductionRender(facts, output_path, attempt_directory)
        except Exception as error:
            if attempt_directory is not None:
                try:
                    _discard_attempt_directory(attempt_directory)
                except OSError as cleanup_error:
                    raise ProductionRenderExecutionError(
                        "PRODUCTION_RENDER_CLEANUP_FAILED",
                        "failed production render left an unclean private attempt directory",
                        outcome="failed",
                    ) from cleanup_error
            if isinstance(error, ProductionRenderExecutionError):
                raise
            raise ProductionRenderExecutionError(
                "PRODUCTION_RENDER_CLEANUP_FAILED",
                "private production render resource cleanup failed",
                outcome="failed",
            ) from error


def _require_request(
    attempt_id: object,
    job: object,
    recipe: object,
    plan: object,
    limits: object,
    profile: object,
) -> None:
    if (
        type(attempt_id) is not UUID
        or type(job) is not Job
        or type(recipe) is not ProductionRecipe
        or type(plan) is not ProductionRenderPlan
        or type(limits) is not ProductionRenderExecutionLimits
        or type(profile) is not ProductionAvRenderProfile
    ):
        raise ProductionRenderExecutionError(
            "PRODUCTION_RENDER_REQUEST_INVALID",
            "production render requires exact request, Recipe, plan, limits, and profile types",
            outcome="denied",
        )


def _resolve_ffmpeg_path(executable: str | None) -> Path:
    selected = shutil.which(executable or "ffmpeg")
    if selected is None:
        raise ProductionRenderExecutionError(
            "PRODUCTION_RENDER_TOOL_UNAVAILABLE",
            "FFmpeg executable is unavailable",
            outcome="failed",
        )
    try:
        path = Path(selected).resolve(strict=True)
        status = path.lstat()
        if not stat.S_ISREG(status.st_mode):
            raise OSError("FFmpeg executable is not a regular file")
        return path
    except OSError as error:
        raise ProductionRenderExecutionError(
            "PRODUCTION_RENDER_TOOL_IDENTITY_FAILED",
            "FFmpeg executable identity could not be verified",
            outcome="failed",
        ) from error


def _pin_ffmpeg_executable(
    source: Path,
    destination: Path,
    runner: ProductionProcessRunner,
) -> tuple[Path, ProductionFfmpegIdentity]:
    try:
        executable_sha256, executable_byte_length = _copy_regular_file(source, destination)
        version = runner(
            (str(destination), "-version"),
            timeout_milliseconds=10_000,
            stdout_max_bytes=_TOOL_VERSION_MAX_BYTES,
            stderr_max_bytes=_TOOL_VERSION_MAX_BYTES,
            pass_fds=(),
        )
    except Exception as error:
        if isinstance(error, ProductionRenderExecutionError):
            raise
        raise ProductionRenderExecutionError(
            "PRODUCTION_RENDER_TOOL_IDENTITY_FAILED",
            "FFmpeg executable identity could not be verified",
            outcome="failed",
        ) from error
    if (
        type(version) is not ProductionProcessResult
        or version.returncode != 0
        or version.stdout_limit_exceeded
        or version.stderr_limit_exceeded
    ):
        raise ProductionRenderExecutionError(
            "PRODUCTION_RENDER_TOOL_IDENTITY_FAILED",
            "FFmpeg executable returned an invalid bounded version response",
            outcome="failed",
        )
    identity = ProductionFfmpegIdentity(
        executable_sha256,
        executable_byte_length,
        _sha256_bytes(version.stdout + b"\0" + version.stderr),
    )
    return destination, identity


def _copy_regular_file(source: Path, destination: Path) -> tuple[str, int]:
    source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    destination_fd: int | None = None
    try:
        source_status = os.fstat(source_fd)
        if not stat.S_ISREG(source_status.st_mode):
            raise OSError("FFmpeg source is not a regular file")
        destination_fd = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o500,
        )
        digest = hashlib.sha256()
        length = 0
        while chunk := os.read(source_fd, _HASH_CHUNK_BYTES):
            digest.update(chunk)
            length += len(chunk)
            written = 0
            while written < len(chunk):
                written += os.write(destination_fd, chunk[written:])
        os.fsync(destination_fd)
        os.fchmod(destination_fd, 0o500)
        destination_status = os.fstat(destination_fd)
        final_source_status = os.fstat(source_fd)
        if (
            destination_status.st_size != length
            or destination_status.st_nlink != 1
            or not stat.S_ISREG(destination_status.st_mode)
            or (source_status.st_dev, source_status.st_ino, source_status.st_size)
            != (
                final_source_status.st_dev,
                final_source_status.st_ino,
                final_source_status.st_size,
            )
        ):
            raise OSError("FFmpeg executable changed while it was pinned")
        return f"sha256:{digest.hexdigest()}", length
    finally:
        if destination_fd is not None:
            os.close(destination_fd)
        os.close(source_fd)


def _run_bounded_process(
    argv: tuple[str, ...],
    *,
    timeout_milliseconds: int,
    stdout_max_bytes: int,
    stderr_max_bytes: int,
    pass_fds: tuple[int, ...],
) -> ProductionProcessResult:
    """Execute shell-free and kill the process group when either stream exceeds its cap."""

    if (
        type(timeout_milliseconds) is not int
        or timeout_milliseconds <= 0
        or type(stdout_max_bytes) is not int
        or stdout_max_bytes < 0
        or type(stderr_max_bytes) is not int
        or stderr_max_bytes <= 0
        or any(type(value) is not int or value < 0 for value in pass_fds)
        or len(pass_fds) != len(set(pass_fds))
    ):
        raise ValueError("bounded process limits or inherited descriptors are invalid")

    process = subprocess.Popen(  # noqa: S603 - argv is built by the closed plan binder.
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if stdout_max_bytes else subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
        pass_fds=pass_fds,
    )
    stdout_pipe = process.stdout
    stderr_pipe = process.stderr
    if stderr_pipe is None:  # pragma: no cover - guaranteed by stderr=PIPE.
        _kill_process_group(process)
        process.wait()
        raise OSError("production FFmpeg stderr pipe is unavailable")
    stdout = bytearray()
    stderr = bytearray()
    stdout_limit_exceeded = threading.Event()
    stderr_limit_exceeded = threading.Event()
    reader_error: list[OSError] = []

    def read_stream(
        descriptor: int,
        captured: bytearray,
        limit: int,
        exceeded: threading.Event,
    ) -> None:
        try:
            while chunk := os.read(descriptor, 64 * 1024):
                remaining = max(0, limit - len(captured))
                captured.extend(chunk[:remaining])
                if len(chunk) > remaining and not exceeded.is_set():
                    exceeded.set()
                    try:
                        _kill_process_group(process)
                    except ProcessLookupError:
                        pass
        except OSError as error:
            reader_error.append(error)

    readers = [
        threading.Thread(
            target=read_stream,
            args=(stderr_pipe.fileno(), stderr, stderr_max_bytes, stderr_limit_exceeded),
            name="production-process-stderr",
            daemon=True,
        )
    ]
    if stdout_pipe is not None:
        readers.append(
            threading.Thread(
                target=read_stream,
                args=(stdout_pipe.fileno(), stdout, stdout_max_bytes, stdout_limit_exceeded),
                name="production-process-stdout",
                daemon=True,
            )
        )
    for reader in readers:
        reader.start()
    try:
        returncode = process.wait(timeout=timeout_milliseconds / 1000)
    except subprocess.TimeoutExpired:
        _kill_process_group(process)
        process.wait()
        _join_process_readers(process, readers)
        raise
    _join_process_readers(process, readers)
    if reader_error:
        raise reader_error[0]
    return ProductionProcessResult(
        returncode,
        bytes(stdout),
        bytes(stderr),
        stdout_limit_exceeded.is_set(),
        stderr_limit_exceeded.is_set(),
    )


def _join_process_readers(
    process: subprocess.Popen[bytes],
    readers: list[threading.Thread],
) -> None:
    for reader in readers:
        reader.join(timeout=0.25)
    if any(reader.is_alive() for reader in readers):
        _kill_process_group(process)
        for reader in readers:
            reader.join(timeout=1)
    if any(reader.is_alive() for reader in readers):
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
        raise OSError("production process output readers did not terminate")


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    """Terminate FFmpeg and any helper children holding inherited descriptors."""

    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def _staging_root(value: object) -> Path:
    if not isinstance(value, Path) or not value.is_absolute():
        raise ProductionRenderExecutionError(
            "PRODUCTION_RENDER_REQUEST_INVALID",
            "production render staging_root must be an absolute pathlib.Path",
            outcome="denied",
        )
    try:
        value.mkdir(parents=True, mode=0o700, exist_ok=True)
        status = value.lstat()
        if (
            not stat.S_ISDIR(status.st_mode)
            or stat.S_ISLNK(status.st_mode)
            or status.st_uid != os.geteuid()
            or status.st_mode & 0o077
        ):
            raise OSError("staging root is not a real directory")
        return value.resolve(strict=True)
    except OSError as error:
        raise ProductionRenderExecutionError(
            "PRODUCTION_RENDER_REQUEST_INVALID",
            "production render staging_root is unavailable or unsafe",
            outcome="denied",
        ) from error


def _create_attempt_directory(root: Path, attempt_id: UUID) -> Path:
    directory: Path | None = None
    try:
        directory = Path(tempfile.mkdtemp(prefix=f"render-{attempt_id}-", dir=root))
        status = directory.lstat()
        if (
            not stat.S_ISDIR(status.st_mode)
            or stat.S_ISLNK(status.st_mode)
            or status.st_uid != os.geteuid()
            or status.st_mode & 0o077
            or directory.parent.resolve(strict=True) != root
        ):
            raise OSError("render attempt directory is unsafe")
        return directory
    except OSError as error:
        if directory is not None:
            try:
                os.rmdir(directory)
            except OSError as cleanup_error:
                raise ProductionRenderExecutionError(
                    "PRODUCTION_RENDER_CLEANUP_FAILED",
                    "unsafe production render attempt directory could not be removed",
                    outcome="failed",
                ) from cleanup_error
        raise ProductionRenderExecutionError(
            "PRODUCTION_RENDER_EXECUTION_FAILED",
            "private production render attempt directory could not be allocated safely",
            outcome="failed",
        ) from error


def _open_verified_source(
    path: object,
    expected_sha256: str,
    expected_length: int,
    *,
    label: str,
) -> tuple[int, tuple[int, int]]:
    if not isinstance(path, Path) or not path.is_absolute():
        raise ProductionRenderExecutionError(
            "PRODUCTION_RENDER_SOURCE_INTEGRITY_FAILED",
            f"{label} path is not an absolute pathlib.Path",
            outcome="failed",
        )
    try:
        parent_status = path.parent.lstat()
        if (
            not stat.S_ISDIR(parent_status.st_mode)
            or stat.S_ISLNK(parent_status.st_mode)
            or parent_status.st_uid != os.geteuid()
            or parent_status.st_mode & 0o077
        ):
            raise OSError("source parent is not private")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise ProductionRenderExecutionError(
            "PRODUCTION_RENDER_SOURCE_INTEGRITY_FAILED",
            f"{label} is not a readable single-link regular file",
            outcome="failed",
        ) from error
    try:
        inode = _verify_exact_descriptor(
            descriptor,
            expected_sha256,
            expected_length,
            label=label,
        )
        return descriptor, inode
    except Exception:
        os.close(descriptor)
        raise


def _verify_exact_descriptor(
    descriptor: int,
    expected_sha256: str,
    expected_length: int,
    *,
    label: str,
) -> tuple[int, int]:
    try:
        status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_nlink != 1
            or status.st_uid != os.geteuid()
            or status.st_mode & 0o077
        ):
            raise OSError("source descriptor topology is unsafe")
        os.lseek(descriptor, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        length = 0
        while chunk := os.read(descriptor, _HASH_CHUNK_BYTES):
            digest.update(chunk)
            length += len(chunk)
        os.lseek(descriptor, 0, os.SEEK_SET)
        final_status = os.fstat(descriptor)
    except OSError as error:
        raise ProductionRenderExecutionError(
            "PRODUCTION_RENDER_SOURCE_INTEGRITY_FAILED",
            f"{label} could not be verified through its pinned descriptor",
            outcome="failed",
        ) from error
    if (
        f"sha256:{digest.hexdigest()}" != expected_sha256
        or length != expected_length
        or (final_status.st_dev, final_status.st_ino, final_status.st_size)
        != (status.st_dev, status.st_ino, status.st_size)
        or not stat.S_ISREG(final_status.st_mode)
        or final_status.st_nlink != 1
    ):
        raise ProductionRenderExecutionError(
            "PRODUCTION_RENDER_SOURCE_INTEGRITY_FAILED",
            f"{label} bytes differ from the exact immutable BlobRef",
            outcome="failed",
        )
    return status.st_dev, status.st_ino


def _verify_pinned_executable(
    path: Path,
    expected_sha256: str,
    expected_length: int,
) -> None:
    try:
        status = path.lstat()
        actual_sha256, actual_length = _sha256_file(path)
        final_status = path.lstat()
    except OSError as error:
        raise ProductionRenderExecutionError(
            "PRODUCTION_RENDER_TOOL_IDENTITY_FAILED",
            "pinned FFmpeg executable could not be reverified",
            outcome="failed",
        ) from error
    if (
        status.st_uid != os.geteuid()
        or status.st_mode & 0o077
        or not stat.S_ISREG(status.st_mode)
        or status.st_nlink != 1
        or actual_sha256 != expected_sha256
        or actual_length != expected_length
        or (final_status.st_dev, final_status.st_ino, final_status.st_size)
        != (status.st_dev, status.st_ino, status.st_size)
    ):
        raise ProductionRenderExecutionError(
            "PRODUCTION_RENDER_TOOL_IDENTITY_FAILED",
            "pinned FFmpeg executable identity changed during execution",
            outcome="failed",
        )


def _verify_output(path: Path, max_output_bytes: int) -> tuple[int, int]:
    try:
        status = path.lstat()
    except OSError as error:
        raise ProductionRenderExecutionError(
            "PRODUCTION_RENDER_OUTPUT_INVALID",
            "production FFmpeg did not create a readable output",
            outcome="failed",
        ) from error
    if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1 or status.st_size <= 0:
        raise ProductionRenderExecutionError(
            "PRODUCTION_RENDER_OUTPUT_INVALID",
            "production FFmpeg output is not one non-empty regular file",
            outcome="failed",
        )
    if status.st_size > max_output_bytes:
        raise ProductionRenderExecutionError(
            "PRODUCTION_RENDER_OUTPUT_LIMIT_EXCEEDED",
            "production FFmpeg output exceeded its explicit byte limit",
            outcome="failed",
        )
    return status.st_dev, status.st_ino


def _discard_attempt_directory(directory: Path) -> None:
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name in ("asset.mp4", "ffmpeg"):
            try:
                os.unlink(name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    os.rmdir(directory)


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    length = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(_HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
            length += len(chunk)
    return f"sha256:{digest.hexdigest()}", length


def _sha256_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


__all__ = (
    "PRODUCTION_RENDER_ATTEMPT_SCHEMA_VERSION",
    "PRODUCTION_RENDER_EXECUTION_SCHEMA_VERSION",
    "ProductionFFmpegRenderer",
    "ProductionFfmpegIdentity",
    "ProductionProcessResult",
    "ProductionProcessRunner",
    "ProductionRenderAttemptFacts",
    "ProductionRenderExecutionError",
    "ProductionRenderExecutionLimits",
    "ProductionRenderSourceStore",
    "VerifiedProductionRender",
)
