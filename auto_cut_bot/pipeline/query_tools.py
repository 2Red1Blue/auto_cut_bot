"""StoryAgent query tools — deterministic DB queries for scene search, character
data retrieval, and fact verification.

These are Python functions called by StoryAgent's query tools, not Tool classes.
All are deterministic DB queries except ``check_fact`` which does deterministic
evidence search and delegates final LLM verification to the caller.

Implements Doc 23 §3.3 and Doc 24 §4.3 search/retrieval tools.
"""

from __future__ import annotations

import re
from typing import Any


# ── Helpers ────────────────────────────────────────────────────────────────────


def _t(schema: str, table: str) -> str:
    """Return schema-qualified table name."""
    return f"{schema}.{table}"


def _db_available(db: Any) -> bool:
    return db is not None and getattr(db, "is_available", False)


_STOP_WORDS: set[str] = {
    "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都", "一",
    "说", "要", "去", "你", "会", "着", "也", "很", "到", "看", "好", "这",
    "他", "她", "它", "们", "那", "什么", "怎么", "哪", "吗", "吧", "啊",
    "因为", "所以", "但是", "如果", "虽然", "然后", "可以", "已经", "还是",
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "can", "shall", "to", "of", "in", "for",
    "on", "with", "at", "by", "from", "as", "and", "but", "or", "not",
    "no", "only", "it", "he", "she", "they", "we", "you", "me", "my",
}


def _extract_keywords(text: str) -> list[str]:
    """Extract meaningful keywords from a claim for DB full-text search."""
    tokens = re.findall(r"[一-鿿]+|[a-zA-Z]+|\d+", text.lower())
    return [t for t in tokens if len(t) >= 2 and t not in _STOP_WORDS]


def _intensity_field() -> str:
    """Expr for emotional intensity score from meta_tags JSONB."""
    return "COALESCE((meta_tags->>'intensity')::float, 0)"


# ── 1. search_scenes ───────────────────────────────────────────────────────────


def search_scenes(
    db: Any,
    book_id: str,
    characters: list[str] | None = None,
    location: str | None = None,
    episode_range: tuple[int, int] | None = None,
    min_intensity: float = 0.0,
    limit: int = 20,
) -> list[dict]:
    """Search scenes matching criteria. Deterministic DB query, zero LLM.

    Filters: characters (ANY match on characters_present), location (ILIKE),
    episode_range (inclusive), min_intensity (meta_tags->>'intensity').

    Returns list of scene dicts sorted by intensity_score DESC, with keys:
    scene_id, episode_id, scene_order, heading, location, characters_present,
    distilled_summary, meta_tags, start_time, end_time, intensity_score.
    """
    if not _db_available(db):
        return []

    s = db.schema
    conditions = ["s.book_id = %s"]
    params: list[Any] = [book_id]

    if characters:
        conditions.append("s.characters_present && %s::text[]")
        params.append(characters)
    if location:
        conditions.append("s.location ILIKE %s")
        params.append(f"%{location}%")
    if episode_range:
        conditions.append("s.episode_id >= %s AND s.episode_id <= %s")
        params.extend([episode_range[0], episode_range[1]])
    if min_intensity > 0.0:
        conditions.append(f"{_intensity_field()} >= %s")
        params.append(min_intensity)

    sql = f"""
        SELECT s.scene_id, s.episode_id, s.scene_order, s.heading,
               s.location, s.characters_present, s.distilled_summary,
               s.meta_tags, s.start_time, s.end_time,
               {_intensity_field()} AS intensity_score
        FROM {_t(s, 'scenes')} s
        WHERE {' AND '.join(conditions)}
        ORDER BY intensity_score DESC, s.episode_id, s.scene_order
        LIMIT %s
    """
    params.append(limit)
    rows = db._execute(sql, tuple(params))
    return [dict(r) for r in rows]


# ── 2. get_dialogue_samples ────────────────────────────────────────────────────


