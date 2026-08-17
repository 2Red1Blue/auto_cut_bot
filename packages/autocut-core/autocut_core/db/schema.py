"""DB schema constants — table names, alignment confidence, and data source enums.

These constants are shared between StageDBClient, Pydantic entity models,
and Stage contracts' db_reads/db_writes declarations.
"""

from __future__ import annotations

# ── Table names ────────────────────────────────────────────────────────────────

TABLE_BOOKS = "books"
TABLE_SUBJECTS = "subjects"
TABLE_RELATIONSHIPS = "relationships"
TABLE_EPISODES = "episodes"
TABLE_SUBTITLES = "subtitles"
TABLE_SPEAKER_MAPPINGS = "speaker_mappings"
TABLE_SUBJECT_EPISODES = "subject_episodes"
TABLE_SHOTS = "shots"
TABLE_SCENES = "scenes"
TABLE_BOUNDARIES = "boundaries"
TABLE_GLOBAL_CONTEXT = "global_context"
TABLE_VLM_CONFIDENCE_LOG = "vlm_confidence_log"
TABLE_HIGHLIGHT_SKILL_EVOLUTION = "highlight_skill_evolution"
TABLE_HIGHLIGHT_SKILL_VERSIONS = "highlight_skill_versions"
TABLE_HIGHLIGHT_CANDIDATES = "highlight_candidates"
TABLE_SPAN_CANDIDATES = "span_candidates"
TABLE_FINAL_CLIPS = "final_clips"
TABLE_STORY_EVENTS = "story_events"
TABLE_STORY_THREADS = "story_threads"
TABLE_STORY_THREAD_BEATS = "story_thread_beats"
TABLE_STORY_FACTS = "story_facts"

# ── Alignment confidence enum ─────────────────────────────────────────────────

ALIGNMENT_EXACT = "exact"
ALIGNMENT_FUZZY = "fuzzy"
ALIGNMENT_INFERRED = "inferred"
ALIGNMENT_NONE = "none"

# ── Data source enum ──────────────────────────────────────────────────────────

SOURCE_API = "api"
SOURCE_ASR = "asr"
SOURCE_VLM = "vlm"
SOURCE_SCRIPT = "script"

# ── All table names as a set ──────────────────────────────────────────────────

ALL_TABLES: set[str] = {
    TABLE_BOOKS,
    TABLE_SUBJECTS,
    TABLE_RELATIONSHIPS,
    TABLE_EPISODES,
    TABLE_SUBTITLES,
    TABLE_SPEAKER_MAPPINGS,
    TABLE_SUBJECT_EPISODES,
    TABLE_SHOTS,
    TABLE_SCENES,
    TABLE_BOUNDARIES,
    TABLE_GLOBAL_CONTEXT,
    TABLE_VLM_CONFIDENCE_LOG,
    TABLE_HIGHLIGHT_SKILL_EVOLUTION,
    TABLE_HIGHLIGHT_SKILL_VERSIONS,
    TABLE_HIGHLIGHT_CANDIDATES,
    TABLE_SPAN_CANDIDATES,
    TABLE_FINAL_CLIPS,
    TABLE_STORY_EVENTS,
    TABLE_STORY_THREADS,
    TABLE_STORY_THREAD_BEATS,
    TABLE_STORY_FACTS,
}

__all__ = [
    "TABLE_BOOKS",
    "TABLE_SUBJECTS",
    "TABLE_RELATIONSHIPS",
    "TABLE_EPISODES",
    "TABLE_SUBTITLES",
    "TABLE_SPEAKER_MAPPINGS",
    "TABLE_SUBJECT_EPISODES",
    "TABLE_SHOTS",
    "TABLE_SCENES",
    "TABLE_BOUNDARIES",
    "TABLE_GLOBAL_CONTEXT",
    "TABLE_VLM_CONFIDENCE_LOG",
    "TABLE_HIGHLIGHT_SKILL_EVOLUTION",
    "TABLE_HIGHLIGHT_SKILL_VERSIONS",
    "TABLE_HIGHLIGHT_CANDIDATES",
    "TABLE_SPAN_CANDIDATES",
    "TABLE_FINAL_CLIPS",
    "TABLE_STORY_EVENTS",
    "TABLE_STORY_THREADS",
    "TABLE_STORY_THREAD_BEATS",
    "TABLE_STORY_FACTS",
    "ALIGNMENT_EXACT",
    "ALIGNMENT_FUZZY",
    "ALIGNMENT_INFERRED",
    "ALIGNMENT_NONE",
    "SOURCE_API",
    "SOURCE_ASR",
    "SOURCE_VLM",
    "SOURCE_SCRIPT",
    "ALL_TABLES",
]