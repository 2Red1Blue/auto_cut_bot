"""Filmability gate — pre-approval material coverage check (Doc 24 §5.3).

Verifies every beat in a Story Treatment has enough usable media footage
to be rendered, before the treatment is approved for downstream stages."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

COVERAGE_THRESHOLD_PCT: float = 0.80
DEFAULT_EPISODE: int = 1
MIN_USABLE_DURATION: float = 0.5


# ── 1. MediaInterval ─────────────────────────────────────────────────────────

@dataclass
class MediaInterval:
    """A resolved time range within a source media file.

    source_file: Source id (e.g. "source-001"). start_time / end_time: in
    seconds. duration: computed (end - start). usable: False when episode is
    paywalled or excluded by policy. metadata: extra context.
    """

    source_file: str = ""
    start_time: float = 0.0
    end_time: float = 0.0
    duration: float = 0.0
    usable: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.duration == 0.0 and self.end_time > self.start_time:
            self.duration = round(self.end_time - self.start_time, 3)


# ── 2. FilmabilityReport ────────────────────────────────────────────────────

@dataclass
class FilmabilityReport:
    """Per-beat coverage verdict: beat_id, usable_seconds, required_seconds,
    status (ok/starved), coverage_pct, intervals, warnings."""
    beat_id: str = ""
    usable_seconds: float = 0.0
    required_seconds: float = 0.0
    status: str = "ok"
    coverage_pct: float = 0.0
    intervals: list[MediaInterval] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.required_seconds <= 0:
            self.coverage_pct = 0.0
            self.status = "starved"
        else:
            self.coverage_pct = min(self.usable_seconds / self.required_seconds, 1.0)
            self.status = "ok" if self.coverage_pct >= COVERAGE_THRESHOLD_PCT else "starved"


# ── 3. resolve_source_refs ──────────────────────────────────────────────────

def resolve_source_refs(
    source_refs: list[dict[str, Any]],
    db: Any,
    *,
    free_only: bool = True,
) -> list[MediaInterval]:
    """Convert beat source_refs into concrete MediaIntervals.

    Each source_ref may contain: episode, scene_id, start_time, end_time,
    source_id, event_type.  Queries DB boundaries (or scenes as fallback),
    merges overlapping intervals, and marks non-free episode intervals as
    unusable when free_only=True.

    Args:
        source_refs: Reference dicts from a treatment beat.
        db: Client supporting ``db.query(sql, params) -> list[dict]``.
        free_only: If True, exclude intervals from non-free episodes.

    Returns:
        Deduplicated, merged list of MediaIntervals.
    """
    if not source_refs:
        return []

    free_episodes: set[int] = set()
    if free_only:
        try:
            rows = db.query("SELECT episode_id FROM episodes WHERE is_free = TRUE;")
            free_episodes = {int(r["episode_id"]) for r in rows}
        except Exception:
            free_episodes = set()

    intervals: list[MediaInterval] = []
    for ref in source_refs:
        episode = ref.get("episode")
        scene_id = ref.get("scene_id")
        source_id = ref.get("source_id")
        event_type = ref.get("event_type")

        # Explicit time range — no DB lookup needed.
        if "start_time" in ref and "end_time" in ref:
            start = float(ref["start_time"])
            end = float(ref["end_time"])
            if end <= start or (end - start) < MIN_USABLE_DURATION:
                continue
            ep = episode or DEFAULT_EPISODE
            intervals.append(MediaInterval(
                source_file=source_id or f"source-{ep:03d}",
                start_time=start, end_time=end,
                usable=not free_only or ep in free_episodes,
                metadata={"episode": ep, "ref_type": "explicit"},
            ))
            continue

        # DB-backed resolution: boundaries first, then scenes as fallback.
        for row in _query_intervals(db, episode, scene_id, event_type):
            ep = row.get("episode_id", episode or DEFAULT_EPISODE)
            dur = float(row.get("end_time", 0)) - float(row.get("start_time", 0))
            if dur < MIN_USABLE_DURATION:
                continue
            intervals.append(MediaInterval(
                source_file=source_id or f"source-{ep:03d}",
                start_time=float(row.get("start_time", 0)),
                end_time=float(row.get("end_time", 0)),
                usable=not free_only or ep in free_episodes,
                metadata={
                    "episode": ep, "ref_type": "db_reference",
                    "boundary_id": row.get("boundary_id"),
                    "scene_id": row.get("scene_id"),
                    "event_type": row.get("event_type"),
                    "confidence": row.get("confidence"),
                },
            ))

    return _merge_overlapping(intervals)


# ── 4. filmability_check ────────────────────────────────────────────────────

def filmability_check(
    treatment: dict[str, Any],
    source_manifest: dict[str, Any],
    db: Any,
) -> list[FilmabilityReport]:
    """Run the filmability gate over every beat in a treatment.

    For each beat: resolve source_refs into intervals, sum usable durations,
    compare against required_seconds, emit a FilmabilityReport.

    Args:
        treatment: Dict with ``"beats"`` list containing ``"id"``,
            ``"required_seconds"``, and ``"source_refs"`` per beat.
        source_manifest: source_manifest.json (reserved for future cross-
            validation of source file existence).
        db: Client supporting ``db.query(sql, params) -> list[dict]``.

    Returns:
        One FilmabilityReport per beat; starved beats have status "starved".
    """
    beats = treatment.get("beats")
    if not isinstance(beats, list) or not beats:
        return []

    reports: list[FilmabilityReport] = []
    for beat in beats:
        if not isinstance(beat, dict):
            continue
        beat_id = beat.get("id", "unknown")
        required = float(beat.get("required_seconds", 0))
        source_refs = beat.get("source_refs")
        if not isinstance(source_refs, list):
            source_refs = []

        intervals = resolve_source_refs(source_refs, db, free_only=True)
        usable = sum(iv.duration for iv in intervals if iv.usable)

        warnings: list[str] = []
        total_available = sum(iv.duration for iv in intervals)
        if total_available > usable:
            warnings.append(
                f"{total_available - usable:.1f}s excluded (non-free episodes or policy)"
            )

        reports.append(FilmabilityReport(
            beat_id=beat_id,
            usable_seconds=round(usable, 3),
            required_seconds=required,
            intervals=intervals,
            warnings=warnings,
        ))
    return reports


# ── Internal helpers ─────────────────────────────────────────────────────────

def _query_intervals(
    db: Any, episode: int | None, scene_id: str | None, event_type: str | None
) -> list[dict[str, Any]]:
    """Query boundaries (preferred) or scenes for matching time intervals."""
    conditions: list[str] = []
    params: list[Any] = []
    if episode is not None:
        conditions.append("episode_id = %s"); params.append(episode)
    if scene_id is not None:
        conditions.append("scene_id = %s"); params.append(scene_id)
    if event_type is not None:
        conditions.append("event_type = %s"); params.append(event_type)
    where = " AND ".join(conditions) if conditions else "1=1"
    for table in ("boundaries", "scenes"):
        try:
            rows = db.query(
                f"SELECT * FROM {table} WHERE {where} ORDER BY start_time;", params
            )
            if rows:
                return rows
        except Exception:
            continue
    return []


def _merge_overlapping(intervals: list[MediaInterval]) -> list[MediaInterval]:
    """Merge overlapping or adjacent intervals (gap <= 0.5s) within the same
    source file.  Usability is ANDed across constituents."""
    if not intervals:
        return []
    sorted_iv = sorted(intervals, key=lambda iv: (iv.source_file, iv.start_time))
    merged: list[MediaInterval] = []
    cur = sorted_iv[0]
    for nxt in sorted_iv[1:]:
        if nxt.source_file == cur.source_file and nxt.start_time <= cur.end_time + 0.5:
            cur.end_time = max(cur.end_time, nxt.end_time)
            cur.duration = round(cur.end_time - cur.start_time, 3)
            cur.usable = cur.usable and nxt.usable
            cur.metadata["merged_count"] = cur.metadata.get("merged_count", 1) + 1
        else:
            merged.append(cur)
            cur = nxt
    merged.append(cur)
    return merged


__all__ = [
    "MediaInterval",
    "FilmabilityReport",
    "COVERAGE_THRESHOLD_PCT",
    "resolve_source_refs",
    "filmability_check",
]