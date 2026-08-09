#!/usr/bin/env python3
"""Evidence-preserving local media fallback for failed remote Windows.

The normal remote path intentionally starts immediately and analyzes the URL
while the source downloads in parallel.  If the model uses content outside a
declared Window, this module validates the completed local source and creates
a physical H.264/AAC clip for one failed Window.  It never changes model
output, Window boundaries, or the original Batch job.
"""

from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autocut_core.io import (
    atomic_write_json,
    json_sha256,
    load_json,
    sha256_file,
    utc_now,
)


POLICY_VERSION = "window-local-clip-recovery-v1"
RECOVERY_MEDIA_MODE = "physical_window_recovery"
MEDIA_DURATION_TOLERANCE_SECONDS = 0.25
ACCEPTED_DOWNLOAD_STATUSES = frozenset({"downloaded", "reused"})


class WindowMediaRecoveryError(RuntimeError):
    """A typed local recovery failure safe to persist in batch metadata."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class MediaProbe:
    duration_seconds: float
    streams: list[dict[str, Any]]

    @property
    def has_video(self) -> bool:
        return any(item.get("codec_type") == "video" for item in self.streams)


@dataclass(frozen=True)
class WindowMediaArtifact:
    path: Path
    source_path: Path
    source_sha256: str
    clip_sha256: str
    source_duration_seconds: float
    clip_duration_seconds: float
    size_bytes: int
    start: float
    end: float
    reused: bool

    def as_metadata(self) -> dict[str, Any]:
        return {
            "policy_version": POLICY_VERSION,
            "source_path": str(self.source_path),
            "source_sha256": self.source_sha256,
            "source_duration_seconds": self.source_duration_seconds,
            "clip_path": str(self.path),
            "clip_sha256": self.clip_sha256,
            "clip_duration_seconds": self.clip_duration_seconds,
            "clip_size_bytes": self.size_bytes,
            "start": self.start,
            "end": self.end,
            "reused": self.reused,
        }


def _safe_component(value: Any) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value)).strip("-._")
    return text or "window"


def _range_component(value: float) -> str:
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text.replace(".", "p")


def _require_float(value: Any, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WindowMediaRecoveryError(
            "WINDOW_RECOVERY_JOB_INVALID",
            f"{label} must be a finite number",
        )
    parsed = float(value)
    if not (-float("inf") < parsed < float("inf")):
        raise WindowMediaRecoveryError(
            "WINDOW_RECOVERY_JOB_INVALID",
            f"{label} must be finite",
        )
    return parsed


def original_job_identity(job: dict[str, Any]) -> str:
    """Return a stable identity without persisting a signed media URL."""

    media_url = job.get("media_url")
    return json_sha256(
        {
            "task": job.get("task"),
            "stage_version": job.get("stage_version"),
            "source_id": job.get("source_id"),
            "episode": job.get("episode"),
            "window_id": job.get("window_id"),
            "start": job.get("start"),
            "end": job.get("end"),
            "media_url_sha256": (
                json_sha256(media_url) if isinstance(media_url, str) else None
            ),
        }
    )


def recovery_report_path(job: dict[str, Any]) -> Path:
    output = job.get("output")
    if not isinstance(output, str):
        raise WindowMediaRecoveryError(
            "WINDOW_RECOVERY_JOB_INVALID",
            "job.output is required",
        )
    output_path = Path(output).expanduser().resolve()
    job_id = _safe_component(job.get("id") or output_path.stem)
    return output_path.parent / ".media-recovery" / f"{job_id}.json"


def probe_media(path: Path, *, ffprobe: str = "ffprobe") -> MediaProbe:
    command = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=index,codec_name,codec_type,width,height",
        "-of",
        "json",
        str(path),
    ]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = " ".join((completed.stderr or "ffprobe failed").split())[:600]
        raise WindowMediaRecoveryError(
            "WINDOW_RECOVERY_MEDIA_UNREADABLE",
            f"ffprobe could not read {path}: {detail}",
        )
    try:
        payload = json.loads(completed.stdout)
        duration = float(payload.get("format", {}).get("duration"))
        streams = payload.get("streams", [])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise WindowMediaRecoveryError(
            "WINDOW_RECOVERY_MEDIA_UNREADABLE",
            f"ffprobe returned invalid metadata for {path}",
        ) from exc
    if duration <= 0 or not isinstance(streams, list):
        raise WindowMediaRecoveryError(
            "WINDOW_RECOVERY_MEDIA_UNREADABLE",
            f"media duration/streams are invalid for {path}",
        )
    return MediaProbe(
        duration_seconds=duration,
        streams=[item for item in streams if isinstance(item, dict)],
    )


def _source_record(
    local_source_manifest: dict[str, Any],
    *,
    source_id: str,
) -> dict[str, Any]:
    sources = local_source_manifest.get("sources")
    if not isinstance(sources, list):
        raise WindowMediaRecoveryError(
            "WINDOW_RECOVERY_SOURCE_STALE",
            "local source manifest has no sources array",
        )
    matches = [
        item
        for item in sources
        if isinstance(item, dict) and item.get("id") == source_id
    ]
    if len(matches) != 1:
        raise WindowMediaRecoveryError(
            "WINDOW_RECOVERY_SOURCE_STALE",
            f"expected one local source for {source_id}, found {len(matches)}",
        )
    return matches[0]


def _validated_source(
    job: dict[str, Any],
    *,
    local_source_manifest_path: Path,
    ffprobe: str,
) -> tuple[Path, str, MediaProbe]:
    if not local_source_manifest_path.is_file():
        raise WindowMediaRecoveryError(
            "WINDOW_RECOVERY_SOURCE_NOT_READY",
            f"local source manifest is missing: {local_source_manifest_path}",
        )
    manifest = load_json(local_source_manifest_path)
    if not isinstance(manifest, dict):
        raise WindowMediaRecoveryError(
            "WINDOW_RECOVERY_SOURCE_STALE",
            "local source manifest must be an object",
        )
    source_id = job.get("source_id")
    if not isinstance(source_id, str) or not source_id:
        raise WindowMediaRecoveryError(
            "WINDOW_RECOVERY_JOB_INVALID",
            "job.source_id is required",
        )
    source = _source_record(manifest, source_id=source_id)
    if source.get("episode") != job.get("episode"):
        raise WindowMediaRecoveryError(
            "WINDOW_RECOVERY_SOURCE_STALE",
            f"episode mismatch for source {source_id}",
        )
    download = source.get("download")
    if not isinstance(download, dict):
        download = {}
    status = download.get("status")
    if status not in ACCEPTED_DOWNLOAD_STATUSES:
        raise WindowMediaRecoveryError(
            "WINDOW_RECOVERY_SOURCE_NOT_READY",
            f"source {source_id} download status is {status!r}",
        )
    path_value = source.get("path") or download.get("path")
    if not isinstance(path_value, str):
        raise WindowMediaRecoveryError(
            "WINDOW_RECOVERY_SOURCE_STALE",
            f"source {source_id} has no local path",
        )
    source_path = Path(path_value).expanduser().resolve()
    if not source_path.is_file():
        raise WindowMediaRecoveryError(
            "WINDOW_RECOVERY_SOURCE_STALE",
            f"local source does not exist: {source_path}; rebase copied job paths",
        )
    expected_sha256 = source.get("sha256") or download.get("sha256")
    if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
        raise WindowMediaRecoveryError(
            "WINDOW_RECOVERY_SOURCE_STALE",
            f"source {source_id} has no valid SHA-256",
        )
    actual_sha256 = sha256_file(source_path)
    if actual_sha256 != expected_sha256:
        raise WindowMediaRecoveryError(
            "WINDOW_RECOVERY_SOURCE_HASH_MISMATCH",
            f"source {source_id} SHA-256 does not match its manifest",
        )
    probe = probe_media(source_path, ffprobe=ffprobe)
    if not probe.has_video:
        raise WindowMediaRecoveryError(
            "WINDOW_RECOVERY_SOURCE_NO_VIDEO",
            f"source {source_id} has no video stream",
        )
    return source_path, actual_sha256, probe


def _validate_clip(
    path: Path,
    *,
    expected_duration: float,
    max_inline_mb: float,
    ffprobe: str,
) -> tuple[MediaProbe, int, str]:
    probe = probe_media(path, ffprobe=ffprobe)
    if not probe.has_video:
        raise WindowMediaRecoveryError(
            "WINDOW_RECOVERY_CLIP_NO_VIDEO",
            f"recovery clip has no video stream: {path}",
        )
    if abs(probe.duration_seconds - expected_duration) > MEDIA_DURATION_TOLERANCE_SECONDS:
        raise WindowMediaRecoveryError(
            "WINDOW_RECOVERY_CLIP_DURATION_MISMATCH",
            (
                f"recovery clip duration {probe.duration_seconds:.3f}s does not "
                f"match target {expected_duration:.3f}s"
            ),
        )
    size_bytes = path.stat().st_size
    maximum_bytes = int(max_inline_mb * 1024 * 1024)
    if size_bytes > maximum_bytes:
        raise WindowMediaRecoveryError(
            "WINDOW_RECOVERY_CLIP_TOO_LARGE",
            (
                f"recovery clip is {size_bytes / 1024 / 1024:.1f} MiB, above "
                f"inline limit {max_inline_mb:.1f} MiB"
            ),
        )
    return probe, size_bytes, sha256_file(path)


def prepare_window_media_artifact(
    job: dict[str, Any],
    *,
    local_source_manifest_path: Path,
    cache_root: Path,
    max_inline_mb: float,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
) -> WindowMediaArtifact:
    """Validate one downloaded source and atomically create its physical Window."""

    if job.get("task") != "window_analysis":
        raise WindowMediaRecoveryError(
            "WINDOW_RECOVERY_JOB_INVALID",
            "only window_analysis jobs can use local Window recovery",
        )
    if job.get("media_url_mode") != "full_source" or not isinstance(
        job.get("media_url"), str
    ):
        raise WindowMediaRecoveryError(
            "WINDOW_RECOVERY_JOB_INVALID",
            "recovery requires an original full_source media_url job",
        )
    start = _require_float(job.get("start"), label="job.start")
    end = _require_float(job.get("end"), label="job.end")
    if start < 0 or end <= start:
        raise WindowMediaRecoveryError(
            "WINDOW_RECOVERY_JOB_INVALID",
            f"invalid Window range [{start}, {end}]",
        )
    source_path, source_sha256, source_probe = _validated_source(
        job,
        local_source_manifest_path=local_source_manifest_path,
        ffprobe=ffprobe,
    )
    if source_probe.duration_seconds + MEDIA_DURATION_TOLERANCE_SECONDS < end:
        raise WindowMediaRecoveryError(
            "WINDOW_RECOVERY_SOURCE_TOO_SHORT",
            (
                f"source duration {source_probe.duration_seconds:.3f}s does not "
                f"cover Window end {end:.3f}s"
            ),
        )
    expected_duration = end - start
    destination = (
        cache_root.expanduser().resolve()
        / source_sha256
        / (
            f"{_safe_component(job.get('window_id') or job.get('id'))}-"
            f"{_range_component(start)}-{_range_component(end)}.mp4"
        )
    )
    if destination.is_file():
        try:
            clip_probe, size_bytes, clip_sha256 = _validate_clip(
                destination,
                expected_duration=expected_duration,
                max_inline_mb=max_inline_mb,
                ffprobe=ffprobe,
            )
            return WindowMediaArtifact(
                path=destination,
                source_path=source_path,
                source_sha256=source_sha256,
                clip_sha256=clip_sha256,
                source_duration_seconds=source_probe.duration_seconds,
                clip_duration_seconds=clip_probe.duration_seconds,
                size_bytes=size_bytes,
                start=start,
                end=end,
                reused=True,
            )
        except WindowMediaRecoveryError:
            # A stale/incomplete cache entry is replaced atomically below.
            pass

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}-{uuid.uuid4().hex}.tmp.mp4"
    )
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{start:.6f}",
        "-t",
        f"{expected_duration:.6f}",
        "-i",
        str(source_path),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-vf",
        "scale='min(720,iw)':-2,format=yuv420p",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "28",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-movflags",
        "+faststart",
        str(temporary),
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0 or not temporary.is_file():
            detail = " ".join((completed.stderr or "ffmpeg failed").split())[:600]
            raise WindowMediaRecoveryError(
                "WINDOW_RECOVERY_TRANSCODE_FAILED",
                f"could not create physical Window: {detail}",
            )
        clip_probe, size_bytes, clip_sha256 = _validate_clip(
            temporary,
            expected_duration=expected_duration,
            max_inline_mb=max_inline_mb,
            ffprobe=ffprobe,
        )
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return WindowMediaArtifact(
        path=destination,
        source_path=source_path,
        source_sha256=source_sha256,
        clip_sha256=clip_sha256,
        source_duration_seconds=source_probe.duration_seconds,
        clip_duration_seconds=clip_probe.duration_seconds,
        size_bytes=size_bytes,
        start=start,
        end=end,
        reused=False,
    )


def build_window_media_recovery_job(
    original_job: dict[str, Any],
    artifact: WindowMediaArtifact,
    *,
    failure_codes: list[str],
    blockers: list[dict[str, Any]],
    original_failure_request_signature: str | None = None,
) -> dict[str, Any]:
    """Deep-copy a Batch job and replace only its effective request media."""

    recovery_job = copy.deepcopy(original_job)
    recovery_job.pop("media_url", None)
    recovery_job["media_file"] = str(artifact.path)
    recovery_job["media_url_mode"] = RECOVERY_MEDIA_MODE
    report_path = recovery_report_path(original_job)
    recovery_job["window_media_recovery"] = {
        "policy_version": POLICY_VERSION,
        "original_job_identity": original_job_identity(original_job),
        "source_sha256": artifact.source_sha256,
        "clip_sha256": artifact.clip_sha256,
        "start": artifact.start,
        "end": artifact.end,
        "failure_codes": sorted(set(failure_codes)),
        "blockers_sha256": json_sha256(blockers),
        "original_failure_request_signature": original_failure_request_signature,
        "report_path": str(report_path),
    }
    atomic_write_json(
        report_path,
        {
            "schema_version": "1.0",
            "policy_version": POLICY_VERSION,
            "status": "prepared",
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "job_id": original_job.get("id"),
            "window_id": original_job.get("window_id"),
            "source_id": original_job.get("source_id"),
            "episode": original_job.get("episode"),
            "original_job_identity": original_job_identity(original_job),
            "original_failure_request_signature": (
                original_failure_request_signature
            ),
            "failure_codes": sorted(set(failure_codes)),
            "blockers": blockers,
            "artifact": artifact.as_metadata(),
            "recovery_request_signature": None,
            "attempt_ledger_invocation_id": None,
            "result_output_sha256": None,
            "error": None,
        },
        private=True,
    )
    return recovery_job


def mark_window_media_recovery_outcome(
    job: dict[str, Any],
    *,
    status: str,
    request_signature: str | None,
    output_sha256: str | None = None,
    error: str | None = None,
    attempt_ledger_invocation_id: str | None = None,
) -> None:
    metadata = job.get("window_media_recovery")
    if not isinstance(metadata, dict):
        return
    report_value = metadata.get("report_path")
    if not isinstance(report_value, str):
        return
    report_path = Path(report_value).expanduser().resolve()
    report = load_json(report_path) if report_path.is_file() else {}
    if not isinstance(report, dict):
        report = {}
    safe_error = None
    if error:
        safe_error = re.sub(r"https?://\S+", "[redacted-url]", error)
        safe_error = " ".join(safe_error.split())[:1200]
    updates = {
        "status": status,
        "updated_at": utc_now(),
        "recovery_request_signature": request_signature,
        "result_output_sha256": output_sha256,
        "error": safe_error,
    }
    if attempt_ledger_invocation_id is not None:
        updates["attempt_ledger_invocation_id"] = attempt_ledger_invocation_id
    report.update(updates)
    atomic_write_json(report_path, report, private=True)


def load_window_media_recovery_report(
    original_job: dict[str, Any],
) -> dict[str, Any] | None:
    path = recovery_report_path(original_job)
    if not path.is_file():
        return None
    value = load_json(path)
    if not isinstance(value, dict):
        return None
    if value.get("policy_version") != POLICY_VERSION:
        return None
    if value.get("original_job_identity") != original_job_identity(original_job):
        return None
    return value
