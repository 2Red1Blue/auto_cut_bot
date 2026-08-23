"""The local, argv-only execution boundary for a :class:`RenderPlan`."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..media.types import sha256_prefixed
from .models import H264_MP4_VIDEO_PROFILE, Recipe, RenderPlan
from .render_plan import build_render_plan

_CHUNK_SIZE = 1024 * 1024
_MAX_STDERR_BYTES = 16 * 1024


class RenderError(RuntimeError):
    """Base error for a local render that did not produce an immutable attempt."""


class FFmpegUnavailableError(RenderError):
    """Raised when the required ffmpeg executable cannot be found."""


class SourceIdentityMismatchError(RenderError):
    """Raised when source bytes differ from the recipe before or after rendering."""


class FFmpegExecutionError(RenderError):
    """Raised when ffmpeg fails, times out, or cannot be started."""


Runner = Callable[..., subprocess.CompletedProcess[bytes]]


@dataclass(frozen=True, slots=True)
class RenderAttempt:
    """Facts about one completed local render, with no mutable approval field."""

    recipe_hash: str
    profile_hash: str
    source_sha256: str
    output_path: Path
    output_sha256: str
    output_byte_size: int
    ffmpeg_argv: tuple[str, ...]
    stderr_sha256: str

    def __post_init__(self) -> None:
        sha256_prefixed(self.recipe_hash, "render_attempt.recipe_hash")
        sha256_prefixed(self.profile_hash, "render_attempt.profile_hash")
        sha256_prefixed(self.source_sha256, "render_attempt.source_sha256")
        sha256_prefixed(self.output_sha256, "render_attempt.output_sha256")
        sha256_prefixed(self.stderr_sha256, "render_attempt.stderr_sha256")
        if self.output_byte_size <= 0:
            raise ValueError("render_attempt.output_byte_size must be positive")
        if not self.ffmpeg_argv or self.ffmpeg_argv[0] != "ffmpeg":
            raise ValueError("render_attempt.ffmpeg_argv must invoke ffmpeg directly")


class FFmpegRenderer:
    """Render a verified source into a private staging directory using argv only."""

    def __init__(
        self,
        executable: str | None = None,
        *,
        timeout_seconds: float = 120.0,
        runner: Runner = subprocess.run,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        resolved = executable or shutil.which("ffmpeg")
        if not resolved:
            raise FFmpegUnavailableError("ffmpeg executable is unavailable")
        self._executable = resolved
        self._timeout_seconds = timeout_seconds
        self._runner = runner

    @property
    def executable(self) -> str:
        return self._executable

    def render(self, recipe: Recipe, plan: RenderPlan, *, source_path: Path, staging_root: Path) -> RenderAttempt:
        """Execute ``plan`` after checking source identity before and after encoding.

        The caller owns ``staging_root``.  Each attempt gets a newly-created
        subdirectory below it, so a successful output is never overwritten.
        """
        if plan.recipe_hash != recipe.canonical_hash:
            raise RenderError("render plan recipe hash does not match recipe")
        source = _regular_path(source_path, "source_path")
        source_sha256, source_size = _sha256_file(source)
        if source_sha256 != recipe.source_sha256 or source_size != recipe.source_byte_size:
            raise SourceIdentityMismatchError("source identity does not match recipe before rendering")
        root = _staging_root(staging_root)
        attempt_dir = Path(tempfile.mkdtemp(prefix="render-", dir=root))
        staged_source = attempt_dir / "source.mp4"
        output = attempt_dir / "asset.mp4"
        _require_trusted_plan(plan, recipe, source)
        _copy_verified_source(source, staged_source, recipe)
        argv = build_render_plan(recipe, source_path=staged_source, output_path=output).argv
        try:
            completed = self._runner(
                [self._executable, *argv[1:]], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                check=False, timeout=self._timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            raise FFmpegExecutionError("ffmpeg timed out") from error
        except OSError as error:
            raise FFmpegExecutionError("ffmpeg could not be executed") from error
        if completed.returncode != 0:
            detail = completed.stderr[:_MAX_STDERR_BYTES].decode("utf-8", "replace").strip()
            raise FFmpegExecutionError(f"ffmpeg failed: {detail or 'no stderr'}")
        output = _regular_path(output, "ffmpeg output")
        output_sha256, output_size = _sha256_file(output)
        if output_size <= 0:
            raise FFmpegExecutionError("ffmpeg produced an empty output")
        return RenderAttempt(
            plan.recipe_hash, plan.profile_hash, source_sha256, output, output_sha256, output_size,
            argv, _sha256_bytes(completed.stderr[:_MAX_STDERR_BYTES]),
        )


def render(
    plan: RenderPlan,
    recipe: Recipe,
    *,
    source_path: Path,
    staging_root: Path,
    executable: str | None = None,
    timeout_seconds: float = 120.0,
    runner: Runner = subprocess.run,
) -> RenderAttempt:
    """Convenience one-shot renderer; use :class:`FFmpegRenderer` to inject a runner."""
    return FFmpegRenderer(executable, timeout_seconds=timeout_seconds, runner=runner).render(
        recipe, plan, source_path=source_path, staging_root=staging_root
    )


def _require_trusted_plan(plan: RenderPlan, recipe: Recipe, source: Path) -> None:
    """Accept only the exact fixed plan shape, never caller-controlled argv."""
    if plan.recipe_hash != recipe.canonical_hash:
        raise RenderError("render plan recipe hash does not match recipe")
    if plan.profile_hash != H264_MP4_VIDEO_PROFILE.canonical_hash:
        raise RenderError("render plan profile is unsupported")
    expected = build_render_plan(recipe, source_path=source, output_path=Path("__output__.mp4"))
    if plan.filter_graph != expected.filter_graph:
        raise RenderError("render plan filter graph is not trusted")
    expected_argv = expected.argv
    try:
        input_index = expected_argv.index("-i") + 1
    except ValueError as error:  # Defensive: the committed plan builder is closed.
        raise RenderError("trusted render plan has no input") from error
    if len(plan.argv) != len(expected_argv) or input_index >= len(plan.argv):
        raise RenderError("render plan argv is not trusted")
    # Paths are intentionally caller-variable; every option and its position is closed.
    for index, (actual, trusted) in enumerate(zip(plan.argv, expected_argv, strict=True)):
        if index not in {input_index, len(plan.argv) - 1} and actual != trusted:
            raise RenderError("render plan argv is not trusted")


def _copy_verified_source(source: Path, destination: Path, recipe: Recipe) -> None:
    """Copy and re-hash source bytes before invoking ffmpeg on the private copy."""
    digest = hashlib.sha256()
    size = 0
    try:
        with source.open("rb") as input_stream, destination.open("xb") as output_stream:
            for chunk in iter(lambda: input_stream.read(_CHUNK_SIZE), b""):
                digest.update(chunk)
                size += len(chunk)
                output_stream.write(chunk)
            output_stream.flush()
            os.fsync(output_stream.fileno())
    except OSError as error:
        raise RenderError("source_path could not be copied into private staging") from error
    copied_sha256 = f"sha256:{digest.hexdigest()}"
    if copied_sha256 != recipe.source_sha256 or size != recipe.source_byte_size:
        destination.unlink(missing_ok=True)
        raise SourceIdentityMismatchError("source identity changed while copying to staging")
    verified_sha256, verified_size = _sha256_file(destination)
    if verified_sha256 != recipe.source_sha256 or verified_size != recipe.source_byte_size:
        destination.unlink(missing_ok=True)
        raise SourceIdentityMismatchError("private staging source verification failed")
    destination.chmod(0o444)


def _staging_root(path: Path) -> Path:
    root = Path(path).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir() or root.is_symlink():
        raise RenderError("staging_root must be a real directory")
    return root


def _regular_path(path: Path, label: str) -> Path:
    candidate = Path(path).absolute()
    try:
        status = candidate.lstat()
    except OSError as error:
        raise RenderError(f"{label} must be a readable regular file") from error
    if not stat.S_ISREG(status.st_mode):
        raise RenderError(f"{label} must be a readable regular file")
    return candidate


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(_CHUNK_SIZE), b""):
            digest.update(chunk)
            size += len(chunk)
    return f"sha256:{digest.hexdigest()}", size


def _sha256_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"
