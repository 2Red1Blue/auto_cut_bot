"""Event/Candidate -> retrieval index document projection (R2A).

Documents are derived views of already-frozen semantic objects. The projection
version is part of index identity: changing the text projection invalidates
the manifest instead of silently mixing documents.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tools.reuse.models import json_sha256
from tools.reuse.window_memory.event_deduper import EventObservation

TEXT_PROJECTION_VERSION = "retrieval-text-projection-v1"


@dataclass(frozen=True)
class IndexDocument:
    object_ref: str
    content_hash: str  # hash of the SOURCE object payload this document projects
    text: str
    kind: str  # "event" | "candidate"
    episode: int | None
    source_refs: tuple[str, ...]

    def to_mapping(self) -> dict[str, Any]:
        return {
            "object_ref": self.object_ref,
            "content_hash": self.content_hash,
            "text": self.text,
            "kind": self.kind,
            "episode": self.episode,
            "source_refs": list(self.source_refs),
        }


def project_event(event: EventObservation) -> IndexDocument:
    """Event -> document: summary + participants; no new facts introduced."""
    text = " ".join([event.summary, *event.participants]).strip()
    content_hash = json_sha256(event.to_mapping())
    return IndexDocument(
        object_ref=event.ref, content_hash=content_hash, text=text,
        kind="event", episode=event.episode, source_refs=event.source_refs,
    )


def project_candidate(
    ref: str, payload: dict[str, Any]
) -> IndexDocument:
    """Candidate highlight -> document: reason/excerpt/tags fields only.

    Unknown payload fields are ignored by the projection (they still bind into
    content_hash through the whole payload), so the index can never invent
    information that is not in the frozen source object."""
    parts = [
        str(payload.get("reason", "")),
        str(payload.get("dialogue_excerpt", "") or ""),
        " ".join(str(t) for t in (payload.get("tags") or [])),
        str(payload.get("anchor_summary", "") or ""),
    ]
    text = " ".join(p for p in parts if p).strip()
    return IndexDocument(
        object_ref=ref, content_hash=json_sha256(payload), text=text,
        kind="candidate",
        episode=int(payload["episode"]) if payload.get("episode") is not None else None,
        source_refs=tuple(str(s) for s in (payload.get("source_refs") or ())),
    )