def get_dialogue_samples(
    db: Any,
    book_id: str,
    character: str,
    n: int = 5,
    scene_filter: str | None = None,
) -> list[dict]:
    """Get real dialogue samples for a character. Used for voice consistency.

    Samples are randomly selected from subtitles. Optionally scoped by
    scene_filter (scene_id prefix). Returns list of dicts with keys:
    episode_id, speaker, text, tone, start_time.
    """
    if not _db_available(db):
        return []

    s = db.schema
    conditions = ["sub.book_id = %s", "sub.speaker = %s"]
    params: list[Any] = [book_id, character]

    if scene_filter:
        conditions.append("sub.scene_id LIKE %s")
        params.append(f"{scene_filter}%")

    sql = f"""
        SELECT sub.episode_id, sub.speaker, sub.text, sub.tone, sub.start_time
        FROM {_t(s, 'subtitles')} sub
        WHERE {' AND '.join(conditions)}
        ORDER BY RANDOM()
        LIMIT %s
    """
    params.append(n)
    rows = db._execute(sql, tuple(params))
    return [dict(r) for r in rows]


# ── 3. get_character_coverage ──────────────────────────────────────────────────


def get_character_coverage(
    db: Any,
    book_id: str,
    character: str,
) -> dict:
    """Get character's total screentime, scene count, and episode distribution.

    Returns dict with: character, total_scenes, total_episodes,
    episode_distribution (list of {episode_id, scene_count, total_duration}),
    first_episode, last_episode. Returns zeroed defaults when DB is down.
    """
    if not _db_available(db):
        return {
            "character": character,
            "total_scenes": 0,
            "total_episodes": 0,
            "episode_distribution": [],
            "first_episode": None,
            "last_episode": None,
        }

    s = db.schema

    subj_rows = db._execute(
        f"""
            SELECT first_episode, last_episode
            FROM {_t(s, 'subjects')}
            WHERE book_id = %s AND name = %s
        """,
        (book_id, character),
    )
    first_ep = subj_rows[0]["first_episode"] if subj_rows else None
    last_ep = subj_rows[0]["last_episode"] if subj_rows else None

    scene_rows = db._execute(
        f"""
            SELECT episode_id,
                   COUNT(*) AS scene_count,
                   SUM(COALESCE((meta_tags->>'duration')::float, 0)) AS total_duration
            FROM {_t(s, 'scenes')}
            WHERE book_id = %s AND characters_present @> ARRAY[%s]::text[]
            GROUP BY episode_id ORDER BY episode_id
        """,
        (book_id, character),
    )

    distribution = [dict(r) for r in scene_rows]
    return {
        "character": character,
        "total_scenes": sum(r["scene_count"] for r in distribution),
        "total_episodes": len(distribution),
        "episode_distribution": distribution,
        "first_episode": first_ep,
        "last_episode": last_ep,
    }


# ── 4. get_relation_timeline ───────────────────────────────────────────────────


def get_relation_timeline(
    db: Any,
    book_id: str,
    char_a: str,
    char_b: str,
) -> list[dict]:
    """Get every scene where both characters appear, in chronological order.

    Returns list of scene dicts ordered by episode_id, scene_order. Keys:
    scene_id, episode_id, scene_order, heading, location, characters_present,
    distilled_summary, meta_tags, start_time, end_time.
    """
    if not _db_available(db):
        return []

    s = db.schema
    sql = f"""
        SELECT scene_id, episode_id, scene_order, heading, location,
               characters_present, distilled_summary, meta_tags, start_time, end_time
        FROM {_t(s, 'scenes')}
        WHERE book_id = %s AND characters_present @> ARRAY[%s, %s]::text[]
        ORDER BY episode_id, scene_order
    """
    rows = db._execute(sql, (book_id, char_a, char_b))
    return [dict(r) for r in rows]


# ── 5. get_emotion_peaks ───────────────────────────────────────────────────────


