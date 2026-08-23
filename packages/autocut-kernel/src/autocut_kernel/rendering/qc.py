"""Deterministic, fail-closed local QC for a completed render attempt."""

from __future__ import annotations

import hashlib
import json
import shutil
import stat
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from ..media.types import canonical_sha256, sha256_prefixed
from .ffmpeg_renderer import RenderAttempt
from .models import Recipe

_CHUNK_SIZE = 1024 * 1024
Runner = Callable[..., subprocess.CompletedProcess[bytes]]


@dataclass(frozen=True, slots=True)
class QCCheck:
    """A required QC check and deterministic evidence digest."""

    name: str
    passed: bool
    evidence_sha256: str

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("qc check name must be non-empty")
        sha256_prefixed(self.evidence_sha256, "qc_check.evidence_sha256")


@dataclass(frozen=True, slots=True)
class QCReport:
    """Closed QC outcome.  Approval is derived only from all required checks."""

    recipe_hash: str
    output_sha256: str
    checks: tuple[QCCheck, ...]

    def __post_init__(self) -> None:
        sha256_prefixed(self.recipe_hash, "qc_report.recipe_hash")
        sha256_prefixed(self.output_sha256, "qc_report.output_sha256")
        if not self.checks:
            raise ValueError("qc_report must contain checks")

    @property
    def approved(self) -> bool:
        return all(check.passed for check in self.checks)

    @property
    def status(self) -> str:
        return "approved" if self.approved else "rejected"

    def to_manifest(self) -> dict[str, object]:
        return {
            "checks": [
                {"evidence_sha256": item.evidence_sha256, "name": item.name, "passed": item.passed}
                for item in self.checks
            ],
            "output_sha256": self.output_sha256,
            "recipe_hash": self.recipe_hash,
            "status": self.status,
        }


