"""Local-only render, QC, and output-promotion orchestration.

This module is deliberately a non-DB boundary.  It consumes an already
compiled fixture recipe (or its still-to-be-parsed JSON form), produces private
staging evidence, and exposes an asset only through the promotion module's
atomic ``current.json`` pointer.  A durable command receipt belongs to the
later persistence integration rather than to this local coordinator.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

from ..output import (
    LocalPromotionError,
    LocalPromotionRequest,
    PromotionResult,
    promote_local_output,
)
from ..rendering import (
    H264_MP4_VIDEO_PROFILE,
    Recipe,
    RecipeValidationError,
    build_render_plan,
    parse_recipe,
)
from ..rendering.ffmpeg_renderer import FFmpegRenderer, RenderAttempt, RenderError
from ..rendering.qc import LocalQC, QCReport


@dataclass(frozen=True, slots=True)
class RenderLocalRequest:
    """Input for one local-only rendering attempt.

    ``recipe`` normally is the parsed :class:`Recipe` emitted by the preceding
    command.  Accepting its JSON form is intentionally limited to this boundary
    so malformed predecessor output is still denied before FFmpeg starts.
    """

    recipe: Recipe | object
    source_path: Path
    output_root: Path
    job_id: str
    attempt_id: str
    profile: str = "test"


@dataclass(frozen=True, slots=True)
class RenderLocalDenied:
    """A validation, QC, or promotion-eligibility denial with no visible output."""

    code: str
    detail: str
    attempt: RenderAttempt | None = None
    qc_report: QCReport | None = None

    @property
    def outcome(self) -> str:
        return "denied"


@dataclass(frozen=True, slots=True)
class RenderLocalFailed:
    """A renderer/tooling infrastructure failure with no visible output."""

    code: str
    detail: str

    @property
    def outcome(self) -> str:
        return "failed"


@dataclass(frozen=True, slots=True)
class RenderLocalSuccess:
    """A fully QC-approved local result made visible by atomic promotion."""

    recipe: Recipe
    attempt: RenderAttempt
    qc_report: QCReport
    promotion: PromotionResult

    @property
    def outcome(self) -> str:
        return "succeeded"


RenderLocalOutcome: TypeAlias = RenderLocalDenied | RenderLocalFailed | RenderLocalSuccess


class LocalRenderOrchestrator:
    """Sequence validation, staging render, derived QC, then atomic promotion."""

    def __init__(self, *, renderer: FFmpegRenderer | None = None, qc: LocalQC | None = None) -> None:
        self._renderer = renderer
        self._qc = qc

    def execute(self, request: RenderLocalRequest) -> RenderLocalOutcome:
        """Run one attempt without writing a database receipt or publishing externally."""
        try:
            recipe = _validate_recipe_and_source(request)
        except RecipeValidationError as error:
            return RenderLocalDenied(error.code, str(error))
        except ValueError as error:
            return RenderLocalDenied("SOURCE_IDENTITY_MISMATCH", str(error))

        try:
            plan = build_render_plan(
                recipe,
                source_path=request.source_path,
                output_path=request.output_root / "staging" / "ignored.mp4",
                profile=H264_MP4_VIDEO_PROFILE,
            )
            renderer = self._renderer or FFmpegRenderer()
            attempt = renderer.render(
                recipe,
                plan,
                source_path=request.source_path,
                staging_root=request.output_root / "staging" / request.job_id,
            )
        except RenderError as error:
            return RenderLocalFailed(_render_failure_code(error), str(error))
        except OSError as error:
            return RenderLocalFailed("RENDER_INFRASTRUCTURE_FAILED", str(error))

        try:
            report = (self._qc or LocalQC()).inspect(recipe, attempt)
            _persist_derived_qc_report(attempt, report)
        except (OSError, ValueError) as error:
            return RenderLocalDenied("QC_EVIDENCE_FAILED", str(error), attempt=attempt)
        if not report.approved:
            return RenderLocalDenied(_qc_failure_code(report), "derived QC report rejected render", attempt, report)

        try:
            promotion = promote_local_output(
                LocalPromotionRequest(
                    output_root=request.output_root,
                    job_id=request.job_id,
                    attempt_id=request.attempt_id,
                    staging_asset=attempt.output_path,
                    recipe_hash=recipe.canonical_hash,
                    asset_sha256=attempt.output_sha256,
                    qc_report=report,
                )
            )
        except LocalPromotionError as error:
            return RenderLocalDenied("PROMOTION_DENIED", str(error), attempt, report)
        return RenderLocalSuccess(recipe, attempt, report, promotion)


def render_local(
    request: RenderLocalRequest,
    *,
    renderer: FFmpegRenderer | None = None,
    qc: LocalQC | None = None,
) -> RenderLocalOutcome:
    """Convenience entry point for the local orchestration boundary."""
    return LocalRenderOrchestrator(renderer=renderer, qc=qc).execute(request)


def _validate_recipe_and_source(request: RenderLocalRequest) -> Recipe:
    """Bind input bytes to the recipe before constructing an FFmpeg invocation."""
    source_hash, source_size = _source_identity(request.source_path)
    if isinstance(request.recipe, Recipe):
        recipe = request.recipe
    else:
        recipe = parse_recipe(request.recipe, expected_source_sha256=source_hash, profile=request.profile)  # type: ignore[arg-type]
    if recipe.source_sha256 != source_hash or recipe.source_byte_size != source_size:
        raise ValueError("source identity does not match recipe")
    return recipe


def _source_identity(path: Path) -> tuple[str, int]:
    """Hash only a regular source file; never coerce paths or identities."""
    try:
        status = path.lstat()
    except OSError as error:
        raise ValueError("source_path must be a readable regular file") from error
    if not stat.S_ISREG(status.st_mode):
        raise ValueError("source_path must be a readable regular file")
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
    except OSError as error:
        raise ValueError("source_path must be a readable regular file") from error
    return f"sha256:{digest.hexdigest()}", size


def _render_failure_code(error: RenderError) -> str:
    message = str(error).lower()
    if "timed out" in message:
        return "RENDER_TIMEOUT"
    if "unavailable" in message:
        return "RENDER_UNAVAILABLE"
    return "RENDER_EXECUTION_FAILED"


def _qc_failure_code(report: QCReport) -> str:
    failed = next(check.name for check in report.checks if not check.passed)
    return f"QC_{failed.upper()}"


def _persist_derived_qc_report(attempt: RenderAttempt, report: QCReport) -> Path:
    """Durably record the QC observation in staging before promotion can begin."""
    value = report.to_manifest()
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    target = attempt.output_path.parent / "qc-report.json"
    descriptor, name = tempfile.mkstemp(prefix=".qc-report-", dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        temporary = None
        _fsync_directory(target.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return target


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
