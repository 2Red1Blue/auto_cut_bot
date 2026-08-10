"""Derived assets builder — deterministic DB queries for computed data assets
from existing pipeline tables with zero LLM cost.  Implements Doc 24 S3.

Components:
  1. build_character_appearance_index — character -> [(episode, scene, ts), ...]
  2. build_emotion_curve — per-scene intensity from meta_tags + rule-based dialogue
  3. build_relationship_timeline — co-appearance timeline with relation inference
  4. build_episode_coverage — per-episode stats: scene/character count, duration, intensity
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any


# ── Helpers ────────────────────────────────────────────────────────────────────────


def _t(schema: str, table: str) -> str:
    return f"{schema}.{table}"


def _db_available(db: Any) -> bool:
    return db is not None and getattr(db, "is_available", False)


def _intensity_field() -> str:
    return "COALESCE((meta_tags->>'intensity')::float, 0)"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Rule-based scoring constants ────────────────────────────────────────────────────

_CONFLICT_KW: list[str] = [
    "杀", "死", "滚", "恨", "背叛", "害", "毁", "灭", "仇",
    "不", "别", "绝不", "休想", "骗", "谎", "瞒", "滚开",
    "kill", "die", "hate", "destroy", "betray", "liar", "never",
]
_EXCLAMATION_RE: re.Pattern = re.compile(r"[！!]{1,}")


# ── 1. build_character_appearance_index ────────────────────────────────────────────


def build_character_appearance_index(
    db: Any,
    book_id: str,
) -> dict[str, list[dict[str, Any]]]:
    """Build character -> [(episode, scene, start_ts, end_ts), ...] index.

    Reads scenes.characters_present and start/end_time.  Characters sorted by
    name; each character's scenes sorted by (episode_id, scene_order).
    Returns {} when DB is unavailable.
    """
    if not _db_available(db):
        return {}

    s = db.schema
    char_rows = db._execute(
        f"""
            SELECT DISTINCT unnest(characters_present) AS name
            FROM {_t(s, 'scenes')} WHERE book_id = %s ORDER BY name
        """,
        (book_id,),
    )
    characters = [r["name"] for r in char_rows]
    if not characters:
        return {}

    scene_rows = db._execute(
        f"""
            SELECT characters_present, episode_id, scene_id, scene_order,
                   start_time, end_time
            FROM {_t(s, 'scenes')}
            WHERE book_id = %s ORDER BY episode_id, scene_order
        """,
        (book_id,),
    )

    char_scenes: dict[str, list[dict[str, Any]]] = {ch: [] for ch in characters}
    for row in scene_rows:
        entry = {
            "episode_id": row["episode_id"],
            "scene_id": row["scene_id"],
            "start_ts": row["start_time"],
            "end_ts": row["end_time"],
        }
        for ch in (row["characters_present"] or []):
            if ch in char_scenes:
                char_scenes[ch].append(entry)

    return dict(sorted(char_scenes.items()))


# ── 2. build_emotion_curve ─────────────────────────────────────────────────────────


def build_emotion_curve(
    db: Any,
    book_id: str,
    episode_range: tuple[int, int] | None = None,
) -> list[dict[str, Any]]:
    """Build per-scene emotional intensity curve via rule-based scoring.

    Combines meta_tags->>'intensity' with dialogue analysis (conflict keyword
    count + exclamation density).  Final score = max(meta_intensity, dialogue_score),
    clipped to [0, 10].  Returns list sorted by intensity_score DESC.

    Args:
        episode_range: Optional (min_ep, max_ep) inclusive filter.
    """
    if not _db_available(db):
        return []

    s = db.schema
    conditions = ["sc.book_id = %s"]
    params: list[Any] = [book_id]

    if episode_range:
        conditions.append("sc.episode_id >= %s AND sc.episode_id <= %s")
        params.extend([episode_range[0], episode_range[1]])

    sql = f"""
        SELECT sc.scene_id, sc.episode_id, sc.scene_order, sc.heading,
               {_intensity_field()} AS meta_intensity,
               COALESCE(sub_stats.dialogue_text, '') AS dialogue_text
        FROM {_t(s, 'scenes')} sc
        LEFT JOIN LATERAL (
            SELECT string_agg(sub.text, ' ') AS dialogue_text
            FROM {_t(s, 'subtitles')} sub
            WHERE sub.book_id = sc.book_id
              AND sub.episode_id = sc.episode_id
              AND sub.start_time >= COALESCE(sc.start_time, 0)
              AND sub.start_time <= COALESCE(sc.end_time, 999999)
        ) sub_stats ON true
        WHERE {' AND '.join(conditions)}
        ORDER BY sc.episode_id, sc.scene_order
    """
    rows = db._execute(sql, tuple(params))

    result: list[dict[str, Any]] = []
    for row in rows:
        text: str = row["dialogue_text"] or ""
        meta_intensity: float = float(row["meta_intensity"] or 0.0)

        conflict_count = sum(1 for kw in _CONFLICT_KW if kw in text)
        excl_count = len(_EXCLAMATION_RE.findall(text))

        conflict_score = min(conflict_count * 1.5, 7.0)
        excl_score = min(excl_count * 0.3, 3.0)
        dialogue_score = conflict_score + excl_score
        intensity_score = round(min(max(meta_intensity, dialogue_score), 10.0), 2)

        result.append({
            "scene_id": row["scene_id"],
            "episode_id": row["episode_id"],
            "scene_order": row["scene_order"],
            "heading": row["heading"],
            "intensity_score": intensity_score,
            "conflict_keyword_count": conflict_count,
            "exclamation_count": excl_count,
            "dialogue_score": round(dialogue_score, 2),
            "meta_intensity": round(meta_intensity, 2),
        })

    return sorted(result, key=lambda x: x["intensity_score"], reverse=True)


# ── 3. build_relationship_timeline ─────────────────────────────────────────────────


def build_relationship_timeline(
    db: Any,
    book_id: str,
    char_a: str,
    char_b: str,
) -> list[dict[str, Any]]:
    """Build chronological co-appearance timeline for two characters.

    Finds all scenes where both characters appear, scoped by valid_episode_range
    from subjects.first_episode/last_episode (Doc 22 intersection).  Each result
    includes a heuristic relation_inference label (hostile, intimate, tense,
    private, neutral).  Returns [] when DB is unavailable.
    """
    if not _db_available(db):
        return []

    s = db.schema

    # Fetch valid episode ranges (Doc 22)
    range_rows = db._execute(
        f"""
            SELECT name, first_episode, last_episode
            FROM {_t(s, 'subjects')}
            WHERE book_id = %s AND name IN (%s, %s)
        """,
        (book_id, char_a, char_b),
    )
    ranges: dict[str, tuple[int | None, int | None]] = {}
    for r in range_rows:
        ranges[r["name"]] = (r["first_episode"], r["last_episode"])

    ra, rb = ranges.get(char_a, (None, None)), ranges.get(char_b, (None, None))
    _min = max(ra[0], rb[0]) if ra[0] is not None and rb[0] is not None else None
    _max = min(ra[1], rb[1]) if ra[1] is not None and rb[1] is not None else None

    conditions = [
        "book_id = %s",
        "characters_present @> ARRAY[%s, %s]::text[]",
    ]
    params: list[Any] = [book_id, char_a, char_b]

    if _min is not None:
        conditions.append("episode_id >= %s")
        params.append(_min)
    if _max is not None:
        conditions.append("episode_id <= %s")
        params.append(_max)

    sql = f"""
        SELECT scene_id, episode_id, scene_order, heading, location,
               characters_present, distilled_summary, meta_tags,
               start_time, end_time,
               {_intensity_field()} AS intensity_score
        FROM {_t(s, 'scenes')}
        WHERE {' AND '.join(conditions)}
        ORDER BY episode_id, scene_order
    """
    rows = db._execute(sql, tuple(params))

    result: list[dict[str, Any]] = []
    for row in rows:
        meta_tags = row["meta_tags"] or {}
        present = row["characters_present"] or []
        result.append({
            "scene_id": row["scene_id"],
            "episode_id": row["episode_id"],
            "scene_order": row["scene_order"],
            "heading": row["heading"],
            "location": row["location"],
            "characters_present": present,
            "distilled_summary": row["distilled_summary"],
            "meta_tags": meta_tags,
            "start_time": row["start_time"],
            "end_time": row["end_time"],
            "intensity_score": row["intensity_score"],
            "relation_inference": _infer_relation(meta_tags, char_a, char_b, present),
        })

    return result


def _infer_relation(
    meta_tags: dict[str, Any],
    char_a: str,
    char_b: str,
    present_characters: list[str],
) -> str:
    """Heuristic relation label from meta_tags conflict/affection/mood signals."""
    c_tag = meta_tags.get("conflict", "")
    a_tag = meta_tags.get("affection", "")
    mood = str(meta_tags.get("mood", "")).lower()

    cs = (
        isinstance(c_tag, str) and c_tag.lower() in ("high", "true", "1")
    ) or (isinstance(c_tag, bool) and c_tag)
    as_ = (
        isinstance(a_tag, str) and a_tag.lower() in ("high", "true", "1")
    ) or (isinstance(a_tag, bool) and a_tag)

    if cs and not as_:
        return "hostile"
    if as_ and not cs:
        return "intimate"
    if cs and as_:
        return "tense"

    if len([c for c in present_characters if c not in (char_a, char_b)]) == 0:
        return "private"

    if "angry" in mood or "tense" in mood:
        return "hostile"
    if "romantic" in mood or "sweet" in mood:
        return "intimate"

    return "neutral"


# ── 4. build_episode_coverage ──────────────────────────────────────────────────────


def build_episode_coverage(
    db: Any,
    book_id: str,
) -> dict[str, Any]:
    """Build per-episode coverage statistics.

    Returns {book_id, generated_at, episodes: [...], summary: {...}}.
    Per-episode: scene_count, character_count, total_duration, mean_intensity.
    Summary: totals across episodes plus distinct character count.
    Returns zeroed defaults when DB is unavailable.
    """
    empty = {
        "book_id": book_id,
        "generated_at": _utc_now(),
        "episodes": [],
        "summary": {
            "total_episodes": 0, "total_scenes": 0,
            "total_characters": 0, "total_duration": 0.0, "mean_intensity": 0.0,
        },
    }
    if not _db_available(db):
        return empty

    s = db.schema
    rows = db._execute(
        f"""
            SELECT sc.episode_id,
                   COUNT(*) AS scene_count,
                   COUNT(DISTINCT uc) AS character_count,
                   SUM(COALESCE((sc.meta_tags->>'duration')::float, 0)) AS total_duration,
                   AVG({_intensity_field()}) AS mean_intensity
            FROM {_t(s, 'scenes')} sc,
                 LATERAL unnest(sc.characters_present) AS uc
            WHERE sc.book_id = %s
            GROUP BY sc.episode_id ORDER BY sc.episode_id
        """,
        (book_id,),
    )

    episodes: list[dict[str, Any]] = []
    total_scenes, total_duration, total_intensity = 0, 0.0, 0.0

    for row in rows:
        episodes.append({
            "episode_id": row["episode_id"],
            "scene_count": row["scene_count"],
            "character_count": row["character_count"],
            "total_duration": round(float(row["total_duration"] or 0), 2),
            "mean_intensity": round(float(row["mean_intensity"] or 0), 2),
        })
        total_scenes += row["scene_count"]
        total_duration += float(row["total_duration"] or 0)
        total_intensity += float(row["mean_intensity"] or 0)

    mean_intensity = round(total_intensity / len(episodes), 2) if episodes else 0.0

    if episodes:
        char_rows = db._execute(
            f"""
                SELECT DISTINCT unnest(characters_present) AS name
                FROM {_t(s, 'scenes')} WHERE book_id = %s
            """,
            (book_id,),
        )
        all_characters = {r["name"] for r in char_rows}
    else:
        all_characters = set()

    return {
        "book_id": book_id,
        "generated_at": _utc_now(),
        "episodes": episodes,
        "summary": {
            "total_episodes": len(episodes),
            "total_scenes": total_scenes,
            "total_characters": len(all_characters),
            "total_duration": round(total_duration, 2),
            "mean_intensity": mean_intensity,
        },
    }