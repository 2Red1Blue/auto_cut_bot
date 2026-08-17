"""Genre registry — discovers genre profiles from _references/ editorial knowledge files.

Each genre is backed by one or more JSON golden-case files in
``_references/editorial-knowledge/``.  Adding a new genre is a data-only
operation: drop a JSON file with ``genre_profile`` and ``case_id`` and the
registry discovers it at startup.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class GenreAdapter:
    """Immutable adapter for a single genre profile.

    Attributes:
        genre_profile: The genre key (e.g. ``"female_romance"``).
        primary_focus:  Primary thread focus (e.g. ``"男女主关系发展"``).
        support_roles:  Roles that support the primary thread.
        golden_case_ids: Case IDs backing this genre.
        status: ``"ready"`` for file-backed adapters, ``"human_review_required"``
                for ``project_specific`` and ``generic``.
    """

    genre_profile: str
    primary_focus: str
    support_roles: list[str]
    golden_case_ids: list[str]
    status: str = "ready"


@dataclass(frozen=True)
class ResourceRef:
    """A typed reference to a specific artifact used in evidence extraction.

    Attributes:
        type: Resource type (e.g. ``"bible"``, ``"script"``, ``"plan"``).
        case_id: Identifier for the resource.
        path: Optional filesystem path to the resource.
    """

    type: str
    case_id: str
    path: str | None = None


# Sentinel: returned when a Bible specifies "project_specific".
PROJECT_SPECIFIC_ADAPTER = GenreAdapter(
    genre_profile="project_specific",
    primary_focus="由本剧 Bible 与中心承诺确定",
    support_roles=[],
    golden_case_ids=[],
    status="human_review_required",
)

# Sentinel: returned for unknown genres (behaves identically to
# the old UNKNOWN_PROFILE sentinel, but is a structured adapter).
GENERIC_ADAPTER = GenreAdapter(
    genre_profile="generic",
    primary_focus="由本剧 Bible 与中心承诺确定",
    support_roles=[],
    golden_case_ids=[],
    status="human_review_required",
)


class GenreRegistry:
    """Filesystem-driven genre registry.

    Discovers genre profiles from ``_references/editorial-knowledge/*.json``
    (primary) and ``_references/editorial-golden-case-arya.json``.

    Each JSON file is self-describing: ``genre_profile`` and ``case_id`` are
    required; ``transferable_contract.primary_thread`` and
    ``transferable_contract.support_roles`` supply the adapter's focus and roles.

    Usage::

        registry = GenreRegistry.build(editorial_knowledge_dir)
        adapter = registry.get("female_romance")  # => GenreAdapter or GENERIC_ADAPTER
    """

    def __init__(self, adapters: dict[str, GenreAdapter]) -> None:
        self._adapters = adapters  # genre_profile -> adapter
        self._case_id_to_genre: dict[str, str] = {}
        for adapter in adapters.values():
            for case_id in adapter.golden_case_ids:
                self._case_id_to_genre[case_id] = adapter.genre_profile

    def get(self, genre_profile: str) -> GenreAdapter:
        """Return the adapter for *genre_profile*, or ``GENERIC_ADAPTER``."""
        return self._adapters.get(genre_profile, GENERIC_ADAPTER)

    def genre_for_case(self, case_id: str) -> str | None:
        """Return the genre_profile that owns *case_id*, or None."""
        return self._case_id_to_genre.get(case_id)

    def known_genres(self) -> set[str]:
        """Return the set of known genre profile names."""
        return set(self._adapters.keys())

    def known_case_ids(self) -> set[str]:
        """Return the set of known golden case IDs."""
        return set(self._case_id_to_genre.keys())

    @classmethod
    def build(
        cls,
        editorial_knowledge_dir: Path,
        *,
        legacy_arya_path: Path | None = None,
    ) -> "GenreRegistry":
        """Build the registry from the filesystem.

        Args:
            editorial_knowledge_dir: Directory containing ``*.json`` golden cases.
            legacy_arya_path: Optional path to the legacy Arya golden case.
        """
        adapters: dict[str, GenreAdapter] = {}
        _load_json_cases(editorial_knowledge_dir, adapters)

        # Legacy Arya case — discover its genre if not already known.
        if legacy_arya_path is not None and legacy_arya_path.is_file():
            _load_arya_case(legacy_arya_path, adapters)

        # Always include the two sentinels.
        adapters[PROJECT_SPECIFIC_ADAPTER.genre_profile] = PROJECT_SPECIFIC_ADAPTER
        # Do not overwrite a real genre that happens to be named "generic".
        if GENERIC_ADAPTER.genre_profile not in adapters:
            adapters[GENERIC_ADAPTER.genre_profile] = GENERIC_ADAPTER

        return cls(adapters)


def _load_json_cases(
    directory: Path,
    adapters: dict[str, GenreAdapter],
) -> None:
    """Scan *directory* for ``*.json`` files and populate *adapters*."""
    for path in sorted(directory.glob("*.json")):
        try:
            with path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
        except (OSError, json.JSONDecodeError):
            continue

        if not isinstance(value, dict):
            continue

        genre_profile = value.get("genre_profile")
        case_id = value.get("case_id")
        if not isinstance(genre_profile, str) or not isinstance(case_id, str):
            continue

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

        existing = adapters.get(genre_profile)
        if existing is not None:
            # Merge: append case_id and merge support_roles.
            merged_ids = list(dict.fromkeys([*existing.golden_case_ids, case_id]))
            merged_roles = list(dict.fromkeys([*existing.support_roles, *support_roles]))
            adapters[genre_profile] = GenreAdapter(
                genre_profile=genre_profile,
                primary_focus=existing.primary_focus or primary_focus,
                support_roles=merged_roles,
                golden_case_ids=merged_ids,
                status="ready",
            )
        else:
            adapters[genre_profile] = GenreAdapter(
                genre_profile=genre_profile,
                primary_focus=primary_focus,
                support_roles=support_roles,
                golden_case_ids=[case_id],
                status="ready",
            )


def _load_arya_case(
    path: Path,
    adapters: dict[str, GenreAdapter],
) -> None:
    """Load the Arya golden case and register its genre if not already present."""
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return

    if not isinstance(value, dict):
        return

    genre_profile = value.get("genre_profile")
    case_id = value.get("case_id")
    if not isinstance(genre_profile, str) or not isinstance(case_id, str):
        return

    # Only register if this genre isn't already known from the primary directory.
    if genre_profile in adapters:
        return

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

    adapters[genre_profile] = GenreAdapter(
        genre_profile=genre_profile,
        primary_focus=primary_focus,
        support_roles=support_roles,
        golden_case_ids=[case_id],
        status="ready",
    )