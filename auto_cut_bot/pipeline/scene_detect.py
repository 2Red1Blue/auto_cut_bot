"""PySceneDetect wrapper — frame-accurate shot boundary detection."""

import json
from pathlib import Path
from typing import Any


def detect_scenes(
    video_path: str | Path,
    *,
    threshold: float = 27.0,
    min_scene_len: int = 15,
    detector: str = "content",
) -> list[dict[str, Any]]:
    """Detect shot/scene boundaries using PySceneDetect."""
    from scenedetect import open_video, SceneManager
    from scenedetect.detectors import ContentDetector, ThresholdDetector

    video_path = Path(video_path).expanduser().resolve()
    if not video_path.is_file():
        raise FileNotFoundError(f"Video not found: {video_path}")

    video = open_video(str(video_path))
    scene_manager = SceneManager()
    detector_cls = {"content": ContentDetector, "threshold": ThresholdDetector}.get(detector, ContentDetector)
    scene_manager.add_detector(detector_cls(threshold=threshold, min_scene_len=min_scene_len))
    scene_manager.detect_scenes(video)

    boundaries: list[dict[str, Any]] = []
    for i, (start, end) in enumerate(scene_manager.get_scene_list(), start=1):
        boundaries.append({
            "shot_id": i,
            "start": round(start.get_seconds(), 3),
            "end": round(end.get_seconds(), 3),
            "duration": round(end.get_seconds() - start.get_seconds(), 3),
        })
    return boundaries


def get_boundary_times(boundaries: list[dict[str, Any]]) -> list[float]:
    return sorted({b[k] for b in boundaries for k in ("start", "end")})


def align_window(start: float, end: float, boundary_times: list[float], *, tolerance: float = 2.0):
    if not boundary_times:
        return start, end, {"start_aligned": False, "end_aligned": False, "start_shift": 0.0, "end_shift": 0.0}
    ns = min(boundary_times, key=lambda t: abs(t - start))
    ne = min(boundary_times, key=lambda t: abs(t - end))
    return (
        ns if abs(ns - start) <= tolerance else start,
        ne if abs(ne - end) <= tolerance else end,
        {"start_aligned": abs(ns - start) <= tolerance, "end_aligned": abs(ne - end) <= tolerance,
         "start_shift": round(ns - start, 3) if abs(ns - start) <= tolerance else 0.0,
         "end_shift": round(ne - end, 3) if abs(ne - end) <= tolerance else 0.0},
    )


def align_windows(windows, boundaries, *, tolerance=2.0):
    times = get_boundary_times(boundaries)
    result = []
    for w in windows:
        s, e, info = align_window(w["start"], w["end"], times, tolerance=tolerance)
        adjusted = {**w, "start": s, "end": e, "_alignment": info}
        result.append(adjusted)
    return result
