"""StoryAgent query tools — deterministic DB queries for scene search, character data
retrieval, and fact verification.

These are Python functions called by the query tools that StoryAgent uses, not Tool
classes. All functions are deterministic DB queries except ``check_fact`` which
performs deterministic evidence search and delegates final verification to the
StoryAgent LLM.

Implements Doc 23 §3.3 and Doc 24 §4.3 search/retrieval tools.
"""

from __future__ import annotations

from typing import Any

from auto_cut_bot.pipeline.core.db.client import StageDBClient


# ── Helpers ────────────────────────────────────────────────────────────────────


def _t(schema: str, table: str) -> str:
    """Return schema-qualified table name: ``autocut.scenes``."""
    return f"{schema}.{table}"


def _db_available(db: Any) -> bool:
    """Check if the StageDBClient is available and connected."""
    return db is not None and getattr(db, "is_available", False)


def _extract_keywords(text: str) -> list[str]:
    """Extract meaningful keywords from a claim for DB full-text search.

    Filters out common stop words in both Chinese and English, and tokens
    shorter than two characters.
    """
    import re

    _STOP_WORDS: set[str] = {
        # Chinese stop words
        "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都", "一",
        "一个", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着",
        "没有", "看", "好", "自己", "这", "他", "她", "它", "们", "那", "些",
        "什么", "怎么", "哪", "吗", "吧", "啊", "呢", "哦", "嗯", "哈", "哇",
        "因为", "所以", "但是", "如果", "虽然", "然后", "可以", "已经", "这个",
        "那个", "还是", "只是", "不过", "而且", "或者", "一直", "一定", "怎么",
        # English stop words
        "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "do", "does", "did", "will", "would", "could",
        "should", "may", "might", "can", "shall", "to", "of", "in", "for",
        "on", "with", "at", "by", "from", "as", "into", "through", "during",
        "before", "after", "above", "below", "between", "and", "but", "or",
        "nor", "not", "so", "yet", "both", "either", "neither", "each",
        "every", "all", "any", "few", "more", "most", "other", "some",
        "such", "no", "only", "own", "same", "than", "too", "very",
        "it", "its", "he", "him", "his", "she", "her", "they", "them",
        "their", "we", "us", "our", "you", "your", "me", "my", "mine",
    }

    # Split on CJK characters, alphabetic words, and digits
    tokens = re.findall(r"[一-鿿]+|[a-zA-Z]+|\d+", text.lower())

    keywords: list[str] = []
    for token in tokens:
        if len(token) >= 2 and token not in _STOP_WORDS:
            keywords.append(token)

    return keywords


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

    Args:
        db: StageDBClient instance (or any object with ``is_available`` and
            ``_execute``).
        book_id: Book identifier.
        characters: If provided, only return scenes where at least one of
            these characters appears in ``characters_present``.
        location: If provided, case-insensitive substring match against the
            ``location`` column.
        episode_range: ``(min_episode, max_episode)`` inclusive filter.
        min_intensity: Minimum emotional intensity score from
            ``meta_tags->>'intensity'`` (float, defaults to 0).
        limit: Maximum number of results (default 20).

    Returns:
        List of scene dicts sorted by intensity_score descending, then
        episode_id and scene_order. Each dict has keys: ``scene_id``,
        ``episode_id``, ``scene_order``, ``heading``, ``location``,
        ``characters_present``, ``distilled_summary``, ``meta_tags``,
        ``start_time``, ``end_time``, ``intensity_score``.
        Returns an empty list when the DB is unavailable.
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
        conditions.append(
            "COALESCE((s.meta_tags->>'intensity')::float, 0) >= %s"
        )
        params.append(min_intensity)

    sql = f"""
        SELECT s.scene_id, s.episode_id, s.scene_order, s.heading,
               s.location, s.characters_present, s.distilled_summary,
               s.meta_tags, s.start_time, s.end_time,
               COALESCE((s.meta_tags->>'intensity')::float, 0) AS intensity_score
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

    Samples are randomly selected from the subtitles table to avoid bias
    toward any particular episode or scene.

    Args:
        db: StageDBClient instance.
        book_id: Book identifier.
        character: Character name to filter by (matches ``speaker`` column).
        n: Number of samples to return (default 5).
        scene_filter: Optional scene_id prefix to narrow the scope. Useful
            for getting samples from a specific episode or scene group.

    Returns:
        List of dialogue dicts with keys: ``episode_id``, ``speaker``,
        ``text``, ``tone``, ``start_time``.
        Returns an empty list when the DB is unavailable.
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
        SELECT sub.episode_id, sub.speaker, sub.text, sub.tone,
               sub.start_time
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

    Computes aggregate statistics from the scenes table and supplements
    with subject metadata from the subjects table.

    Args:
        db: StageDBClient instance.
        book_id: Book identifier.
        character: Character name.

    Returns:
        Dict with keys:
          - ``character``: str — the character name
          - ``total_scenes``: int — total scenes this character appears in
          - ``total_episodes``: int — number of distinct episodes
          - ``episode_distribution``: list[dict] — per-episode breakdown with
            ``episode_id``, ``scene_count``, ``total_duration``
          - ``first_episode``: int | None — from subjects table
          - ``last_episode``: int | None — from subjects table
        Returns defaults (0 / None) when DB is unavailable.
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

    # Subject metadata (first/last episode)
    subject_rows = db._execute(
        f"""
            SELECT first_episode, last_episode
            FROM {_t(s, 'subjects')}
            WHERE book_id = %s AND name = %s
        """,
        (book_id, character),
    )
    first_ep = subject_rows[0]["first_episode"] if subject_rows else None
    last_ep = subject_rows[0]["last_episode"] if subject_rows else None

    # Per-episode scene statistics
    scene_rows = db._execute(
        f"""
            SELECT episode_id,
                   COUNT(*) AS scene_count,
                   SUM(COALESCE((meta_tags->>'duration')::float, 0)) AS total_duration
            FROM {_t(s, 'scenes')}
            WHERE book_id = %s
              AND characters_present @> ARRAY[%s]::text[]
            GROUP BY episode_id
            ORDER BY episode_id
        """,
        (book_id, character),
    )

    distribution = [dict(r) for r in scene_rows]
    total_scenes = sum(r["scene_count"] for r in distribution)
    total_episodes = len(distribution)

    return {
        "character": character,
        "total_scenes": total_scenes,
        "total_episodes": total_episodes,
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
    """Get relationship evolution timeline — every scene where both characters appear.

    Returns scenes in chronological order so the caller can trace how the
    relationship evolves across episodes.

    Args:
        db: StageDBClient instance.
        book_id: Book identifier.
        char_a: First character name.
        char_b: Second character name.

    Returns:
        List of scene dicts ordered by ``episode_id`` then ``scene_order``.
        Each dict has keys: ``scene_id``, ``episode_id``, ``scene_order``,
        ``heading``, ``location``, ``characters_present``,
        ``distilled_summary``, ``meta_tags``, ``start_time``, ``end_time``.
        Returns an empty list when the DB is unavailable.
    """
    if not _db_available(db):
        return []

    s = db.schema
    sql = f"""
        SELECT scene_id, episode_id, scene_order, heading, location,
               characters_present, distilled_summary, meta_tags,
               start_time, end_time
        FROM {_t(s, 'scenes')}
        WHERE book_id = %s
          AND characters_present @> ARRAY[%s, %s]::text[]
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

    Reads ``meta_tags->>'intensity'`` from the scenes table and returns
    the top-k scenes sorted by that score.

    Args:
        db: StageDBClient instance.
        book_id: Book identifier.
        episode_range: Optional ``(min_episode, max_episode)`` inclusive filter.
        top_k: Number of top scenes to return (default 10).

    Returns:
        List of scene dicts sorted by intensity_score descending, then
        episode_id. Each dict has keys: ``scene_id``, ``episode_id``,
        ``scene_order``, ``heading``, ``location``, ``characters_present``,
        ``distilled_summary``, ``meta_tags``, ``start_time``, ``end_time``,
        ``intensity_score``.
        Returns an empty list when the DB is unavailable.
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
               COALESCE((meta_tags->>'intensity')::float, 0) AS intensity_score
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

    Performs deterministic keyword-based search across both scenes and
    subtitles to gather evidence. The final ``supported`` decision is a
    heuristic based on evidence quantity; the caller (StoryAgent) should
    perform single-point LLM verification on the returned evidence for
    the definitive answer.

    Args:
        db: StageDBClient instance.
        book_id: Book identifier.
        claim: Natural language claim to verify (e.g., "The protagonist
            confesses in episode 42").

    Returns:
        Dict with keys:
          - ``supported``: bool — whether the claim is supported by source data
          - ``evidence``: list[dict] — supporting or contradicting evidence,
            each with ``source`` (``"scene"`` or ``"subtitle"``) and relevant
            columns
          - ``confidence``: float — 0.0 to 1.0, based on evidence quantity and
            keyword coverage (capped at 0.8 without LLM verification)
    """
    if not _db_available(db):
        return {"supported": False, "evidence": [], "confidence": 0.0}

    s = db.schema

    keywords = _extract_keywords(claim)
    if not keywords:
        return {"supported": False, "evidence": [], "confidence": 0.0}

    # Limit to top 5 keywords for performance.
    search_keywords = keywords[:5]
    evidence: list[dict] = []

    # ── Search scenes ────────────────────────────────────────────────────
    scene_conditions: list[str] = []
    scene_params: list[Any] = [book_id]
    for kw in search_keywords:
        scene_conditions.append(
            "(s.distilled_summary ILIKE %s "
            "OR s.raw_description ILIKE %s "
            "OR s.heading ILIKE %s)"
        )
        scene_params.extend([f"%{kw}%", f"%{kw}%", f"%{kw}%"])

    scene_sql = f"""
        SELECT s.scene_id, s.episode_id, s.heading, s.distilled_summary,
               s.characters_present, s.meta_tags
        FROM {_t(s, 'scenes')} s
        WHERE s.book_id = %s
          AND ({' OR '.join(scene_conditions)})
        LIMIT 10
    """
    scene_rows = db._execute(scene_sql, tuple(scene_params))
    for row in scene_rows:
        evidence.append({
            "source": "scene",
            "scene_id": row["scene_id"],
            "episode_id": row["episode_id"],
            "heading": row["heading"],
            "summary": row["distilled_summary"],
            "characters": row["characters_present"],
            "meta_tags": row["meta_tags"],
        })

    # ── Search subtitles ─────────────────────────────────────────────────
    sub_conditions: list[str] = []
    sub_params: list[Any] = [book_id]
    for kw in search_keywords:
        sub_conditions.append("sub.text ILIKE %s")
        sub_params.append(f"%{kw}%")

    sub_sql = f"""
        SELECT sub.episode_id, sub.speaker, sub.text, sub.tone
        FROM {_t(s, 'subtitles')} sub
        WHERE sub.book_id = %s
          AND ({' OR '.join(sub_conditions)})
        LIMIT 10
    """
    sub_rows = db._execute(sub_sql, tuple(sub_params))
    for row in sub_rows:
        evidence.append({
            "source": "subtitle",
            "episode_id": row["episode_id"],
            "speaker": row["speaker"],
            "text": row["text"],
            "tone": row["tone"],
        })

    # ── Compute confidence ───────────────────────────────────────────────
    if evidence:
        # Heuristic: more evidence + higher keyword coverage = higher confidence.
        # Capped at 0.8 — the caller should use LLM verification for > 0.8.
        # The evidence count is clamped to avoid over-weighting many weak hits.
        keyword_coverage = sum(
            1 for kw in search_keywords
            if any(kw.lower() in str(e.get("summary", ""))
                   + str(e.get("text", ""))
                   for e in evidence)
        )
        coverage_ratio = keyword_coverage / len(search_keywords)
        evidence_score = min(len(evidence) / 10.0, 0.5)
        confidence = round(0.3 + evidence_score + coverage_ratio * 0.2, 2)
        confidence = min(confidence, 0.8)
    else:
        confidence = 0.0

    supported = len(evidence) > 0 and confidence >= 0.3

    return {
        "supported": supported,
        "evidence": evidence,
        "confidence": confidence,
    }