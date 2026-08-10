"""Grounded generation validator — Doc 24 §4.2: three structural constraints for hallucination prevention.

1. validate_source_refs: every beat must cite real scenes with matching characters.
2. validate_temporal_constraints: relationships must be valid at the beat's time point.
3. validate_voice_constraints: dialogue must be similar to real samples (difflib, not LLM).

All validators are deterministic (zero LLM) and return ValidationResult.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Any

from auto_cut_bot.pipeline.core.db.client import StageDBClient
from auto_cut_bot.pipeline.query_tools import get_dialogue_samples


@dataclass
class ValidationResult:
    """Result of a single grounded-generation validation pass.

    Attributes:
        passed: True if no violations were found.
        violations: Human-readable descriptions of each constraint violation.
        suggestions: Optional suggestions for resolving each violation.
    """

    passed: bool = True
    violations: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)

    def merge(self, other: ValidationResult) -> ValidationResult:
        """Combine two results (AND semantics for passed)."""
        return ValidationResult(
            passed=self.passed and other.passed,
            violations=self.violations + other.violations,
            suggestions=self.suggestions + other.suggestions,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "violations": list(self.violations),
            "suggestions": list(self.suggestions),
        }


# ── 1. Source reference validation ────────────────────────────────────────────────


def validate_source_refs(
    beat: dict[str, Any], db: StageDBClient | None, book_id: str
) -> ValidationResult:
    """Every beat's source_refs must point to real scenes with matching characters.

    Args:
        beat: Keys: source_refs (list[str]), characters (list[str]), beat_id (str).
        db: StageDBClient (no-op if None or unavailable).
        book_id: Book identifier for DB queries.
    """
    violations: list[str] = []
    suggestions: list[str] = []
    source_refs = beat.get("source_refs", [])
    beat_id = beat.get("beat_id", "unknown")

    if not source_refs:
        return ValidationResult(
            passed=False,
            violations=[f"Beat '{beat_id}' has no source_refs."],
            suggestions=["Assign source_refs matching beat content to real scenes."],
        )

    if db is None or not db.is_available:
        return ValidationResult(passed=True)

    beat_characters: set[str] = set(beat.get("characters", []))
    for ref in source_refs:
        scenes = db.query_scenes(book_id)
        matching = [s for s in scenes if s.get("scene_id") == ref]
        if not matching:
            violations.append(
                f"Beat '{beat_id}': scene_id '{ref}' does not exist."
            )
            suggestions.append(f"Verify scene_id '{ref}' or remove it from source_refs.")
            continue

        scene = matching[0]
        scene_chars: set[str] = set(scene.get("characters_present", []))
        missing = beat_characters - scene_chars
        if missing:
            violations.append(
                f"Beat '{beat_id}': characters {sorted(missing)} not in "
                f"scene '{ref}' (has: {sorted(scene_chars)})."
            )
            suggestions.append(
                f"Add {sorted(missing)} to scene characters_present, "
                f"remove them from beat, or reference a different scene."
            )

    return ValidationResult(
        passed=len(violations) == 0,
        violations=violations,
        suggestions=suggestions,
    )


# ── 2. Temporal constraint validation ─────────────────────────────────────────────


def validate_temporal_constraints(
    beat: dict[str, Any], db: StageDBClient | None, book_id: str
) -> ValidationResult:
    """Referenced relationships must have valid_episode_range covering the beat's time point.

    Prevents continuity errors (e.g. referencing a relationship that hasn't formed yet).

    Args:
        beat: Keys: episode_id (int), relationship_refs (list of (src, tgt) pairs), beat_id (str).
        db: StageDBClient (no-op if None or unavailable).
        book_id: Book identifier for DB queries.
    """
    violations: list[str] = []
    suggestions: list[str] = []
    relationship_refs = beat.get("relationship_refs", [])
    beat_id = beat.get("beat_id", "unknown")
    beat_episode = beat.get("episode_id")

    if not relationship_refs:
        return ValidationResult(passed=True)
    if beat_episode is None:
        return ValidationResult(
            passed=False,
            violations=[f"Beat '{beat_id}' has relationship_refs but no episode_id."],
            suggestions=["Assign an episode_id to the beat."],
        )
    if db is None or not db.is_available:
        return ValidationResult(passed=True)

    all_names: set[str] = set()
    for rel in relationship_refs:
        if isinstance(rel, (list, tuple)) and len(rel) >= 2:
            all_names.update([rel[0], rel[1]])

    db_relationships = db.query_relationships(book_id, list(all_names))

    for rel in relationship_refs:
        if not isinstance(rel, (list, tuple)) or len(rel) < 2:
            continue
        src_name, tgt_name = rel[0], rel[1]

        match = None
        for r in db_relationships:
            r_src, r_tgt = r.get("source_name", ""), r.get("target_name", "")
            if (r_src == src_name and r_tgt == tgt_name) or (r_src == tgt_name and r_tgt == src_name):
                match = r
                break

        if match is None:
            violations.append(
                f"Beat '{beat_id}': relationship ({src_name}, {tgt_name}) not found in DB."
            )
            suggestions.append(
                f"Verify relationship ({src_name}, {tgt_name}) exists or remove this ref."
            )
            continue

        valid_range = match.get("valid_episode_range")
        if valid_range is None:
            continue  # No range constraint — always valid

        if isinstance(valid_range, (list, tuple)) and len(valid_range) == 2:
            start_ep, end_ep = valid_range
            if not (start_ep <= beat_episode <= end_ep):
                violations.append(
                    f"Beat '{beat_id}' (ep {beat_episode}) references "
                    f"({src_name}, {tgt_name}) valid only in [{start_ep}, {end_ep}]."
                )
                suggestions.append(
                    f"Adjust episode_id to [{start_ep}, {end_ep}], "
                    f"remove ref, or extend valid_episode_range."
                )

    return ValidationResult(
        passed=len(violations) == 0,
        violations=violations,
        suggestions=suggestions,
    )


# ── 3. Voice constraint validation ────────────────────────────────────────────────


def validate_voice_constraints(
    dialogue: str,
    character: str,
    db: StageDBClient | None,
    book_id: str,
    *,
    similarity_threshold: float = 0.25,
    sample_count: int = 5,
) -> ValidationResult:
    """Character dialogue must be similar to real dialogue samples via difflib.SequenceMatcher.

    Deterministic — no LLM calls. Best match ratio must exceed similarity_threshold.

    Args:
        dialogue: Generated dialogue line to validate.
        character: Character name to fetch real samples for.
        db: StageDBClient (no-op if None or unavailable).
        book_id: Book identifier.
        similarity_threshold: Minimum SequenceMatcher ratio (default 0.25).
        sample_count: Number of real samples to fetch (default 5).
    """
    if db is None or not db.is_available:
        return ValidationResult(passed=True)

    samples = get_dialogue_samples(db, book_id, character, n=sample_count)
    if not samples:
        return ValidationResult(
            passed=False,
            violations=[f"No real dialogue samples for '{character}' in book '{book_id}'."],
            suggestions=["Ensure the character has subtitle records, or mark as new in the bible."],
        )

    sample_texts = [s.get("text", "") for s in samples if s.get("text")]
    if not sample_texts:
        return ValidationResult(
            passed=False,
            violations=[f"Dialogue samples for '{character}' have no text content."],
            suggestions=["Check the subtitles table for empty text fields."],
        )

    best_ratio = 0.0
    best_sample = ""
    for sample_text in sample_texts:
        ratio = difflib.SequenceMatcher(None, dialogue, sample_text).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_sample = sample_text

    if best_ratio < similarity_threshold:
        return ValidationResult(
            passed=False,
            violations=[
                f"Dialogue for '{character}' similarity {best_ratio:.3f} "
                f"(threshold: {similarity_threshold}). "
                f"Best match: \"{best_sample[:80]}...\""
            ],
            suggestions=[
                f"Review dialogue for voice consistency with '{character}'. "
                f"Vocabulary, sentence structure, or tone may be off."
            ],
        )

    return ValidationResult(passed=True)


# ── Convenience: run all three validations ────────────────────────────────────────


def validate_beat_grounded(
    beat: dict[str, Any],
    db: StageDBClient | None,
    book_id: str,
    *,
    dialogue: str | None = None,
    character: str | None = None,
    voice_threshold: float = 0.25,
) -> ValidationResult:
    """Run all three grounded-generation validations on a beat, returning merged result.

    Args:
        beat: Beat dict (see individual validators for required keys).
        db: StageDBClient (no-op if None or unavailable).
        book_id: Book identifier.
        dialogue: Optional dialogue line for voice validation.
        character: Optional character name for voice validation.
        voice_threshold: Similarity threshold for voice validation.
    """
    result = validate_source_refs(beat, db, book_id)
    result = result.merge(validate_temporal_constraints(beat, db, book_id))
    if dialogue and character:
        result = result.merge(
            validate_voice_constraints(
                dialogue, character, db, book_id,
                similarity_threshold=voice_threshold,
            )
        )
    return result


__all__ = [
    "ValidationResult",
    "validate_source_refs",
    "validate_temporal_constraints",
    "validate_voice_constraints",
    "validate_beat_grounded",
]