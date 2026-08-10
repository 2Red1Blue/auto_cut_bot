"""Field-level provenance tracking and deterministic merge operator (Doc 22).

Classifies every field into one of four categories and applies deterministic
merge policies without requiring LLM calls for the merge step itself.

Components:
  1. Field Classification Matrix — categorises every field path
  2. ProvenanceRecord — immutable audit trail for each field-level decision
  3. Merge Operator — deterministic, zero-LLM reconciliation of two sources
  4. Ontology Mapper — aligns API concepts to script concepts before comparison
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# ═══════════════════════════════════════════════════════════════════════════════
# 1. Field Classification Matrix
# ═══════════════════════════════════════════════════════════════════════════════

FIELD_CATEGORIES: dict[str, dict[str, str]] = {
    "objective_measurable": {
        "description": "Objectively measurable values (episode count, duration, timestamps)",
        "primary_source": "api",
        "conflict_resolution": "api_wins",
    },
    "author_intent": {
        "description": "Author-intent semantics (persona, relationships, themes)",
        "primary_source": "llm_pass1",
        "conflict_resolution": "conflict_queue",
    },
    "api_enrichment": {
        "description": "API-only data (voice_timbre, visual tags, platform keywords)",
        "primary_source": "api",
        "conflict_resolution": "api_direct",
    },
    "video_verifiable": {
        "description": "Video-verifiable claims (character presence, scene location, action)",
        "primary_source": "llm_pass1",
        "conflict_resolution": "vlm_arbitration",
    },
}

FIELD_CLASSIFICATION: dict[str, str] = {
    "episodes[].episode_number": "objective_measurable",
    "episodes[].duration": "objective_measurable",
    "episodes[].summary": "author_intent",
    "scenes[].location": "video_verifiable",
    "scenes[].time_of_day": "video_verifiable",
    "scenes[].characters_present": "video_verifiable",
    "scenes[].heading": "author_intent",
    "scenes[].distilled_summary": "author_intent",
    "scenes[].start_time": "objective_measurable",
    "scenes[].end_time": "objective_measurable",
    "characters[].persona": "author_intent",
    "characters[].personality": "author_intent",
    "characters[].traits": "author_intent",
    "characters[].voice_timbre": "api_enrichment",
    "characters[].visual_features": "api_enrichment",
    "characters[].relationship": "author_intent",
    "shots[].subjects": "video_verifiable",
    "shots[].actions": "video_verifiable",
    "shots[].highlight_score": "api_enrichment",
    "book.genre": "author_intent",
    "book.mood": "author_intent",
    "book.tags": "api_enrichment",
    "book.total_episodes": "objective_measurable",
    "subtitles[].speaker": "author_intent",
    "subtitles[].tone": "author_intent",
    "relationships[].description": "author_intent",
    "boundaries[].subjects": "video_verifiable",
}


def classify_field_path(field_path: str) -> str:
    """Return the category for a field path. Falls back to author_intent."""
    return FIELD_CLASSIFICATION.get(field_path, "author_intent")


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Provenance Record
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class ProvenanceRecord:
    """Immutable audit trail for a single field-level merge decision.

    Fields:
        entity_id: Which entity, e.g. "characters[3]".
        field_path: Field name within the entity, e.g. "persona".
        values: Source label -> value, e.g. {"llm_pass1": {...}, "api": {...}}.
        canonical_source: Which source's value was selected.
        resolved_at: ISO 8601 timestamp.
        resolved_by: "auto_policy", "human", or "vlm".
    """

    entity_id: str
    field_path: str
    values: dict[str, Any] = field(default_factory=dict)
    canonical_source: str = ""
    resolved_at: str = ""
    resolved_by: str = "auto_policy"

    @property
    def canonical_value(self) -> Any:
        return self.values.get(self.canonical_source)

    @property
    def has_conflict(self) -> bool:
        non_empty = {k: v for k, v in self.values.items() if v is not None and v != "" and v != []}
        return len({str(v) for v in non_empty.values()}) > 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "field_path": self.field_path,
            "values": dict(self.values),
            "canonical_source": self.canonical_source,
            "resolved_at": self.resolved_at,
            "resolved_by": self.resolved_by,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProvenanceRecord:
        return cls(
            entity_id=data["entity_id"],
            field_path=data["field_path"],
            values=dict(data.get("values", {})),
            canonical_source=data.get("canonical_source", ""),
            resolved_at=data.get("resolved_at", ""),
            resolved_by=data.get("resolved_by", "auto_policy"),
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Ontology Mapper
# ═══════════════════════════════════════════════════════════════════════════════

ONTOLOGY_MAP: dict[str, str] = {
    "api.scene": "script.scene",              # 1:N, aggregate
    "api.shot": "script.scene",               # aggregate shots into scenes
    "api.episode": "script.episode",          # 1:1, direct compare
    "api.character.traits": "script.character.persona",   # same domain
    "api.character.emotion": "script.character.tone",
    "api.scene.summary": "script.scene.distilled_summary",
    "api.episode.synopsis": "script.episode.summary",
}


def map_api_to_script(api_concept: str) -> str:
    """Map an API concept path to its corresponding script concept path."""
    return ONTOLOGY_MAP.get(api_concept, api_concept)


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Merge Operator (deterministic, zero LLM)
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class MergeResult:
    """Complete result of a deterministic merge operation.

    canonical: merged record with resolved values.
    provenance: audit trail for every field-level decision.
    conflicts: structured conflict queue entries for HITL or VLM review.
    """

    canonical: dict[str, Any] = field(default_factory=dict)
    provenance: list[ProvenanceRecord] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def has_conflicts(self) -> bool:
        return len(self.conflicts) > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonical": self.canonical,
            "provenance": [p.to_dict() for p in self.provenance],
            "conflicts": list(self.conflicts),
        }


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    if isinstance(value, (list, dict)) and len(value) == 0:
        return True
    return False


def _values_equal(a: Any, b: Any) -> bool:
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_values_equal(ai, bi) for ai, bi in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_values_equal(a[k], b[k]) for k in a)
    return a == b


def _normalize_field_path(field_path: str) -> str:
    """Convert 'episodes[0].duration' to 'episodes[].duration' for classification."""
    import re
    return re.sub(r"\[\d+\]", "[]", field_path)


def _resolve_conflict(
    category: str,
    llm_value: Any,
    api_value: Any,
    field_path: str,
    entity_id: str,
    resolved_at: str,
) -> tuple[Any, str, str, dict[str, Any] | None]:
    """Apply resolution policy. Returns (canonical_value, source, resolved_by, conflict_entry)."""
    resolution = FIELD_CATEGORIES[category]["conflict_resolution"]

    if resolution in ("api_wins", "api_direct"):
        return (api_value if not _is_empty(api_value) else llm_value, "api", "auto_policy", None)

    if resolution == "conflict_queue":
        conflict = {
            "entity_id": entity_id, "field_path": field_path, "category": category,
            "llm_value": llm_value, "api_value": api_value,
            "resolution": "pending_hitl", "resolved_at": resolved_at,
        }
        return (llm_value, "llm_pass1", "auto_policy", conflict)

    if resolution == "vlm_arbitration":
        conflict = {
            "entity_id": entity_id, "field_path": field_path, "category": category,
            "llm_value": llm_value, "api_value": api_value,
            "resolution": "pending_vlm", "resolved_at": resolved_at,
        }
        return (llm_value, "llm_pass1", "auto_policy", conflict)

    conflict = {
        "entity_id": entity_id, "field_path": field_path, "category": category,
        "llm_value": llm_value, "api_value": api_value,
        "resolution": "pending_hitl", "resolved_at": resolved_at,
    }
    return (llm_value, "llm_pass1", "auto_policy", conflict)


def merge_operator(
    llm_data: dict[str, Any],
    api_data: dict[str, Any],
    field_matrix: dict[str, dict[str, str]] | None = None,
    *,
    entity_id: str = "",
    resolved_at: str = "",
) -> MergeResult:
    """Deterministic merge of two data sources.

    1. Ontology mapping: align API concepts to script concepts
    2. Field-by-field comparison (only same-concept, same-granularity pairs)
    3. Apply policy matrix per field category
    4. Return canonical values + provenance records + conflict queue entries
    """
    if field_matrix is None:
        field_matrix = FIELD_CATEGORIES
    if not resolved_at:
        resolved_at = datetime.now(timezone.utc).isoformat()

    canonical: dict[str, Any] = {}
    provenance: list[ProvenanceRecord] = []
    conflicts: list[dict[str, Any]] = []

    for key in sorted(set(llm_data.keys()) | set(api_data.keys())):
        llm_value = llm_data.get(key)
        api_value = api_data.get(key)
        field_path = f"{entity_id}.{key}" if entity_id else key

        if _is_empty(llm_value) and _is_empty(api_value):
            continue

        if _is_empty(llm_value) and not _is_empty(api_value):
            canonical[key] = api_value
            provenance.append(ProvenanceRecord(
                entity_id=entity_id, field_path=key,
                values={"llm_pass1": llm_value, "api": api_value},
                canonical_source="api", resolved_at=resolved_at,
            ))
            continue

        if not _is_empty(llm_value) and _is_empty(api_value):
            canonical[key] = llm_value
            provenance.append(ProvenanceRecord(
                entity_id=entity_id, field_path=key,
                values={"llm_pass1": llm_value, "api": api_value},
                canonical_source="llm_pass1", resolved_at=resolved_at,
            ))
            continue

        if _values_equal(llm_value, api_value):
            canonical[key] = llm_value
            provenance.append(ProvenanceRecord(
                entity_id=entity_id, field_path=key,
                values={"llm_pass1": llm_value, "api": api_value},
                canonical_source="both", resolved_at=resolved_at,
            ))
            continue

        norm_path = _normalize_field_path(field_path)
        category = classify_field_path(norm_path)
        cv, csource, cby, conflict_entry = _resolve_conflict(
            category, llm_value, api_value, key, entity_id, resolved_at,
        )
        canonical[key] = cv
        provenance.append(ProvenanceRecord(
            entity_id=entity_id, field_path=key,
            values={"llm_pass1": llm_value, "api": api_value},
            canonical_source=csource, resolved_at=resolved_at, resolved_by=cby,
        ))
        if conflict_entry is not None:
            conflicts.append(conflict_entry)

    return MergeResult(canonical=canonical, provenance=provenance, conflicts=conflicts)


__all__ = [
    "FIELD_CATEGORIES",
    "FIELD_CLASSIFICATION",
    "ONTOLOGY_MAP",
    "ProvenanceRecord",
    "MergeResult",
    "classify_field_path",
    "map_api_to_script",
    "merge_operator",
]