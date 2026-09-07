"""EventDuplicateHypothesis/v1 — repeated-event suggestion (R2A).

Similar events across windows are suggested for merging. Original events are
never deleted or rewritten; a hypothesis is an evaluation artifact only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

DUPLICATE_SCHEMA = "EventDuplicateHypothesis/v1"
DUPLICATE_THRESHOLD = 0.75

_TOKEN_RE = re.compile(r"[\w一-鿿]+")


@dataclass(frozen=True)
class EventObservation:
    ref: str
    episode: int
    window: int
    summary: str
    participants: tuple[str, ...]
    source_refs: tuple[str, ...]

    def to_mapping(self) -> dict[str, Any]:
        return {
            "ref": self.ref, "episode": self.episode, "window": self.window,
            "summary": self.summary, "participants": list(self.participants),
            "source_refs": list(self.source_refs),
        }

    @classmethod
    def from_mapping(cls, obj: Any) -> EventObservation:
        if not isinstance(obj, dict):
            raise ValueError("EventObservation must be an object")
        allowed = {"ref", "episode", "window", "summary", "participants", "source_refs"}
        unknown = sorted(set(obj) - allowed)
        if unknown:
            raise ValueError(f"EventObservation: unknown keys {unknown}")
        return cls(
            ref=obj["ref"], episode=int(obj["episode"]), window=int(obj["window"]),
            summary=str(obj.get("summary", "")),
            participants=tuple(obj.get("participants", ())),
            source_refs=tuple(obj.get("source_refs", ())),
        )


@dataclass(frozen=True)
class EventDuplicateHypothesis:
    left_ref: str
    right_ref: str
    method: str
    score: float
    decision: str

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": DUPLICATE_SCHEMA,
            "left_ref": self.left_ref,
            "right_ref": self.right_ref,
            "method": self.method,
            "score": self.score,
            "decision": self.decision,
        }


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(text) if t.strip()}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def match_event_pair(a: EventObservation, b: EventObservation) -> EventDuplicateHypothesis:
    """Summary-token Jaccard + shared participants; the same (episode, window)
    source span shared by both is strong duplicate evidence."""
    text_sim = _jaccard(_tokens(a.summary), _tokens(b.summary))
    participants_a = {p.strip().lower() for p in a.participants if p.strip()}
    participants_b = {p.strip().lower() for p in b.participants if p.strip()}
    if participants_a and participants_b:
        participant_sim = len(participants_a & participants_b) / len(participants_a | participants_b)
    else:
        participant_sim = 0.0
    score = 0.7 * text_sim + 0.3 * participant_sim
    if score >= DUPLICATE_THRESHOLD:
        decision = "merge_candidate"
    elif score >= 0.40:
        decision = "inconclusive"
    else:
        decision = "keep_separate"
    return EventDuplicateHypothesis(
        left_ref=a.ref, right_ref=b.ref, method="lexical_token_jaccard_v1",
        score=round(score, 6), decision=decision,
    )


def find_duplicates(
    events: list[EventObservation], *, max_episode: int | None = None
) -> list[EventDuplicateHypothesis]:
    """Pairwise duplicate hypotheses over the given census prefix only."""
    visible = [e for e in events if max_episode is None or e.episode <= max_episode]
    out: list[EventDuplicateHypothesis] = []
    for i in range(len(visible)):
        for j in range(i + 1, len(visible)):
            a, b = visible[i], visible[j]
            if a.ref == b.ref:
                continue
            out.append(match_event_pair(a, b))
    return out
