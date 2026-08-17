"""Genre/profile routing for the Story-first editorial contract.

The router is deliberately conservative: a genre adapter is loaded only from
an explicit, evidence-backed Series Bible classification.  It never guesses a
genre from a title, a keyword, or a high-light candidate.

Genre profiles are discovered at startup from ``_references/editorial-knowledge/*.json``
via ``GenreRegistry``.  Adding a new genre is a data-only operation — drop a JSON
file and the registry discovers it automatically.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from autocut_core.contracts.genre_evolution import GenreCaptureResult, GenreEvolutionEngine
from autocut_core.contracts.genre_learner import GenreLearner
from autocut_core.contracts.genre_registry import (
    GENERIC_ADAPTER,
    GenreAdapter,
    GenreRegistry,
    PROJECT_SPECIFIC_ADAPTER,
)

ROOT = Path(__file__).resolve().parents[2]
GOLDEN_CASE_DIR = ROOT / "autocut_core" / "_references" / "editorial-knowledge"
# Golden case directory can be overridden via env var for private deployments
_ENV_GOLDEN_CASE_DIR = os.environ.get("AUTOCUT_GOLDEN_CASE_DIR")
if _ENV_GOLDEN_CASE_DIR:
    GOLDEN_CASE_DIR = Path(_ENV_GOLDEN_CASE_DIR)

# Legacy Arya golden case is deployment-specific; configured via AUTOCUT_GOLDEN_CASE env var
# or by passing an explicit path to GenreRegistry.build(). No default in open-source.
_arya_env = os.environ.get("AUTOCUT_GOLDEN_CASE")
LEGACY_ARYA_PATH = Path(_arya_env) if _arya_env else None
MIN_CONFIDENCE = 0.75

# ── Singleton registry — built once, shared by all routing functions ──────────
_registry: GenreRegistry | None = None


def _get_registry() -> GenreRegistry:
    """Lazily build and cache the GenreRegistry singleton."""
    global _registry
    if _registry is None:
        _registry = GenreRegistry.build(
            GOLDEN_CASE_DIR,
            legacy_arya_path=LEGACY_ARYA_PATH,
        )
    return _registry


def reload_registry() -> GenreRegistry:
    """Force-rebuild the registry (useful for testing or hot-reload)."""
    global _registry
    _registry = GenreRegistry.build(
        GOLDEN_CASE_DIR,
        legacy_arya_path=LEGACY_ARYA_PATH,
    )
    return _registry


def _adapter_to_context(adapter: GenreAdapter) -> dict[str, Any]:
    """Convert a GenreAdapter to the legacy route dict format."""
    return {
        "genre_profile": adapter.genre_profile,
        "status": adapter.status,
        "primary_focus": adapter.primary_focus,
        "support_roles": list(adapter.support_roles),
        "golden_case_ids": list(adapter.golden_case_ids),
    }


def profile_context(profile: str) -> dict[str, Any]:
    """Return the route context for *profile*.

    Unknown profiles return the ``generic`` adapter (status ``"human_review_required"``)
    instead of raising or returning a bare ``UNKNOWN_PROFILE`` string.
    """
    registry = _get_registry()
    adapter = registry.get(profile)
    return _adapter_to_context(adapter)


def has_explicit_genre_contract(bible: dict[str, Any]) -> bool:
    """Return whether the Bible came through the new typed routing contract."""
    return any(
        key in bible
        for key in (
            "genre_profile",
            "genre_confidence",
            "genre_evidence_event_ids",
            "genre_review_status",
        )
    )


def golden_case_path(case_id: str) -> Path:
    """Resolve the file system path for a golden case *case_id*.

    Search order:
    1. ``_references/editorial-knowledge/{case_id}.json``
    2. Arya fixture at ``_references/editorial-golden-case-arya.json``
    3. Fallback at ``_references/{case_id}.json``
    """
    if not isinstance(case_id, str) or not case_id or "/" in case_id or "\\" in case_id:
        raise ValueError(f"invalid golden case id: {case_id!r}")

    routed = GOLDEN_CASE_DIR / f"{case_id}.json"
    if routed.is_file():
        return routed

    # Legacy Arya fixture
    if case_id == "arya-rebirth-revenge-final-cut-v1" and LEGACY_ARYA_PATH is not None and LEGACY_ARYA_PATH.is_file():
        return LEGACY_ARYA_PATH

    legacy = ROOT / "autocut_core" / "_references" / f"{case_id}.json"
    return legacy


def load_golden_case(case_id: str) -> dict[str, Any]:
    """Load and validate a single golden case by *case_id*."""
    path = golden_case_path(case_id)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load golden case {case_id}: {exc}") from exc

    if not isinstance(value, dict) or value.get("case_id") != case_id:
        raise ValueError(f"golden case identity mismatch: {path}")

    # Validate genre consistency via the registry.
    registry = _get_registry()
    expected_genre = value.get("genre_profile")
    actual_genre = registry.genre_for_case(case_id)
    if expected_genre is not None and actual_genre is not None and expected_genre != actual_genre:
        raise ValueError(f"golden case genre mismatch: {path}")

    if not isinstance(value.get("positive_templates"), list) or len(value["positive_templates"]) < 3:
        raise ValueError(f"golden case needs at least 3 positive templates: {path}")

    negatives = value.get("negative_examples")
    if not isinstance(negatives, list) or len(negatives) < 3:
        raise ValueError(f"golden case needs at least 3 negative examples: {path}")
    if any(item.get("expected_status") != "blocked" for item in negatives if isinstance(item, dict)):
        raise ValueError(f"golden case negative examples must be blocked: {path}")

    return value


def load_golden_cases(case_ids: list[str]) -> list[dict[str, Any]]:
    """Load multiple golden cases."""
    return [load_golden_case(case_id) for case_id in case_ids]


def route_bible(bible: dict[str, Any]) -> dict[str, Any]:
    """Read only explicit Bible classification; never infer a genre from keywords."""
    profile = bible.get("genre_profile")
    confidence = bible.get("genre_confidence")
    evidence = bible.get("genre_evidence_event_ids", [])

    registry = _get_registry()
    adapter = registry.get(profile if isinstance(profile, str) else "generic")
    route = _adapter_to_context(adapter)

    route["confidence"] = confidence if isinstance(confidence, (int, float)) else 0.0
    route["genre_evidence_event_ids"] = evidence if isinstance(evidence, list) else []

    if route["genre_profile"] not in registry.known_genres() or route["confidence"] < MIN_CONFIDENCE:
        route["status"] = "human_review_required"
    if not route["genre_evidence_event_ids"]:
        route["status"] = "human_review_required"

    if route["status"] == "ready":
        route["golden_cases"] = load_golden_cases(route["golden_case_ids"])
    else:
        route["golden_cases"] = []

    return route


def validate_story_route(
    story: dict[str, Any],
    bible_route: dict[str, Any],
) -> list[str]:
    """Validate that a story's route matches the Bible's route."""
    errors: list[str] = []
    if bible_route.get("status") != "ready":
        errors.append("genre_profile is unknown or low-confidence; human review is required")
    if story.get("genre_profile") != bible_route.get("genre_profile"):
        errors.append("Story genre_profile does not match the Series Bible")
    expected = set(bible_route.get("golden_case_ids", []))
    observed_value = story.get("golden_case_ids", [])
    observed = set(observed_value if isinstance(observed_value, list) else [])
    if expected != observed:
        errors.append(
            "Story golden_case_ids do not match the selected genre adapter"
        )
    # Only enforce arya-specific rule if the arya golden case is configured
    if LEGACY_ARYA_PATH is not None and "arya-rebirth-revenge-final-cut-v1" in observed and story.get(
        "genre_profile"
    ) != "female_rebirth_revenge":
        errors.append("Arya golden case is restricted to female_rebirth_revenge")
    return errors


def learn_from_bible(
    bible: dict[str, Any],
    stories: list[dict[str, Any]] | None = None,
) -> Path | None:
    """Extract a genre profile from the Series Bible and save it as a golden case.

    This is called after ``story_render`` completes successfully.  If the
    Bible contains enough data to infer a genre profile, the learner checks
    ``should_evolve()`` before saving.  Future pipeline runs will discover
    the new profile via ``GenreRegistry`` automatically.

    Returns the path to the saved file, or ``None`` if no profile could be
    extracted or if evolution is not warranted.
    """
    adapter = GenreLearner.extract(bible, stories)
    if adapter is None:
        return None

    # Check if evolution is warranted before saving
    evidence = GenreEvolutionEngine.build_evidence(bible, stories)
    if not GenreEvolutionEngine.should_evolve(
        adapter.genre_profile, evidence, GOLDEN_CASE_DIR
    ):
        return None

    return GenreLearner.save_profile(adapter, GOLDEN_CASE_DIR)


def learn_and_capture(
    bible: dict[str, Any],
    stories: list[dict[str, Any]] | None = None,
) -> GenreCaptureResult | None:
    """Extract, validate, and capture a genre profile using the full CAPTURED pipeline.

    Unlike ``learn_from_bible``, this function runs the complete CAPTURED
    workflow: extract → build_evidence → semantic_review → replay_verify → save.

    Supports continuous evolution:
    - **NEW** genres (no existing profile) → ``"captured"``
    - **EXISTING** genres with higher confidence → ``"derived"``
    - **EXISTING** genres with same/lower confidence → ``"merged"``

    The ``should_evolve()`` check is applied before capture to ensure
    that evolution is warranted.

    Capture only proceeds if:
    - Confidence >= 0.6
    - Semantic review passes (no errors)

    Returns a ``GenreCaptureResult`` with the full capture details (including
    ``evolution_type`` and ``lineage``), or ``None`` if the capture was blocked
    by low confidence, failed semantic review, or should_evolve() returning False.
    """
    result = GenreEvolutionEngine.capture(bible, stories, GOLDEN_CASE_DIR)

    # Only capture if confidence threshold is met and semantic review passes
    if result.evidence.confidence < GenreEvolutionEngine.CONFIDENCE_THRESHOLD:
        return None
    if not result.validation.get("passed"):
        return None

    return result