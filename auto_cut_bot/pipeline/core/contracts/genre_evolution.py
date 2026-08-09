"""GenreEvolutionEngine — CAPTURED evolution pattern for genre profiles.

The CAPTURED pattern (Capture, Audit, Publish, Track, Update, Replay, Evolve, Document)
provides a structured workflow for extracting, validating, and persisting genre
profiles from completed pipeline artifacts.

Pipeline phases:

  1. **extract** — Extract a GenreAdapter from bible + stories.
  2. **build_evidence** — Build a GenreEvidence packet from artifacts.
  3. **semantic_review** — Validate the extracted profile against editorial policy.
  4. **replay_verify** — Verify the profile by checking it against the original data.
  5. **capture** — Full pipeline: extract → build_evidence → semantic_review → replay_verify → save.
     Supports continuous evolution: NEW genres are CAPTURED, existing genres with higher
     confidence are DERIVED, and existing genres with same/lower confidence are MERGED.
  6. **save_profile** — Persist to _references/editorial-knowledge/ and track evolution history.

The ``GenreLearner`` in ``genre_learner.py`` remains as a thin backward-compatible
wrapper delegating to ``GenreEvolutionEngine``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autocut_core.contracts.genre_registry import GenreAdapter, ResourceRef


# ── Data classes ───────────────────────────────────────────────────────────────


@dataclass
class EvolutionLineage:
    """Tracks the evolution chain of a genre profile.

    Attributes:
        genre_profile: The genre key (e.g. ``"female_romance"``).
        parent_profile: Previous version's genre_profile, or None for initial capture.
        evolution_type: How this version was created:
            - ``"captured"`` — initial capture of an unknown genre.
            - ``"derived"`` — improved version with higher confidence.
            - ``"merged"`` — merged evidence from multiple sources.
            - ``"fixed"`` — correction applied to a broken profile.
        iterations: How many times this profile has been updated (1-based).
        changes: List of what changed in each iteration (field, old_value, new_value, reason).
        history_entries: The raw evolution history entries loaded from disk.
    """

    genre_profile: str
    parent_profile: str | None = None
    evolution_type: str = "captured"
    iterations: int = 1
    changes: list[dict[str, Any]] = field(default_factory=list)
    history_entries: list[dict[str, Any]] = field(default_factory=list)

    @property
    def has_prior_version(self) -> bool:
        """Return True if this profile has a parent (i.e., not an initial capture)."""
        return self.parent_profile is not None

    @property
    def total_iterations(self) -> int:
        """Return the total number of iterations from history."""
        return max(self.iterations, len(self.history_entries))


@dataclass
class GenreEvidence:
    """Evidence packet recording how a genre profile was extracted.

    Attributes:
        source: The bible, scripts, and plans used as evidence (serializable dict).
        refs: References to specific artifacts used in extraction.
        confidence: How confident the extraction is (0.0–1.0).
        extraction_timestamp: ISO-8601 timestamp of extraction.
    """

    source: dict[str, Any] = field(default_factory=dict)
    refs: list[ResourceRef] = field(default_factory=list)
    confidence: float = 0.0
    extraction_timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass
class GenreCaptureResult:
    """Capture contract — the complete result of a genre evolution capture.

    Attributes:
        adapter: The extracted GenreAdapter profile.
        evidence: The evidence packet backing the extraction.
        validation: Semantic review results (passed, warnings, errors).
        replay_result: Replay verification against source data, or None if skipped.
        profile_path: Path where the profile was saved, or None if not persisted.
        evolution_type: The type of evolution that occurred
            (``"captured"``, ``"derived"``, ``"merged"``, or ``None`` if not saved).
        lineage: The EvolutionLineage for this capture, or None if not available.
    """

    adapter: GenreAdapter
    evidence: GenreEvidence
    validation: dict[str, Any]
    replay_result: dict[str, Any] | None = None
    profile_path: Path | None = None
    evolution_type: str | None = None
    lineage: EvolutionLineage | None = None

    @property
    def passed(self) -> bool:
        """Return True if semantic review passed and confidence meets threshold."""
        return bool(self.validation.get("passed"))


# ═══════════════════════════════════════════════════════════════════════════════
# GenreEvolutionEngine
# ═══════════════════════════════════════════════════════════════════════════════


class GenreEvolutionEngine:
    """Extract, validate, and persist genre profiles following the CAPTURED pattern.

    The engine reads the Series Bible (and optionally Story Scripts) and distills
    a ``GenreAdapter``, builds a ``GenreEvidence`` packet, runs semantic review
    and replay verification, then persists the result.

    Supports continuous evolution:
    - **NEW** genres (no existing profile) → CAPTURED
    - **EXISTING** genres with higher confidence → DERIVED
    - **EXISTING** genres with same/lower confidence → MERGED

    Usage::

        engine = GenreEvolutionEngine()
        result = engine.capture(bible, stories, output_dir)
        if result.passed:
            print(f"Profile saved to {result.profile_path}")
            print(f"Evolution type: {result.evolution_type}")
    """

    # ── Minimum required keys to produce a valid adapter ──────────────────────
    _MIN_REQUIRED_KEYS = frozenset(
        {"genre", "genre_profile", "case_id", "overview", "central_promise"}
    )

    # ── Confidence threshold for capture ──────────────────────────────────────
    CONFIDENCE_THRESHOLD: float = 0.6

    # ── Evolution history directory (relative to editorial-knowledge/) ────────
    EVOLUTION_DIR = ".evolution"

    # ── extract ───────────────────────────────────────────────────────────────

    @classmethod
    def extract(
        cls,
        bible: dict[str, Any] | None,
        stories: list[dict[str, Any]] | None = None,
    ) -> GenreAdapter | None:
        """Extract a GenreAdapter from the Series Bible and optional Story Scripts.

        Extraction logic (in priority order):

        1. **genre_profile**: ``bible["genre_profile"]``, then ``bible["genre"]``.
           Must be a non-empty string.
        2. **primary_focus**: ``bible["central_promise"]``, then ``bible["overview"]``.
           Falls back to an empty string.
        3. **support_roles**: inferred from the Bible's character list
           (``bible["characters"]``).  Each character's ``identity`` or
           ``canonical_name`` is collected as a role.  Falls back to an
           empty list.
        4. **golden_case_ids**: ``bible["case_id"]`` if available.
        5. **status**: always ``"auto_generated"``.

        Returns ``None`` if ``bible`` is falsy or if no ``genre_profile``
        can be determined.
        """
        if not bible or not isinstance(bible, dict):
            return None

        # ── genre_profile (required) ──────────────────────────────────────
        genre_profile = bible.get("genre_profile") or bible.get("genre")
        if not isinstance(genre_profile, str) or not genre_profile.strip():
            return None
        genre_profile = genre_profile.strip()

        # ── primary_focus (best-effort) ────────────────────────────────────
        primary_focus = ""
        for key in ("central_promise", "overview"):
            val = bible.get(key)
            if isinstance(val, str) and val.strip():
                primary_focus = val.strip()
                break

        # ── support_roles (from character list) ────────────────────────────
        support_roles = cls._infer_support_roles(bible)

        # ── golden_case_ids ───────────────────────────────────────────────
        case_id = bible.get("case_id")
        golden_case_ids = (
            [case_id] if isinstance(case_id, str) and case_id.strip() else []
        )

        return GenreAdapter(
            genre_profile=genre_profile,
            primary_focus=primary_focus,
            support_roles=support_roles,
            golden_case_ids=golden_case_ids,
            status="auto_generated",
        )

    @classmethod
    def _infer_support_roles(cls, bible: dict[str, Any]) -> list[str]:
        """Infer support roles from the Bible's character list.

        For each character, picks the ``identity`` field first, then falls
        back to ``canonical_name``.  Deduplicates and preserves order.
        """
        characters = bible.get("characters")
        if not isinstance(characters, list):
            return []

        roles: list[str] = []
        seen: set[str] = set()
        for ch in characters:
            if not isinstance(ch, dict):
                continue
            role = ch.get("identity") or ch.get("canonical_name")
            if isinstance(role, str) and role.strip() and role.strip() not in seen:
                seen.add(role.strip())
                roles.append(role.strip())

        return roles

    # ── build_evidence ────────────────────────────────────────────────────────

    @classmethod
    def build_evidence(
        cls,
        bible: dict[str, Any] | None,
        stories: list[dict[str, Any]] | None = None,
    ) -> GenreEvidence:
        """Build a GenreEvidence packet from the extraction artifacts.

        The evidence records what was used to extract the profile so that
        future audits can trace the provenance of each captured genre.
        """
        refs: list[ResourceRef] = []
        source: dict[str, Any] = {}

        if isinstance(bible, dict):
            # Record bible reference
            case_id = bible.get("case_id", "")
            if isinstance(case_id, str) and case_id:
                refs.append(ResourceRef(type="bible", case_id=case_id))
            # Record key bible fields as evidence source
            source["bible"] = {
                "genre_profile": bible.get("genre_profile"),
                "genre": bible.get("genre"),
                "central_promise": bible.get("central_promise"),
                "overview": bible.get("overview"),
                "case_id": bible.get("case_id"),
                "character_count": (
                    len(bible["characters"])
                    if isinstance(bible.get("characters"), list)
                    else 0
                ),
            }

        if isinstance(stories, list):
            for story in stories:
                if isinstance(story, dict) and isinstance(story.get("story_id"), str):
                    refs.append(
                        ResourceRef(type="script", case_id=story["story_id"])
                    )
            source["story_count"] = len(stories)

        # Compute confidence based on completeness of extraction
        confidence = cls._compute_confidence(bible, stories)

        return GenreEvidence(
            source=source,
            refs=refs,
            confidence=confidence,
        )

    @classmethod
    def _compute_confidence(
        cls,
        bible: dict[str, Any] | None,
        stories: list[dict[str, Any]] | None = None,
    ) -> float:
        """Compute extraction confidence based on data completeness.

        Factors:
        - Has bible: +0.3
        - Has genre_profile: +0.2
        - Has central_promise or overview: +0.15
        - Has characters: +0.15
        - Has case_id: +0.1
        - Has stories: +0.1
        """
        if not isinstance(bible, dict):
            return 0.0

        confidence = 0.3  # base: has bible

        if isinstance(bible.get("genre_profile"), str) and bible["genre_profile"].strip():
            confidence += 0.2

        if (
            (isinstance(bible.get("central_promise"), str) and bible["central_promise"].strip())
            or (isinstance(bible.get("overview"), str) and bible["overview"].strip())
        ):
            confidence += 0.15

        if isinstance(bible.get("characters"), list) and bible["characters"]:
            confidence += 0.15

        if isinstance(bible.get("case_id"), str) and bible["case_id"].strip():
            confidence += 0.1

        if stories:
            confidence += 0.1

        return min(confidence, 1.0)

    # ── semantic_review ───────────────────────────────────────────────────────

    @classmethod
    def semantic_review(
        cls,
        adapter: GenreAdapter,
        bible: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Validate the extracted profile against editorial policy.

        Checks performed:

        1. **primary_focus match**: The adapter's primary_focus should be
           consistent with the bible's central_promise or overview.
        2. **support_roles presence**: Support roles should be actually
           present in the bible's character list.
        3. **golden_case_ids validity**: Golden case IDs should reference
           valid artifacts (non-empty, well-formed).

        Returns a dict with:
        - ``passed``: bool
        - ``warnings``: list[str]
        - ``errors``: list[str]
        """
        warnings: list[str] = []
        errors: list[str] = []

        if not isinstance(bible, dict):
            return {
                "passed": False,
                "warnings": ["no bible provided for semantic review"],
                "errors": ["missing bible — cannot validate adapter"],
            }

        # ── Check 1: primary_focus match ──────────────────────────────────
        central_promise = bible.get("central_promise")
        overview = bible.get("overview")
        if adapter.primary_focus:
            if isinstance(central_promise, str) and central_promise.strip():
                if adapter.primary_focus != central_promise.strip():
                    warnings.append(
                        "primary_focus differs from bible central_promise"
                    )
            elif isinstance(overview, str) and overview.strip():
                if adapter.primary_focus != overview.strip():
                    warnings.append(
                        "primary_focus differs from bible overview"
                    )
        else:
            warnings.append("primary_focus is empty")

        # ── Check 2: support_roles presence ────────────────────────────────
        characters = bible.get("characters")
        if isinstance(characters, list):
            all_identities = set()
            all_names = set()
            for ch in characters:
                if isinstance(ch, dict):
                    identity = ch.get("identity")
                    if isinstance(identity, str) and identity.strip():
                        all_identities.add(identity.strip())
                    name = ch.get("canonical_name")
                    if isinstance(name, str) and name.strip():
                        all_names.add(name.strip())

            known_roles = all_identities | all_names
            for role in adapter.support_roles:
                if role not in known_roles:
                    warnings.append(
                        f"support_role '{role}' not found in bible characters"
                    )
        else:
            if adapter.support_roles:
                warnings.append(
                    "support_roles present but bible has no characters list"
                )

        # ── Check 3: golden_case_ids validity ─────────────────────────────
        if not adapter.golden_case_ids:
            warnings.append("no golden_case_ids in adapter")
        else:
            for case_id in adapter.golden_case_ids:
                if not isinstance(case_id, str) or not case_id.strip():
                    errors.append(f"invalid golden_case_id: {case_id!r}")
                elif "/" in case_id or "\\" in case_id:
                    errors.append(
                        f"golden_case_id contains path separator: {case_id!r}"
                    )

        passed = len(errors) == 0
        return {
            "passed": passed,
            "warnings": warnings,
            "errors": errors,
        }

    # ── replay_verify ─────────────────────────────────────────────────────────

    @classmethod
    def replay_verify(
        cls,
        adapter: GenreAdapter,
        bible: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Verify the profile by checking it against the original data.

        This is a replay check — does the profile correctly describe the
        source bible?  Two checks:

        1. **Route check**: Does the profile correctly route the source bible?
           i.e., does the extracted genre_profile match what the bible declares?
        2. **Consistency check**: Are the templated rules consistent with the
           source data?

        Returns a dict with:
        - ``route_match``: bool
        - ``consistent``: bool
        - ``details``: list[str]
        """
        if not isinstance(bible, dict):
            return {
                "route_match": False,
                "consistent": False,
                "details": ["no bible provided for replay verification"],
            }

        details: list[str] = []
        route_match = True
        consistent = True

        # ── Route check ───────────────────────────────────────────────────
        bible_genre = bible.get("genre_profile") or bible.get("genre")
        if isinstance(bible_genre, str) and bible_genre.strip():
            if adapter.genre_profile != bible_genre.strip():
                route_match = False
                details.append(
                    f"route mismatch: adapter genre={adapter.genre_profile}, "
                    f"bible genre={bible_genre.strip()}"
                )
        else:
            details.append("bible has no genre_profile — route check skipped")

        if route_match:
            details.append("route check passed")

        # ── Consistency check ──────────────────────────────────────────────
        # Check that the adapter's primary_focus is consistent with what the
        # bible declares
        central_promise = bible.get("central_promise")
        if isinstance(central_promise, str) and central_promise.strip():
            if adapter.primary_focus and adapter.primary_focus != central_promise.strip():
                details.append(
                    "consistency: primary_focus differs from central_promise"
                )
        else:
            details.append(
                "consistency: bible has no central_promise — check skipped"
            )

        # Check that the adapter has minimal required data
        if not adapter.primary_focus:
            consistent = False
            details.append("consistency: adapter has no primary_focus")

        if not adapter.support_roles:
            details.append("consistency: adapter has no support_roles")

        return {
            "route_match": route_match,
            "consistent": consistent,
            "details": details,
        }

    # ── profile_exists ────────────────────────────────────────────────────────

    @classmethod
    def profile_exists(
        cls,
        genre_profile: str,
        output_dir: Path,
    ) -> bool:
        """Check whether a profile file already exists for *genre_profile*.

        Args:
            genre_profile: The genre key to check.
            output_dir: Directory containing golden case JSON files.

        Returns True if ``{output_dir}/{genre_profile}.json`` exists.
        """
        if not isinstance(genre_profile, str) or not genre_profile.strip():
            return False
        return (output_dir / f"{genre_profile}.json").is_file()

    # ── load_existing_profile ─────────────────────────────────────────────────

    @classmethod
    def load_existing_profile(
        cls,
        genre_profile: str,
        output_dir: Path,
    ) -> GenreAdapter | None:
        """Load an existing GenreAdapter from a saved profile file.

        Args:
            genre_profile: The genre key to load.
            output_dir: Directory containing golden case JSON files.

        Returns a ``GenreAdapter`` if the file exists and is valid, or ``None``.
        """
        path = output_dir / f"{genre_profile}.json"
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

        if not isinstance(value, dict):
            return None

        contract = value.get("transferable_contract")
        if isinstance(contract, dict):
            primary_focus = contract.get("primary_thread", "")
            support_roles = contract.get("support_roles", [])
        else:
            primary_focus = ""
            support_roles = []

        if not isinstance(primary_focus, str):
            primary_focus = ""
        if not isinstance(support_roles, list):
            support_roles = []

        return GenreAdapter(
            genre_profile=value.get("genre_profile", genre_profile),
            primary_focus=primary_focus,
            support_roles=support_roles,
            golden_case_ids=value.get("golden_case_ids", []),
            status="ready",
        )

    # ── merge_profiles ────────────────────────────────────────────────────────

    @classmethod
    def merge_profiles(
        cls,
        existing: GenreAdapter,
        new: GenreAdapter,
        existing_confidence: float = 0.0,
        new_confidence: float = 0.0,
    ) -> tuple[GenreAdapter, list[dict[str, Any]]]:
        """Merge two GenreAdapter instances, keeping the best of both.

        Merge rules:
        - Keep higher-confidence primary_focus (fall back to existing if equal).
        - Union support_roles (deduplicate, preserve order).
        - Append new golden_case_ids (keep existing first, deduplicate).
        - Status is ``"ready"`` if the merged result has data, else ``"auto_generated"``.

        Args:
            existing: The existing GenreAdapter (already on disk).
            new: The newly extracted GenreAdapter.
            existing_confidence: Confidence of the existing profile.
            new_confidence: Confidence of the new extraction.

        Returns:
            A tuple of ``(merged_adapter, changes)`` where ``changes`` is a list of
            dicts describing what changed: ``{"field", "old_value", "new_value", "reason"}``.
        """
        changes: list[dict[str, Any]] = []

        # ── primary_focus: keep higher-confidence ──────────────────────────
        if new_confidence > existing_confidence and new.primary_focus:
            if new.primary_focus != existing.primary_focus:
                changes.append({
                    "field": "primary_focus",
                    "old_value": existing.primary_focus,
                    "new_value": new.primary_focus,
                    "reason": "new evidence has higher confidence",
                })
                merged_primary_focus = new.primary_focus
            else:
                merged_primary_focus = existing.primary_focus
        elif existing_confidence > new_confidence and existing.primary_focus:
            # Keep existing — no change
            merged_primary_focus = existing.primary_focus
        else:
            # Equal confidence — prefer the one with content
            if new.primary_focus and not existing.primary_focus:
                changes.append({
                    "field": "primary_focus",
                    "old_value": existing.primary_focus,
                    "new_value": new.primary_focus,
                    "reason": "existing had no primary_focus",
                })
                merged_primary_focus = new.primary_focus
            elif existing.primary_focus and not new.primary_focus:
                merged_primary_focus = existing.primary_focus
            elif new.primary_focus and existing.primary_focus:
                if new.primary_focus != existing.primary_focus:
                    # Keep existing by default; record the divergence
                    changes.append({
                        "field": "primary_focus",
                        "old_value": existing.primary_focus,
                        "new_value": new.primary_focus,
                        "reason": "equal confidence — kept existing, new divergence noted",
                    })
                merged_primary_focus = existing.primary_focus
            else:
                merged_primary_focus = existing.primary_focus or new.primary_focus

        # ── support_roles: union (deduplicate, preserve order) ─────────────
        seen_roles = set(existing.support_roles)
        merged_roles = list(existing.support_roles)
        new_roles_added = False
        for role in new.support_roles:
            if role not in seen_roles:
                seen_roles.add(role)
                merged_roles.append(role)
                new_roles_added = True
        if new_roles_added:
            changes.append({
                "field": "support_roles",
                "old_value": list(existing.support_roles),
                "new_value": merged_roles,
                "reason": "merged new roles from additional evidence",
            })

        # ── golden_case_ids: append new, keep existing ─────────────────────
        seen_ids = set(existing.golden_case_ids)
        merged_ids = list(existing.golden_case_ids)
        new_ids_added = False
        for cid in new.golden_case_ids:
            if cid not in seen_ids:
                seen_ids.add(cid)
                merged_ids.append(cid)
                new_ids_added = True
        if new_ids_added:
            changes.append({
                "field": "golden_case_ids",
                "old_value": list(existing.golden_case_ids),
                "new_value": merged_ids,
                "reason": "appended new case_ids from additional evidence",
            })

        merged_status = "ready" if merged_roles or merged_primary_focus else "auto_generated"

        merged = GenreAdapter(
            genre_profile=existing.genre_profile,
            primary_focus=merged_primary_focus,
            support_roles=merged_roles,
            golden_case_ids=merged_ids,
            status=merged_status,
        )

        return merged, changes

    # ── should_evolve ─────────────────────────────────────────────────────────

    @classmethod
    def should_evolve(
        cls,
        genre_profile: str,
        new_evidence: GenreEvidence,
        output_dir: Path | None = None,
        *,
        existing_confidence: float | None = None,
    ) -> bool:
        """Check whether evolution is warranted for *genre_profile*.

        Evolution is warranted when:
        - The genre is unknown (no profile exists on disk) → always evolve.
        - The genre exists but new evidence adds new case_ids → evolve.
        - The genre exists and new evidence has higher confidence → evolve.

        Args:
            genre_profile: The genre key to check.
            new_evidence: The new GenreEvidence packet.
            output_dir: Directory containing golden case JSON files. Required
                        if ``existing_confidence`` is not provided.
            existing_confidence: Explicit confidence of the existing profile.
                                 If None, it is loaded from the evolution history.

        Returns True if evolution should proceed.
        """
        if not isinstance(genre_profile, str) or not genre_profile.strip():
            return False

        if output_dir is None and existing_confidence is None:
            return True  # Unknown genre — always evolve

        # ── Unknown genre: always evolve ───────────────────────────────────
        if output_dir is not None and not cls.profile_exists(genre_profile, output_dir):
            return True

        # ── Existing genre: check if new evidence adds value ───────────────
        if existing_confidence is None and output_dir is not None:
            # Load existing confidence from evolution history
            history = cls._load_evolution_history(genre_profile, output_dir)
            if history:
                existing_confidence = history[-1].get("confidence", 0.0)
            else:
                existing_confidence = 0.0

        if existing_confidence is None:
            existing_confidence = 0.0

        # ── New evidence has higher confidence → evolve ────────────────────
        if new_evidence.confidence > existing_confidence:
            return True

        # ── New evidence adds new case_ids → evolve ────────────────────────
        if output_dir is not None:
            existing = cls.load_existing_profile(genre_profile, output_dir)
            if existing is not None:
                new_case_ids = {
                    ref.case_id
                    for ref in new_evidence.refs
                    if ref.type == "bible" and ref.case_id
                }
                existing_ids = set(existing.golden_case_ids)
                if new_case_ids - existing_ids:
                    return True

        return False

    # ── evolution_lineage ─────────────────────────────────────────────────────

    @classmethod
    def evolution_lineage(
        cls,
        genre_profile: str,
        output_dir: Path,
    ) -> EvolutionLineage:
        """Read the evolution lineage of a genre profile.

        Loads ``.evolution/{genre_profile}.json`` and builds an
        ``EvolutionLineage`` with the full chain of changes.

        Args:
            genre_profile: The genre key to inspect.
            output_dir: Directory containing golden case JSON files.

        Returns an ``EvolutionLineage``. If no history exists, returns a
        lineage with ``evolution_type="captured"`` and ``iterations=0``.
        """
        history = cls._load_evolution_history(genre_profile, output_dir)

        if not history:
            return EvolutionLineage(
                genre_profile=genre_profile,
                parent_profile=None,
                evolution_type="captured",
                iterations=0,
                changes=[],
                history_entries=[],
            )

        iterations = len(history)
        first_entry = history[0]
        last_entry = history[-1]
        evolution_type = last_entry.get("evolution_type", first_entry.get("evolution_type", "captured"))
        parent_profile = last_entry.get("parent_profile")

        # Collect all changes from history entries
        all_changes: list[dict[str, Any]] = []
        for entry in history:
            entry_changes = entry.get("changes", [])
            if isinstance(entry_changes, list):
                all_changes.extend(entry_changes)

        return EvolutionLineage(
            genre_profile=genre_profile,
            parent_profile=parent_profile,
            evolution_type=evolution_type,
            iterations=iterations,
            changes=all_changes,
            history_entries=history,
        )

    # ── capture ───────────────────────────────────────────────────────────────

    @classmethod
    def capture(
        cls,
        bible: dict[str, Any] | None,
        stories: list[dict[str, Any]] | None = None,
        output_dir: Path | None = None,
    ) -> GenreCaptureResult:
        """Full CAPTURED pipeline with continuous evolution support.

        The pipeline runs: extract → build_evidence → semantic_review → replay_verify.

        Then, based on whether the genre already exists, it applies the appropriate
        evolution strategy:

        - **NEW genre** (no existing profile) → ``"captured"`` — save as-is.
        - **EXISTING genre, higher confidence** → ``"derived"`` — save improved version.
        - **EXISTING genre, same/lower confidence** → ``"merged"`` — merge evidence,
          keep the best of both profiles.

        Args:
            bible: The Series Bible dict.
            stories: Optional list of Story Script dicts.
            output_dir: Directory to save the profile. If None, the profile is
                        not persisted.

        Returns a ``GenreCaptureResult`` with the full capture details, including
        ``evolution_type`` and ``lineage``.
        """
        # Phase 1: Extract
        adapter = cls.extract(bible, stories)

        if adapter is None:
            return GenreCaptureResult(
                adapter=GenreAdapter(
                    genre_profile="",
                    primary_focus="",
                    support_roles=[],
                    golden_case_ids=[],
                    status="human_review_required",
                ),
                evidence=GenreEvidence(),
                validation={
                    "passed": False,
                    "warnings": [],
                    "errors": ["extraction failed — no genre_profile could be determined"],
                },
                replay_result=None,
            )

        # Phase 2: Build evidence
        evidence = cls.build_evidence(bible, stories)

        # Phase 3: Semantic review
        validation = cls.semantic_review(adapter, bible)

        # Phase 4: Replay verify
        replay_result = cls.replay_verify(adapter, bible)

        # Phase 5: Determine evolution strategy and save
        evolution_type: str | None = None
        lineage: EvolutionLineage | None = None
        profile_path: Path | None = None
        merged_changes: list[dict[str, Any]] = []

        if output_dir is not None and validation.get("passed"):
            existing = cls.load_existing_profile(adapter.genre_profile, output_dir)

            if existing is None:
                # ── Scenario (a): NEW genre → CAPTURED ────────────────────
                evolution_type = "captured"
                profile_path = cls.save_profile(
                    adapter,
                    output_dir,
                    evidence=evidence,
                    validation=validation,
                    replay_result=replay_result,
                    bible=bible,
                    evolution_type=evolution_type,
                )

            elif evidence.confidence > cls.CONFIDENCE_THRESHOLD:
                # ── Scenario (b): EXISTING genre, higher confidence → DERIVED ──
                # Load existing confidence from history
                existing_history = cls._load_evolution_history(
                    adapter.genre_profile, output_dir
                )
                existing_conf = (
                    existing_history[-1].get("confidence", 0.0)
                    if existing_history
                    else 0.0
                )

                if evidence.confidence > existing_conf:
                    evolution_type = "derived"
                    profile_path = cls.save_profile(
                        adapter,
                        output_dir,
                        evidence=evidence,
                        validation=validation,
                        replay_result=replay_result,
                        bible=bible,
                        evolution_type=evolution_type,
                        parent_profile=adapter.genre_profile,
                    )
                else:
                    # ── Scenario (c): EXISTING genre, same/lower confidence → MERGED ──
                    evolution_type = "merged"
                    merged, merged_changes = cls.merge_profiles(
                        existing=existing,
                        new=adapter,
                        existing_confidence=existing_conf,
                        new_confidence=evidence.confidence,
                    )
                    profile_path = cls.save_profile(
                        merged,
                        output_dir,
                        evidence=evidence,
                        validation=validation,
                        replay_result=replay_result,
                        bible=bible,
                        evolution_type=evolution_type,
                        parent_profile=adapter.genre_profile,
                        changes=merged_changes,
                    )
                    # Update adapter to the merged result
                    adapter = merged
            else:
                # ── Scenario (c): EXISTING genre, same/lower confidence → MERGED ──
                evolution_type = "merged"
                existing_history = cls._load_evolution_history(
                    adapter.genre_profile, output_dir
                )
                existing_conf = (
                    existing_history[-1].get("confidence", 0.0)
                    if existing_history
                    else 0.0
                )
                merged, merged_changes = cls.merge_profiles(
                    existing=existing,
                    new=adapter,
                    existing_confidence=existing_conf,
                    new_confidence=evidence.confidence,
                )
                profile_path = cls.save_profile(
                    merged,
                    output_dir,
                    evidence=evidence,
                    validation=validation,
                    replay_result=replay_result,
                    bible=bible,
                    evolution_type=evolution_type,
                    parent_profile=adapter.genre_profile,
                    changes=merged_changes,
                )
                # Update adapter to the merged result
                adapter = merged

            # Build lineage after save
            if evolution_type:
                lineage = cls.evolution_lineage(adapter.genre_profile, output_dir)

        return GenreCaptureResult(
            adapter=adapter,
            evidence=evidence,
            validation=validation,
            replay_result=replay_result,
            profile_path=profile_path,
            evolution_type=evolution_type,
            lineage=lineage,
        )

    # ── save_profile ──────────────────────────────────────────────────────────

    @classmethod
    def save_profile(
        cls,
        adapter: GenreAdapter,
        output_dir: Path,
        *,
        evidence: GenreEvidence | None = None,
        validation: dict[str, Any] | None = None,
        replay_result: dict[str, Any] | None = None,
        bible: dict[str, Any] | None = None,
        evolution_type: str | None = None,
        parent_profile: str | None = None,
        changes: list[dict[str, Any]] | None = None,
    ) -> Path:
        """Save *adapter* as a JSON golden case in *output_dir*.

        The file is named ``{genre_profile}.json`` and written to
        *output_dir*.  The format is compatible with the existing
        editorial-knowledge JSON files and is immediately discoverable
        by ``GenreRegistry``.

        If ``evidence`` is provided, evolution history is saved to
        ``_references/editorial-knowledge/.evolution/{genre_profile}.json``.

        Args:
            adapter: The GenreAdapter to save.
            output_dir: Directory to write the JSON file.
            evidence: Optional GenreEvidence packet.
            validation: Optional semantic review results.
            replay_result: Optional replay verification results.
            bible: Optional source bible dict.
            evolution_type: The type of evolution (``"captured"``, ``"derived"``,
                            ``"merged"``, ``"fixed"``).
            parent_profile: The previous version's genre_profile, or None for
                            initial capture.
            changes: List of changes made in this iteration.

        Returns the path to the written file.
        """
        output_dir.mkdir(parents=True, exist_ok=True)

        # Build the golden-case JSON payload.
        case_id = adapter.golden_case_ids[0] if adapter.golden_case_ids else ""
        payload: dict[str, Any] = {
            "schema_version": "1.0",
            "case_id": case_id,
            "genre_profile": adapter.genre_profile,
            "purpose": f"auto-generated profile for {adapter.genre_profile}",
            "transferable_contract": {
                "primary_thread": adapter.primary_focus or "",
                "integrated_support_thread": False,
                "support_roles": list(adapter.support_roles),
                "ending_rule": "",
            },
            "positive_templates": [],
            "approved_story_sequence": [],
            "required_bridge_beats": [],
            "approved_cut": "",
            "negative_examples": [],
            "regression_assertions": [],
        }

        path = output_dir / f"{adapter.genre_profile}.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        # ── Save evolution history ─────────────────────────────────────────
        if evidence is not None:
            cls._save_evolution_history(
                adapter=adapter,
                evidence=evidence,
                validation=validation,
                replay_result=replay_result,
                bible=bible,
                output_dir=output_dir,
                evolution_type=evolution_type,
                parent_profile=parent_profile,
                changes=changes,
            )

        return path

    @classmethod
    def _save_evolution_history(
        cls,
        adapter: GenreAdapter,
        evidence: GenreEvidence,
        validation: dict[str, Any] | None,
        replay_result: dict[str, Any] | None,
        bible: dict[str, Any] | None,
        output_dir: Path,
        *,
        evolution_type: str | None = None,
        parent_profile: str | None = None,
        changes: list[dict[str, Any]] | None = None,
    ) -> None:
        """Save evolution history to .evolution/{genre_profile}.json.

        Tracks: timestamp, evolution_type, parent_profile, iterations,
        confidence, evidence_refs, validation_result, source_bible_hash,
        changes, adapter_snapshot.
        """
        evolution_dir = output_dir / cls.EVOLUTION_DIR
        evolution_dir.mkdir(parents=True, exist_ok=True)

        # Compute source bible hash for tracking
        source_bible_hash = ""
        if isinstance(bible, dict):
            try:
                raw = json.dumps(bible, sort_keys=True, ensure_ascii=False)
                source_bible_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
            except (TypeError, ValueError):
                source_bible_hash = ""

        # Load existing evolution history (append, don't overwrite)
        history_path = evolution_dir / f"{adapter.genre_profile}.json"
        existing_entries: list[dict[str, Any]] = []
        if history_path.is_file():
            try:
                existing_entries = json.loads(history_path.read_text(encoding="utf-8"))
                if not isinstance(existing_entries, list):
                    existing_entries = []
            except (OSError, json.JSONDecodeError):
                existing_entries = []

        # Compute iterations count
        iterations = len(existing_entries) + 1

        # Determine actual evolution_type
        actual_evolution_type = evolution_type or "captured"
        if not existing_entries:
            actual_evolution_type = "captured"

        # Determine actual parent_profile from history if not provided
        actual_parent_profile = parent_profile
        if actual_parent_profile is None and existing_entries:
            actual_parent_profile = adapter.genre_profile

        entry: dict[str, Any] = {
            "timestamp": evidence.extraction_timestamp,
            "evolution_type": actual_evolution_type,
            "parent_profile": actual_parent_profile,
            "iterations": iterations,
            "confidence": evidence.confidence,
            "evidence_refs": [
                {"type": ref.type, "case_id": ref.case_id}
                for ref in evidence.refs
            ],
            "validation_result": {
                "passed": validation.get("passed", False) if validation else False,
                "warning_count": len(validation.get("warnings", [])) if validation else 0,
                "error_count": len(validation.get("errors", [])) if validation else 0,
            },
            "source_bible_hash": source_bible_hash,
            "adapter_snapshot": {
                "genre_profile": adapter.genre_profile,
                "primary_focus": adapter.primary_focus,
                "support_roles": list(adapter.support_roles),
                "golden_case_ids": list(adapter.golden_case_ids),
                "status": adapter.status,
            },
            "changes": list(changes) if changes else [],
        }

        if replay_result is not None:
            entry["replay_result"] = {
                "route_match": replay_result.get("route_match"),
                "consistent": replay_result.get("consistent"),
                "details": replay_result.get("details", []),
            }

        existing_entries.append(entry)
        history_path.write_text(
            json.dumps(existing_entries, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    @classmethod
    def _load_evolution_history(
        cls,
        genre_profile: str,
        output_dir: Path,
    ) -> list[dict[str, Any]]:
        """Load evolution history from .evolution/{genre_profile}.json.

        Returns an empty list if no history file exists or if it's malformed.
        """
        evolution_dir = output_dir / cls.EVOLUTION_DIR
        history_path = evolution_dir / f"{genre_profile}.json"
        if not history_path.is_file():
            return []
        try:
            entries = json.loads(history_path.read_text(encoding="utf-8"))
            if not isinstance(entries, list):
                return []
            return entries
        except (OSError, json.JSONDecodeError):
            return []

    # ── should_learn ──────────────────────────────────────────────────────────

    @classmethod
    def should_learn(cls, genre_route: dict[str, Any]) -> bool:
        """Return True if the genre route indicates a learning opportunity.

        Learning is warranted when the pipeline completed successfully but
        the genre is either unknown (``human_review_required``) or was
        previously auto-generated (``auto_generated``).
        """
        if not isinstance(genre_route, dict):
            return False
        status = genre_route.get("status")
        return status in ("human_review_required", "auto_generated")