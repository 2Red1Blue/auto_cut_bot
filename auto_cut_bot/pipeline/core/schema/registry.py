"""角色注册表 + Assignment + Bible Schema — Pydantic v2。

原 story_schemas.py 涵盖的全部 Series Registry 相关 schema。
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ── 枚举 ───────────────────────────────────────────────────────────

class IdentityDecision(str, Enum):
    merge_with_existing_character = "merge_with_existing_character"
    quarantine_unresolved_identity = "quarantine_unresolved_identity"
    keep_blocking = "keep_blocking"


class RelationshipDecision(str, Enum):
    relationship_found = "relationship_found"
    no_supported_relationship = "no_supported_relationship"


class BeatPhase(str, Enum):
    setup = "setup"
    escalation = "escalation"
    turn = "turn"
    reveal = "reveal"
    payoff = "payoff"
    consequence = "consequence"
    coda = "coda"


class BeatImportance(str, Enum):
    required = "required"
    supporting = "supporting"
    optional = "optional"


class ExcludeReason(str, Enum):
    non_narrative = "non_narrative"
    recap_only = "recap_only"
    credits_or_placeholder = "credits_or_placeholder"
    corrupted_or_unavailable = "corrupted_or_unavailable"
    insufficient_evidence = "insufficient_evidence"
    registry_quarantined_dependency = "registry_quarantined_dependency"


# ── ID 正则模式 (复用自 ids.py) ────────────────────────────────────

_ID_EVENT = r"^event-[0-9a-f]{12}$"
_ID_CHAR = r"^char-[a-z0-9-]{2,40}$"
_ID_REL = r"^rel-[a-z0-9-]{2,40}$"
_ID_THREAD = r"^thread-[a-z0-9-]{2,40}$"
_ID_FACT = r"^fact-[a-z0-9-]{2,40}$"
_ID_OQ = r"^q-[a-z0-9-]{2,40}$"


# ── Registry 子模型 ────────────────────────────────────────────────

class IdentityEvidence(BaseModel):
    episode: int = Field(ge=1)
    quote: str = Field(min_length=10)


class RegistryCharacter(BaseModel):
    id: str = Field(pattern=_ID_CHAR)
    canonical_name: str = Field(min_length=1)
    aliases: list[str] = []
    entity_type: str  # individual | group | creature | unknown
    identity: str = ""
    identity_evidence: IdentityEvidence | None = None
    goals: list[str] = []
    first_event_id: str = Field(pattern=_ID_EVENT)
    evidence_event_ids: list[str] = Field(min_length=1)


class StateChange(BaseModel):
    event_id: str = ""
    state: str = Field(min_length=1)
    reason: str = ""


class RegistryRelationship(BaseModel):
    id: str = Field(pattern=_ID_REL)
    character_ids: list[str] = Field(min_length=1)
    initial_state: str = ""
    state_changes: list[StateChange] = []


class RegistryFact(BaseModel):
    id: str = Field(pattern=_ID_FACT)
    statement: str = Field(min_length=1)
    event_ids: list[str] = Field(min_length=1)


class RegistryThread(BaseModel):
    id: str = Field(pattern=_ID_THREAD)
    title: str = Field(min_length=1)
    premise: str = Field(min_length=1)
    thread_kind: str  # arc | coda
    character_ids: list[str] = Field(min_length=1)
    anchor_event_ids: list[str] = Field(min_length=1)
    open_question_ids: list[str] = []
    status: str  # resolved | partially_resolved | open


class RegistryQuestion(BaseModel):
    id: str = Field(pattern=_ID_OQ)
    question: str = Field(min_length=1)
    event_ids: list[str] = Field(min_length=1)
    status: str  # open | resolved


class UnresolvedIdentityConflict(BaseModel):
    description: str = Field(min_length=1)
    candidate_names: list[str] = Field(min_length=1)
    event_ids: list[str] = Field(min_length=1)


class SeriesRegistry(BaseModel):
    schema_version: str = "1.3"
    language: str  # zh | en
    series_summary: str = Field(min_length=1)
    characters: list[RegistryCharacter] = []
    relationships: list[RegistryRelationship] = []
    facts: list[RegistryFact] = []
    story_threads: list[RegistryThread] = Field(min_length=1)
    open_questions: list[RegistryQuestion] = []
    unresolved_identity_conflicts: list[UnresolvedIdentityConflict] = []

    model_config = {"extra": "forbid"}


# ── Identity Audit ─────────────────────────────────────────────────

class IdentityAuditResult(BaseModel):
    schema_version: str = "1.0"
    subject_character_id: str = Field(min_length=1)
    decision: IdentityDecision
    target_character_id: str = ""
    evidence_event_ids: list[str] = []
    reason: str = Field(min_length=1)


# ── Relationship Repair ────────────────────────────────────────────

class RelationshipRepairStateChange(BaseModel):
    event_id: str = ""
    state: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class RelationshipRepairResult(BaseModel):
    schema_version: str = "1.0"
    subject_character_id: str = Field(min_length=1)
    decision: RelationshipDecision
    partner_character_id: str = ""
    initial_state: str = ""
    state_changes: list[RelationshipRepairStateChange] = []


# ── Assignment ─────────────────────────────────────────────────────

class ThreadBeat(BaseModel):
    id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    episode: int = Field(ge=1)
    phase: BeatPhase
    importance: BeatImportance
    summary: str = Field(min_length=1)
    event_ids: list[str] = Field(min_length=1)
    requires_beat_ids: list[str] = []


class ExcludedEpisode(BaseModel):
    episode: int = Field(ge=1)
    reason_type: ExcludeReason
    explanation: str = Field(min_length=1)
    event_ids: list[str] = []


class SeriesAssignment(BaseModel):
    schema_version: str = "1.0"
    chapter_id: str = Field(min_length=1)
    episodes: list[int] = Field(min_length=1)
    thread_beats: list[ThreadBeat] = []
    excluded_episodes: list[ExcludedEpisode] = []

    model_config = {"extra": "forbid"}


# ── Bible ──────────────────────────────────────────────────────────

class BibleEntityImportance(BaseModel):
    score: float = Field(ge=0, le=1)
    reason: str = ""


class BibleMetadata(BaseModel):
    episode_count: int = Field(ge=1)
    total_events: int = Field(ge=1)


class BibleThreadSummary(BaseModel):
    thread_key: str = Field(min_length=1)
    title: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    status: str
    event_ids: list[str] = Field(min_length=1)


class BibleCharacterSummary(BaseModel):
    character_key: str = Field(min_length=1)
    name: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    importance: BibleEntityImportance


class SeriesBible(BaseModel):
    metadata: BibleMetadata
    characters: list[BibleCharacterSummary] = []
    relationships: list[dict] = []
    story_threads: list[BibleThreadSummary] = []

    model_config = {"extra": "forbid"}


# ── 旧兼容 dict schema ────────────────────────────────────────────

def _event_pat(): return {"type": "string", "pattern": _ID_EVENT}
def _char_pat(): return {"type": "string", "pattern": _ID_CHAR}
def _rel_pat(): return {"type": "string", "pattern": _ID_REL}
def _thread_pat(): return {"type": "string", "pattern": _ID_THREAD}
def _fact_pat(): return {"type": "string", "pattern": _ID_FACT}
def _oq_pat(): return {"type": "string", "pattern": _ID_OQ}
def _arr(items, **kw): return {"type": "array", "items": items, **kw}
def _obj(props, required=None, additional=False):
    return {"type": "object", "properties": props,
            "required": required if required is not None else list(props),
            "additionalProperties": additional}

_S = {"type": "string"}
_NE = {"type": "string", "minLength": 1}
_N = {"type": "number"}
_B = {"type": "boolean"}


def registry_dict_schemas() -> dict[str, dict]:
    """返回所有 registry 相关旧式 dict schema。"""
    identity_evidence = _obj({
        "episode": {"type": "integer", "minimum": 1},
        "quote": {"type": "string", "minLength": 10},
    })
    return {
        "SERIES_REGISTRY_SCHEMA": _obj({
            "schema_version": {"type": "string", "const": "1.3"},
            "language": {"type": "string", "enum": ["zh", "en"]},
            "series_summary": _NE,
            "characters": _arr(_obj({
                "id": _char_pat(), "canonical_name": _NE, "aliases": _arr(_S, minItems=1) if False else _arr(_S),
                "entity_type": {"type": "string", "enum": ["individual", "group", "creature", "unknown"]},
                "identity": _S, "identity_evidence": identity_evidence,
                "goals": _arr(_S), "first_event_id": _event_pat(),
                "evidence_event_ids": _arr(_event_pat(), minItems=1),
            })),
            "relationships": _arr(_obj({
                "id": _rel_pat(), "character_ids": _arr(_char_pat(), minItems=1),
                "initial_state": _S,
                "state_changes": _arr(_obj({
                    "event_id": _event_pat(), "state": _NE, "reason": _S,
                })),
            })),
            "facts": _arr(_obj({
                "id": _fact_pat(), "statement": _NE,
                "event_ids": _arr(_event_pat(), minItems=1),
            })),
            "story_threads": _arr(_obj({
                "id": _thread_pat(), "title": _NE, "premise": _NE,
                "thread_kind": {"type": "string", "enum": ["arc", "coda"]},
                "character_ids": _arr(_char_pat(), minItems=1),
                "anchor_event_ids": _arr(_event_pat(), minItems=1),
                "open_question_ids": _arr(_oq_pat()),
                "status": {"type": "string", "enum": ["resolved", "partially_resolved", "open"]},
            }), minItems=1),
            "open_questions": _arr(_obj({
                "id": _oq_pat(), "question": _NE,
                "event_ids": _arr(_event_pat(), minItems=1),
                "status": {"type": "string", "enum": ["open", "resolved"]},
            })),
            "unresolved_identity_conflicts": _arr(_obj({
                "description": _NE, "candidate_names": _arr(_S, minItems=1),
                "event_ids": _arr(_event_pat(), minItems=1),
            })),
        }),
        "SERIES_REGISTRY_THREAD_SCHEMA": _obj({
            "id": _thread_pat(), "title": _NE, "premise": _NE,
            "thread_kind": {"type": "string", "enum": ["arc", "coda"]},
            "character_ids": _arr(_char_pat(), minItems=1),
            "anchor_event_ids": _arr(_event_pat(), minItems=1),
            "open_question_ids": _arr(_oq_pat()),
            "status": {"type": "string", "enum": ["resolved", "partially_resolved", "open"]},
        }),
        "SERIES_REGISTRY_IDENTITY_AUDIT_SCHEMA": _obj({
            "schema_version": {"type": "string", "const": "1.0"},
            "subject_character_id": _NE,
            "decision": {"type": "string", "enum": ["merge_with_existing_character", "quarantine_unresolved_identity", "keep_blocking"]},
            "target_character_id": _S,
            "evidence_event_ids": _arr(_event_pat()),
            "reason": _NE,
        }),
        "SERIES_REGISTRY_RELATIONSHIP_REPAIR_SCHEMA": _obj({
            "schema_version": {"type": "string", "const": "1.0"},
            "subject_character_id": _NE,
            "decision": {"type": "string", "enum": ["relationship_found", "no_supported_relationship"]},
            "partner_character_id": _S,
            "initial_state": _S,
            "state_changes": _arr(_obj({
                "event_id": _S, "state": _NE, "reason": _NE,
            })),
        }),
        "THREAD_BEAT_SCHEMA": _obj({
            "id": _NE, "thread_id": _NE,
            "episode": {"type": "integer", "minimum": 1},
            "phase": {"type": "string", "enum": ["setup", "escalation", "turn", "reveal", "payoff", "consequence", "coda"]},
            "importance": {"type": "string", "enum": ["required", "supporting", "optional"]},
            "summary": _NE, "event_ids": _arr(_S, minItems=1),
            "requires_beat_ids": _arr(_S),
        }),
        "EXCLUDED_EPISODE_SCHEMA": _obj({
            "episode": {"type": "integer", "minimum": 1},
            "reason_type": {"type": "string", "enum": [
                "non_narrative", "recap_only", "credits_or_placeholder",
                "corrupted_or_unavailable", "insufficient_evidence", "registry_quarantined_dependency",
            ]},
            "explanation": _NE, "event_ids": _arr(_S),
        }),
        "SERIES_ASSIGNMENT_SCHEMA": _obj({
            "schema_version": {"type": "string", "const": "1.0"},
            "chapter_id": _NE,
            "episodes": _arr({"type": "integer", "minimum": 1}, minItems=1),
            "thread_beats": _arr(_obj({
                "id": _NE, "thread_id": _NE,
                "episode": {"type": "integer", "minimum": 1},
                "phase": {"type": "string", "enum": ["setup", "escalation", "turn", "reveal", "payoff", "consequence", "coda"]},
                "importance": {"type": "string", "enum": ["required", "supporting", "optional"]},
                "summary": _NE, "event_ids": _arr(_S, minItems=1),
                "requires_beat_ids": _arr(_S),
            })),
            "excluded_episodes": _arr(_obj({
                "episode": {"type": "integer", "minimum": 1},
                "reason_type": {"type": "string", "enum": [
                    "non_narrative", "recap_only", "credits_or_placeholder",
                    "corrupted_or_unavailable", "insufficient_evidence", "registry_quarantined_dependency",
                ]},
                "explanation": _NE, "event_ids": _arr(_S),
            })),
        }),
        "SERIES_BIBLE_THREAD_SCHEMA": _obj({
            "thread_key": _NE, "title": _NE, "summary": _NE,
            "status": {"type": "string", "enum": ["resolved", "partially_resolved", "open"]},
            "event_ids": _arr(_event_pat(), minItems=1),
        }),
        "BIBLE_METADATA_SCHEMA": _obj({
            "episode_count": {"type": "integer", "minimum": 1},
            "total_events": {"type": "integer", "minimum": 1},
        }),
        "BIBLE_ENTITY_IMPORTANCE_SCHEMA": _obj({
            "score": {"type": "number", "minimum": 0, "maximum": 1},
            "reason": {"type": "string"},
        }),
    }
