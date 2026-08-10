"""Deterministic semantic matching operators — zero LLM, pure Python.

Unlike ``autocut_core/semantic/`` which delegates to LLM calls, this module provides
fast, deterministic matching operators for scene-to-beat alignment, character name
fuzzy matching, and timeline alignment. All operators are synchronous and side-effect-free.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Sequence


# ── Text Normalisation ────────────────────────────────────────────────────────────

_STOP_WORDS: frozenset[str] = frozenset({
    "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都", "一",
    "说", "要", "去", "你", "会", "着", "也", "很", "到", "看", "好", "这",
    "他", "她", "它", "们", "那", "什么", "怎么", "哪", "吗", "吧", "啊",
    "因为", "所以", "但是", "如果", "虽然", "然后", "可以", "已经", "还是",
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "can", "shall", "to", "of", "in", "for",
    "on", "with", "at", "by", "from", "as", "and", "but", "or", "not",
    "no", "only", "it", "he", "she", "they", "we", "you", "me", "my",
})


def _normalise(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace, remove stop words."""
    text = re.sub(r"[^\w\s]", " ", text.lower())
    tokens = [t for t in text.split() if t not in _STOP_WORDS]
    return " ".join(tokens)


def _token_similarity(a: str, b: str) -> float:
    """Jaccard coefficient between two normalised token sets."""
    tokens_a = set(_normalise(a).split())
    tokens_b = set(_normalise(b).split())
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)


# ── Scene-Beat Matching ───────────────────────────────────────────────────────────


def match_scene_to_beat(
    scene: str,
    beat: str,
    *,
    description_weight: float = 0.6,
    dialogue_weight: float = 0.4,
) -> float:
    """Compute deterministic semantic similarity between a scene and a story beat.

    Uses a weighted combination of difflib SequenceMatcher (structural) and
    token-level Jaccard overlap (keyword/concept). Returns a float in [0.0, 1.0].
    """
    if not scene.strip() or not beat.strip():
        return 0.0
    seq_score = difflib.SequenceMatcher(
        None, _normalise(scene), _normalise(beat)
    ).ratio()
    token_score = _token_similarity(scene, beat)
    return round(description_weight * seq_score + dialogue_weight * token_score, 4)


# ── Scene Ranking ─────────────────────────────────────────────────────────────────


@dataclass
class ScoredScene:
    """A scene with its relevance score to a beat."""

    index: int
    score: float
    scene_text: str
    metadata: dict[str, object] = field(default_factory=dict)


def find_best_scenes(
    beat: str,
    scenes: Sequence[str],
    *,
    top_k: int = 5,
    min_score: float = 0.1,
) -> list[ScoredScene]:
    """Rank scenes by relevance to a story beat, returning the top-k matches."""
    results: list[ScoredScene] = []
    for i, scene in enumerate(scenes):
        score = match_scene_to_beat(scene, beat)
        if score >= min_score:
            results.append(ScoredScene(index=i, score=score, scene_text=scene))
    results.sort(key=lambda s: s.score, reverse=True)
    return results[:top_k]


# ── Character Name Matching ───────────────────────────────────────────────────────