class LocalQC:
    """Run fixed ffprobe/ffmpeg checks without caller-controlled pass flags."""

    def __init__(
        self, *, ffprobe: str | None = None, ffmpeg: str | None = None,
        timeout_seconds: float = 120.0, runner: Runner = subprocess.run,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._ffprobe = ffprobe or shutil.which("ffprobe")
        self._ffmpeg = ffmpeg or shutil.which("ffmpeg")
        self._timeout_seconds = timeout_seconds
        self._runner = runner

    def inspect(self, recipe: Recipe, attempt: RenderAttempt) -> QCReport:
        """Derive the complete QC report from the recipe, attempt, and output bytes."""
        checks = (
            self._identity_check(recipe, attempt),
            self._topology_check(attempt),
            self._decode_check(attempt),
            self._sample_check(attempt),
            self._coarse_visual_check(attempt),
        )
        return QCReport(recipe.canonical_hash, attempt.output_sha256, checks)

    def _identity_check(self, recipe: Recipe, attempt: RenderAttempt) -> QCCheck:
        try:
            path = attempt.output_path.resolve()
            regular = stat.S_ISREG(path.stat().st_mode)
            digest, size = _sha256_file(path) if regular else ("", 0)
            passed = (
                attempt.recipe_hash == recipe.canonical_hash
                and regular and size > 0 and digest == attempt.output_sha256
            )
            evidence: object = {"digest": digest, "regular": regular, "size": size}
        except OSError as error:
            passed, evidence = False, {"error": type(error).__name__}
        return _check("regular_nonempty_digest", passed, evidence)

    def _topology_check(self, attempt: RenderAttempt) -> QCCheck:
        if not self._ffprobe:
            return _check("h264_mp4_video_only", False, {"error": "ffprobe unavailable"})
        completed = self._run([self._ffprobe, "-v", "error", "-of", "json", "-show_format", "-show_streams", "--", str(attempt.output_path)])
        if completed is None or completed.returncode != 0:
            return _check("h264_mp4_video_only", False, _completed_evidence(completed))
        try:
            payload = cast(dict[str, Any], json.loads(completed.stdout.decode("utf-8")))
            streams = payload["streams"]
            format_name = payload["format"]["format_name"]
            if not isinstance(streams, list) or not all(
                isinstance(item, dict) for item in cast(list[object], streams)
            ):
                raise TypeError("streams must be an array of objects")
            stream_records = cast(list[dict[str, Any]], streams)
            videos = [item for item in stream_records if item.get("codec_type") == "video"]
            passed = (
                len(stream_records) == 1 and len(videos) == 1
                and videos[0].get("codec_name") == "h264" and isinstance(format_name, str)
                and "mp4" in format_name.split(",")
            )
            evidence: object = payload
        except (KeyError, UnicodeDecodeError, json.JSONDecodeError, TypeError) as error:
            passed, evidence = False, {"error": type(error).__name__}
        return _check("h264_mp4_video_only", passed, evidence)

    def _decode_check(self, attempt: RenderAttempt) -> QCCheck:
        if not self._ffmpeg:
            return _check("full_decode", False, {"error": "ffmpeg unavailable"})
        completed = self._run([self._ffmpeg, "-v", "error", "-i", str(attempt.output_path), "-map", "0:v:0", "-an", "-f", "null", "-"])
        return _check("full_decode", completed is not None and completed.returncode == 0, _completed_evidence(completed))

    def _sample_check(self, attempt: RenderAttempt) -> QCCheck:
        if not self._ffmpeg:
            return _check("deterministic_frame_evidence", False, {"error": "ffmpeg unavailable"})
        completed = self._run([self._ffmpeg, "-v", "error", "-i", str(attempt.output_path), "-map", "0:v:0", "-an", "-f", "framemd5", "-"])
        if completed is None or completed.returncode != 0:
            return _check("deterministic_frame_evidence", False, _completed_evidence(completed))
        lines = tuple(line for line in completed.stdout.decode("utf-8", "replace").splitlines() if line and not line.startswith("#"))
        # FrameMD5 lines cover every decoded frame; retain fixed positions as a compact, reproducible witness.
        samples = (lines[0], lines[len(lines) // 2], lines[-1]) if lines else ()
        return _check("deterministic_frame_evidence", bool(lines), {"frame_count": len(lines), "samples": samples})

    def _coarse_visual_check(self, attempt: RenderAttempt) -> QCCheck:
        if not self._ffmpeg:
            return _check("coarse_black_freeze_guard", False, {"error": "ffmpeg unavailable"})
        completed = self._run([
            self._ffmpeg, "-hide_banner", "-v", "info", "-i", str(attempt.output_path), "-an",
            "-vf", "blackdetect=d=0.50:pix_th=0.10,freezedetect=d=0.50:n=0.001", "-f", "null", "-",
        ])
        output = b"" if completed is None else completed.stdout + completed.stderr
        detected = b"black_start:" in output or b"freeze_start:" in output
        return _check("coarse_black_freeze_guard", completed is not None and completed.returncode == 0 and not detected, _completed_evidence(completed))

    def _run(self, argv: list[str]) -> subprocess.CompletedProcess[bytes] | None:
        try:
            return self._runner(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=self._timeout_seconds)
        except (OSError, subprocess.TimeoutExpired):
            return None


def inspect(
    recipe: Recipe,
    attempt: RenderAttempt,
    *,
    ffprobe: str | None = None,
    ffmpeg: str | None = None,
    timeout_seconds: float = 120.0,
    runner: Runner = subprocess.run,
) -> QCReport:
    """Convenience one-shot QC; use :class:`LocalQC` to inject a runner."""
    return LocalQC(ffprobe=ffprobe, ffmpeg=ffmpeg, timeout_seconds=timeout_seconds, runner=runner).inspect(recipe, attempt)


def _sha256_file(path: Path) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(_CHUNK_SIZE), b""):
            digest.update(chunk)
            size += len(chunk)
    return f"sha256:{digest.hexdigest()}", size


def _check(name: str, passed: bool, evidence: object) -> QCCheck:
    return QCCheck(name, passed, canonical_sha256(evidence))


def _completed_evidence(completed: subprocess.CompletedProcess[bytes] | None) -> dict[str, object]:
    if completed is None:
        return {"error": "execution failed"}
    return {"returncode": completed.returncode, "stderr_sha256": f"sha256:{hashlib.sha256(completed.stderr).hexdigest()}", "stdout_sha256": f"sha256:{hashlib.sha256(completed.stdout).hexdigest()}"}
