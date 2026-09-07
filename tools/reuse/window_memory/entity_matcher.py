"""EntityMatchHypothesis/v1 — cross-window entity association (R2A).

MMLVE-inspired entity/keyframe grounding, reduced to post-processing over
already-saved observations: same-person splits, different-person merges and
repeated events are scored as HYPOTHESES. A hypothesis is never a Character
identity; low scores or conflicting evidence keep entities separate.

Deterministic by construction: identical input produces identical hypotheses.
Future information is impossible by design — callers pass only prefix-window
observations; nothing here reads any store or latest head.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

HYPOTHESIS_SCHEMA = "EntityMatchHypothesis/v1"

METHODS = ("visual_feature_overlap", "name_alias_match", "combined")
DECISIONS = ("merge_candidate", "keep_separate", "inconclusive")

# scoring knobs (frozen; changing them is a new policy version)
MERGE_THRESHOLD = 0.70
INCONCLUSIVE_BAND = (0.40, 0.70)
_NAME_MIN_SIMILARITY = 0.60


@dataclass(frozen=True)
class EntityObservation:
    """One observed entity instance from a saved window output.

    source_refs pin the observation to exact upstream objects; window_ref is
    the (episode, window) census position, not a filename."""
    ref: str
    episode: int
    window: int
    display_names: tuple[str, ...]
    visual_features: tuple[str, ...]
    source_refs: tuple[str, ...]

    def to_mapping(self) -> dict[str, Any]:
        return {
            "ref": self.ref, "episode": self.episode, "window": self.window,
            "display_names": list(self.display_names),
            "visual_features": list(self.visual_features),
            "source_refs": list(self.source_refs),
        }

    @classmethod
    def from_mapping(cls, obj: Any) -> EntityObservation:
        if not isinstance(obj, dict):
            raise ValueError("EntityObservation must be an object")
        allowed = {"ref", "episode", "window", "display_names", "visual_features", "source_refs"}
        unknown = sorted(set(obj) - allowed)
        if unknown:
            raise ValueError(f"EntityObservation: unknown keys {unknown}")
        return cls(
            ref=obj["ref"], episode=int(obj["episode"]), window=int(obj["window"]),
            display_names=tuple(obj.get("display_names", ())),
            visual_features=tuple(obj.get("visual_features", ())),
            source_refs=tuple(obj.get("source_refs", ())),
        )


@dataclass(frozen=True)
class EntityMatchHypothesis:
    left_ref: str
    right_ref: str
    feature_refs: tuple[str, ...]
    method: str
    score: float
    decision: str

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": HYPOTHESIS_SCHEMA,
            "left_ref": self.left_ref,
            "right_ref": self.right_ref,
            "feature_refs": list(self.feature_refs),
            "method": self.method,
            "score": self.score,
            "decision": self.decision,
        }


def _norm_name(name: str) -> str:
    return re.sub(r"\s+", "", name).lower()


def name_similarity(a: str, b: str) -> float:
    """Alias equality or containment; different names score 0 (never fuzzy)."""
    na, nb = _norm_name(a), _norm_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    shorter, longer = (na, nb) if len(na) <= len(nb) else (nb, na)
    if len(shorter) >= 2 and shorter in longer:
        return _NAME_MIN_SIMILARITY + 0.2 * (len(shorter) / len(longer))
    return 0.0


def visual_overlap(a: EntityObservation, b: EntityObservation) -> tuple[float, tuple[str, ...]]:
    """Jaccard over normalized visual feature tokens + the shared feature refs."""
    fa = {f.strip().lower() for f in a.visual_features if f.strip()}
    fb = {f.strip().lower() for f in b.visual_features if f.strip()}
    if not fa or not fb:
        return 0.0, ()
    shared = fa & fb
    if not shared:
        return 0.0, ()
    return len(shared) / len(fa | fb), tuple(sorted(shared))


def _decide(score: float) -> str:
    if score >= MERGE_THRESHOLD:
        return "merge_candidate"
    if INCONCLUSIVE_BAND[0] <= score < INCONCLUSIVE_BAND[1]:
        return "inconclusive"
    return "keep_separate"


def match_entity_pair(a: EntityObservation, b: EntityObservation) -> EntityMatchHypothesis:
    """Score one pair. Same display name is NOT identity evidence alone —
    same-name different-person is the primary error this matcher must not commit."""
    name_score = 0.0
    shared_name = ""
    for na in a.display_names:
        for nb in b.display_names:
            sim = name_similarity(na, nb)
            if sim > name_score:
                name_score, shared_name = sim, na if len(na) <= len(nb) else nb
    visual, shared_features = visual_overlap(a, b)
    if name_score > 0 and visual > 0:
        score = 0.5 * name_score + 0.5 * visual
        method = "combined"
        feature_refs = tuple(f"feature:{shared_name}:{f}" for f in shared_features)
    elif visual > 0:
        score = visual
        method = "visual_feature_overlap"
        feature_refs = tuple(f"feature:::{f}" for f in shared_features)
    else:
        score = name_score
        method = "name_alias_match"
        feature_refs = (f"feature:{shared_name}::",)
    # Name-only evidence is never identity: identical/alias names yield an
    # inconclusive hypothesis at most, never a merge candidate.
    if method == "name_alias_match" and score >= MERGE_THRESHOLD:
        return EntityMatchHypothesis(
            left_ref=a.ref, right_ref=b.ref,
            feature_refs=feature_refs, method=method,
            score=round(score, 6), decision="inconclusive",
        )
    return EntityMatchHypothesis(
        left_ref=a.ref, right_ref=b.ref,
        feature_refs=feature_refs, method=method,
        score=round(score, 6), decision=_decide(score),
    )


def match_entities(
    observations: list[EntityObservation], *, max_episode: int | None = None
) -> list[EntityMatchHypothesis]:
    """Pairwise hypotheses over the given prefix only.

    `max_episode` bounds the census prefix (inclusive); observations from later
    episodes are invisible — the anti-future-information guarantee."""
    visible = [o for o in observations if max_episode is None or o.episode <= max_episode]
    hypotheses: list[EntityMatchHypothesis] = []
    for i in range(len(visible)):
        for j in range(i + 1, len(visible)):
            a, b = visible[i], visible[j]
            if a.ref == b.ref:
                continue
            hypotheses.append(match_entity_pair(a, b))
    return hypotheses