def get_emotion_peaks(
    db: Any,
    book_id: str,
    episode_range: tuple[int, int] | None = None,
    top_k: int = 10,
) -> list[dict]:
    """Get high-intensity scenes ranked by emotional intensity score.

    Reads meta_tags->>'intensity' from scenes. Optionally scoped by
    episode_range. Returns top_k scenes sorted by intensity_score DESC.
    Keys: scene_id, episode_id, scene_order, heading, location,
    characters_present, distilled_summary, meta_tags, start_time, end_time,
    intensity_score.
    """
    if not _db_available(db):
        return []

    s = db.schema
    conditions = ["book_id = %s"]
    params: list[Any] = [book_id]

    if episode_range:
        conditions.append("episode_id >= %s AND episode_id <= %s")
        params.extend([episode_range[0], episode_range[1]])

    sql = f"""
        SELECT scene_id, episode_id, scene_order, heading, location,
               characters_present, distilled_summary, meta_tags,
               start_time, end_time,
               {_intensity_field()} AS intensity_score
        FROM {_t(s, 'scenes')}
        WHERE {' AND '.join(conditions)}
        ORDER BY intensity_score DESC, episode_id
        LIMIT %s
    """
    params.append(top_k)
    rows = db._execute(sql, tuple(params))
    return [dict(r) for r in rows]


# ── 6. check_fact ──────────────────────────────────────────────────────────────


def check_fact(
    db: Any,
    book_id: str,
    claim: str,
) -> dict:
    """Verify a factual claim against source data.

    Deterministic keyword search across scenes and subtitles. The ``supported``
    decision is heuristic; the caller (StoryAgent) should perform single-point
    LLM verification for the definitive answer.

    Returns: {supported: bool, evidence: list[dict], confidence: float}.
    confidence is capped at 0.8 without LLM verification.
    """
    if not _db_available(db):
        return {"supported": False, "evidence": [], "confidence": 0.0}

    s = db.schema
    keywords = _extract_keywords(claim)
    if not keywords:
        return {"supported": False, "evidence": [], "confidence": 0.0}

    search = keywords[:5]
    evidence: list[dict] = []

    # Search scenes
    sc_conds: list[str] = []
    sc_params: list[Any] = [book_id]
    for kw in search:
        sc_conds.append(
            "(s.distilled_summary ILIKE %s OR s.raw_description ILIKE %s OR s.heading ILIKE %s)"
        )
        sc_params.extend([f"%{kw}%", f"%{kw}%", f"%{kw}%"])

    sc_sql = f"""
        SELECT s.scene_id, s.episode_id, s.heading, s.distilled_summary,
               s.characters_present, s.meta_tags
        FROM {_t(s, 'scenes')} s
        WHERE s.book_id = %s AND ({' OR '.join(sc_conds)})
        LIMIT 10
    """
    for row in db._execute(sc_sql, tuple(sc_params)):
        evidence.append({
            "source": "scene",
            "scene_id": row["scene_id"],
            "episode_id": row["episode_id"],
            "heading": row["heading"],
            "summary": row["distilled_summary"],
            "characters": row["characters_present"],
            "meta_tags": row["meta_tags"],
        })

    # Search subtitles
    sub_conds: list[str] = []
    sub_params: list[Any] = [book_id]
    for kw in search:
        sub_conds.append("sub.text ILIKE %s")
        sub_params.append(f"%{kw}%")

    sub_sql = f"""
        SELECT sub.episode_id, sub.speaker, sub.text, sub.tone
        FROM {_t(s, 'subtitles')} sub
        WHERE sub.book_id = %s AND ({' OR '.join(sub_conds)})
        LIMIT 10
    """
    for row in db._execute(sub_sql, tuple(sub_params)):
        evidence.append({
            "source": "subtitle",
            "episode_id": row["episode_id"],
            "speaker": row["speaker"],
            "text": row["text"],
            "tone": row["tone"],
        })

    # Confidence heuristic (capped at 0.8 — caller should verify with LLM)
    if evidence:
        coverage = sum(
            1 for kw in search
            if any(kw.lower() in str(e.get("summary", "") + str(e.get("text", "")))
                   for e in evidence)
        )
        coverage_ratio = coverage / len(search)
        evidence_score = min(len(evidence) / 10.0, 0.5)
        confidence = min(0.3 + evidence_score + coverage_ratio * 0.2, 0.8)
    else:
        confidence = 0.0

    supported = len(evidence) > 0 and confidence >= 0.3

    return {
        "supported": supported,
        "evidence": evidence,
        "confidence": round(confidence, 2),
    }