def _normalise_name(name: str) -> str:
    """Normalise a character name for fuzzy comparison — strip honorifics, punctuation."""
    name = name.strip().lower()
    name = re.sub(r"[^\w\s]", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    name = re.sub(r"^(mr|mrs|ms|dr|prof|sir|lady|lord)\.?\s+", "", name)
    return name


def _split_name_parts(name: str) -> list[str]:
    return _normalise_name(name).split()


def _name_parts_similarity(a_parts: list[str], b_parts: list[str]) -> float:
    """Weighted similarity between two name part lists (family, given, middle, full)."""
    if not a_parts or not b_parts:
        return 0.0
    # Family name (last part) exact match
    family_score = 0.4 if a_parts[-1] == b_parts[-1] else 0.0
    # Given name (first part) fuzzy match
    given_score = difflib.SequenceMatcher(None, a_parts[0], b_parts[0]).ratio() * 0.4
    # Middle parts overlap
    middle_a = set(a_parts[1:-1]) if len(a_parts) > 2 else set()
    middle_b = set(b_parts[1:-1]) if len(b_parts) > 2 else set()
    middle_score = 0.0
    if middle_a or middle_b:
        union = middle_a | middle_b
        if union:
            middle_score = (len(middle_a & middle_b) / len(union)) * 0.2
    # Full-name sequence fallback
    full_score = difflib.SequenceMatcher(
        None, " ".join(a_parts), " ".join(b_parts)
    ).ratio() * 0.2
    return round(family_score + given_score + middle_score + full_score, 4)


@dataclass
class NameMatch:
    """A candidate name match with confidence score."""

    candidate: str
    score: float
    is_exact: bool = False


def match_character_name(
    name: str,
    candidates: Sequence[str],
    *,
    threshold: float = 0.5,
    max_results: int = 3,
) -> list[NameMatch]:
    """Fuzzy-match a character name against a list of candidate names.

    Handles name order variation, partial names, honorific stripping, and
    single-name vs multi-name queries. Uses multi-part weighted edit distance.
    """
    query_parts = _split_name_parts(name)
    query_normalised = " ".join(query_parts)

    results: list[NameMatch] = []
    for candidate in candidates:
        candidate_parts = _split_name_parts(candidate)
        candidate_normalised = " ".join(candidate_parts)

        if query_normalised == candidate_normalised:
            results.append(NameMatch(candidate=candidate, score=1.0, is_exact=True))
            continue

        if len(query_parts) == 1:
            query_token = query_parts[0]
            if query_token in candidate_parts:
                results.append(NameMatch(candidate=candidate, score=0.75, is_exact=False))
                continue
            best = max(
                difflib.SequenceMatcher(None, query_token, cp).ratio()
                for cp in candidate_parts
            )
            if best >= 0.7:
                results.append(NameMatch(candidate=candidate, score=best * 0.7, is_exact=False))
                continue

        score = _name_parts_similarity(query_parts, candidate_parts)
        if score >= threshold:
            results.append(NameMatch(candidate=candidate, score=score, is_exact=False))

    results.sort(key=lambda m: m.score, reverse=True)
    return results[:max_results]


# ── Timeline Alignment ────────────────────────────────────────────────────────────


@dataclass
class TimelinePoint:
    """A single point on a timeline with a timestamp and label."""

    timestamp_seconds: float
    label: str
    source: str = ""


@dataclass
class TimelineAlignment:
    """Result of aligning two timeline points."""

    script_index: int
    api_index: int
    script_label: str
    api_label: str
    confidence: float
    time_delta_seconds: float


def align_timeline(
    script_timeline: Sequence[TimelinePoint],
    api_timeline: Sequence[TimelinePoint],
    *,
    max_time_delta: float = 30.0,
    min_confidence: float = 0.3,
) -> list[TimelineAlignment]:
    """Align two timelines by matching points via label similarity and time proximity.

    Produces 1:1 alignments between script_timeline and api_timeline points.
    Confidence combines label similarity (0.6) and time proximity (0.4).
    """
    if not script_timeline or not api_timeline:
        return []

    alignments: list[TimelineAlignment] = []
    for si, sp in enumerate(script_timeline):
        best_score = 0.0
        best_ai = -1
        best_delta = 0.0
        for ai, ap in enumerate(api_timeline):
            time_delta = abs(sp.timestamp_seconds - ap.timestamp_seconds)
            if time_delta > max_time_delta:
                continue
            label_score = difflib.SequenceMatcher(
                None, _normalise(sp.label), _normalise(ap.label)
            ).ratio()
            time_score = 1.0 - (time_delta / max_time_delta)
            combined = 0.6 * label_score + 0.4 * time_score
            if combined > best_score:
                best_score = combined
                best_ai = ai
                best_delta = time_delta

        if best_ai >= 0 and best_score >= min_confidence:
            alignments.append(
                TimelineAlignment(
                    script_index=si,
                    api_index=best_ai,
                    script_label=sp.label,
                    api_label=api_timeline[best_ai].label,
                    confidence=round(best_score, 4),
                    time_delta_seconds=round(best_delta, 2),
                )
            )

    # Deduplicate: each API point aligns to at most one script point
    used_api: set[int] = set()
    deduped: list[TimelineAlignment] = []
    for a in sorted(alignments, key=lambda x: x.confidence, reverse=True):
        if a.api_index not in used_api:
            deduped.append(a)
            used_api.add(a.api_index)

    deduped.sort(key=lambda x: x.script_index)
    return deduped