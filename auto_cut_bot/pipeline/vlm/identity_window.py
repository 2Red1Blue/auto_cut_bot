"""Build an exact VLM window when the submitted MP4 is itself the Source.

This is the smallest real-media path: no proxy-to-original conversion exists,
so the timeline certificate is a true identity translation.  Transcoded
proxies must use a separately verified timeline map and cannot use this builder.
"""

# pyright: reportMissingTypeStubs=false

from __future__ import annotations

import hashlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from autocut_kernel.media import (
    Coverage,
    CoverageOutcome,
    EvidenceContext,
    FramePtsIndexSet,
    MediaKind,
    TickRange,
)
from autocut_kernel.media.ffprobe_port import FFprobePort
from autocut_kernel.media.types import canonical_sha256
from autocut_kernel.store import BlobRef, Job
from autocut_kernel.vlm import (
    WindowFrameSample,
    WindowManifest,
    WindowManifestSet,
    WindowProxyBlobRef,
)
from autocut_kernel.vlm.window import ProxyTimelineMap


class BlobWriter(Protocol):
    def put_immutable_blob(
        self,
        job: Job,
        *,
        content: bytes,
        content_hash: str,
        media_type: str,
    ) -> BlobRef: ...


@dataclass(frozen=True, slots=True)
class IdentityProxyWindow:
    proxy_blob: BlobRef
    manifest: WindowManifest
    manifest_set: WindowManifestSet


class IdentityProxyWindowBuilder:
    """Probe, sample, hash and persist one unchanged MP4 Source window."""

    def __init__(
        self,
        *,
        ffprobe: FFprobePort | None = None,
        ffmpeg_executable: str | None = None,
        sample_count: int = 9,
        timeout_seconds: float = 60.0,
    ) -> None:
        if type(sample_count) is not int or sample_count < 1:  # noqa: E721
            raise ValueError("sample_count must be a positive integer")
        if type(timeout_seconds) not in (int, float) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        executable = ffmpeg_executable or shutil.which("ffmpeg")
        if not executable:
            raise RuntimeError("ffmpeg executable is unavailable")
        self._ffprobe = ffprobe or FFprobePort(timeout_seconds=timeout_seconds)
        self._ffmpeg = executable
        self._sample_count = sample_count
        self._timeout_seconds = float(timeout_seconds)

    def build(
        self,
        *,
        store: BlobWriter,
        job: Job,
        source_path: Path,
        source_id: str,
    ) -> IdentityProxyWindow:
        path = Path(source_path).expanduser().resolve()
        if path.suffix.lower() != ".mp4" or not path.is_file():
            raise ValueError("identity VLM source must be an existing MP4 file")
        source_bytes = path.read_bytes()
        if not source_bytes:
            raise ValueError("identity VLM source must not be empty")
        source_sha256 = _sha256(source_bytes)
        probe = self._ffprobe.probe(path)
        ticks = probe.pts_index.ticks
        origin_tick = ticks[0]
        end_tick = ticks[-1] + 1
        source_range = TickRange(origin_tick, end_tick)
        clock_id = f"video-stream-{probe.video_stream.stream_index}"
        generation_policy = canonical_sha256(
            {
                "builder": "identity-proxy-window-v1",
                "ffprobe_version": probe.tool.version,
                "media_type": "video/mp4",
            }
        )
        context = EvidenceContext(
            source_id,
            source_sha256,
            MediaKind.VIDEO,
            clock_id,
            probe.video_stream.time_base,
            origin_tick,
            end_tick - origin_tick,
            "identity-proxy-window-v1",
            generation_policy,
        )
        coverage = Coverage(
            source_id,
            source_sha256,
            clock_id,
            probe.video_stream.time_base,
            origin_tick,
            end_tick,
            CoverageOutcome.COMPLETE,
        )
        frame_index = FramePtsIndexSet(
            "identity-proxy-frame-pts-v1",
            context,
            coverage,
            probe.pts_index,
            canonical_sha256(list(ticks)),
        )
        samples = tuple(
            WindowFrameSample(ticks[index], ticks[index], _sha256(self._frame_png(path, index)))
            for index in _sample_indices(len(ticks), self._sample_count)
        )
        final_sha256 = _sha256(path.read_bytes())
        if final_sha256 != source_sha256:
            raise RuntimeError("source bytes changed while VLM window evidence was collected")
        blob = store.put_immutable_blob(
            job,
            content=source_bytes,
            content_hash=source_sha256,
            media_type="video/mp4",
        )
        proxy_ref = WindowProxyBlobRef(
            str(blob.object_id),
            blob.content_hash,
            blob.byte_length,
            blob.media_type,
        )
        sampling_policy = canonical_sha256(
            {
                "algorithm": "uniform-decoded-frame-index-v1",
                "sample_count": self._sample_count,
                "selected_indices": list(_sample_indices(len(ticks), self._sample_count)),
            }
        )
        manifest = WindowManifest(
            source_id=source_id,
            source_clock_id=clock_id,
            source_sha256=source_sha256,
            stream_index=probe.video_stream.stream_index,
            source_time_base=probe.video_stream.time_base,
            source_range=source_range,
            core_range=source_range,
            frame_pts_index_set=frame_index,
            proxy_blob_ref=proxy_ref,
            preprocess_policy_sha256=canonical_sha256(
                {"operation": "identity", "source_sha256": source_sha256}
            ),
            window_sampling_policy_sha256=sampling_policy,
            timeline_map=ProxyTimelineMap.translation(
                time_base=probe.video_stream.time_base,
                proxy_range=source_range,
                source_start_pts=origin_tick,
            ),
            frame_samples=samples,
        )
        manifest_set = WindowManifestSet(
            source_id=source_id,
            source_clock_id=clock_id,
            source_sha256=source_sha256,
            stream_index=probe.video_stream.stream_index,
            source_time_base=probe.video_stream.time_base,
            declared_source_range=source_range,
            manifests=(manifest,),
        )
        return IdentityProxyWindow(blob, manifest, manifest_set)

    def _frame_png(self, path: Path, frame_index: int) -> bytes:
        completed = subprocess.run(
            [
                self._ffmpeg,
                "-v",
                "error",
                "-i",
                str(path),
                "-map",
                "0:v:0",
                "-vf",
                f"select=eq(n\\,{frame_index})",
                "-frames:v",
                "1",
                "-f",
                "image2pipe",
                "-vcodec",
                "png",
                "pipe:1",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=self._timeout_seconds,
        )
        if completed.returncode != 0 or not completed.stdout:
            detail = completed.stderr[:1024].decode("utf-8", "replace").strip()
            raise RuntimeError(f"ffmpeg could not extract sampled frame: {detail or 'no output'}")
        return completed.stdout


def _sample_indices(frame_count: int, sample_count: int) -> tuple[int, ...]:
    if frame_count < 1:
        raise ValueError("decoded frame index must not be empty")
    count = min(frame_count, sample_count)
    if count == 1:
        return (0,)
    return tuple((position * (frame_count - 1)) // (count - 1) for position in range(count))


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


__all__ = ["IdentityProxyWindow", "IdentityProxyWindowBuilder"]
