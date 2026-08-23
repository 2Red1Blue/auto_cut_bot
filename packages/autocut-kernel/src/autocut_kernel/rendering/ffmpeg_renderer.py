"""The local, argv-only execution boundary for a :class:`RenderPlan`."""

from __future__ import annotations

import hashlib
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..media.types import sha256_prefixed
from .models import Recipe, RenderPlan

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
        output = attempt_dir / "asset.mp4"
        argv = _bound_argv(plan, source, output)
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
        source_after_sha256, source_after_size = _sha256_file(source)
        if source_after_sha256 != recipe.source_sha256 or source_after_size != recipe.source_byte_size:
            raise SourceIdentityMismatchError("source identity changed while rendering")
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


def _bound_argv(plan: RenderPlan, source: Path, output: Path) -> tuple[str, ...]:
    """Bind immutable per-attempt paths without admitting a shell or extra options."""
    argv = list(plan.argv)
    try:
        input_index = argv.index("-i") + 1
    except ValueError as error:
        raise RenderError("render plan has no input path") from error
    if input_index >= len(argv) - 1 or argv[-1].startswith("-"):
        raise RenderError("render plan has invalid input or output path")
    argv[0] = "ffmpeg"  # The record remains portable; executable resolution happens at execution.
    argv[input_index] = str(source)
    argv[-1] = str(output)
    return tuple(argv)


def _staging_root(path: Path) -> Path:
    root = Path(path).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir() or root.is_symlink():
        raise RenderError("staging_root must be a real directory")
    return root


def _regular_path(path: Path, label: str) -> Path:
    resolved = Path(path).resolve()
    try:
        status = resolved.stat()
    except OSError as error:
        raise RenderError(f"{label} must be a readable regular file") from error
    if not stat.S_ISREG(status.st_mode):
        raise RenderError(f"{label} must be a readable regular file")
    return resolved


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
