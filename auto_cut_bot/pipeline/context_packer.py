"""Context packer — Doc 23 Section 3.2 context assembly for story generation tasks.

Assembles a ContextPack from multiple artifacts (series bible, episode digests,
scene details, story catalog) with priority-based ordering and consumption mode
annotations.  The ContextPack is serialized to text for LLM consumption.

Consumption modes (from Doc 23 Artifact Contract):
  - full_in_context: entire artifact is included inline in the LLM prompt
  - query: consumer queries the artifact via DB/tool at runtime (not inlined)
  - deterministic_join: two-stage deterministic join, not LLM-mediated
  - human_only: artifact is for human review only, never sent to LLM
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .core.io import load_json


# ── Valid consumption modes ──────────────────────────────────────────────────────

_VALID_MODES = frozenset({"full_in_context", "query", "deterministic_join", "human_only"})

# ── Artifact -> expected consumption mode mapping (from Doc 23 contract) ─────────

_ARTIFACT_CONSUMPTION_CONTRACT: dict[str, str] = {
    "series-bible.json": "full_in_context",
    "story-catalog.json": "query",
    "episode_digests": "query",
    "source_script": "deterministic_join",
    "scene_details": "query",
    "character_registry": "query",
    "genre_profile": "full_in_context",
    "review_checklist": "human_only",
}


# ── ContextItem ──────────────────────────────────────────────────────────────────


@dataclass
class ContextItem:
    """A single item in a context pack.

    Attributes:
        content: The actual artifact data (dict, list, str, or any JSON-serializable).
        priority: 0=required (must be in context), 1=important, 2=supplemental.
        mode: Consumption mode — full_in_context, query, deterministic_join, human_only.
        source: Which artifact or source this item came from.
    """

    content: Any
    priority: int       # 0=required, 1=important, 2=supplemental
    mode: str           # full_in_context, query, deterministic_join, human_only
    source: str         # which artifact this came from

    def __post_init__(self) -> None:
        if self.mode not in _VALID_MODES:
            raise ValueError(
                f"Invalid consumption mode {self.mode!r}; must be one of "
                f"{sorted(_VALID_MODES)}"
            )
        if self.priority not in (0, 1, 2):
            raise ValueError(
                f"Invalid priority {self.priority!r}; must be 0, 1, or 2"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "priority": self.priority,
            "mode": self.mode,
            "source": self.source,
        }

    def char_count(self) -> int:
        """Character count of this item's content when serialized."""
        if isinstance(self.content, str):
            return len(self.content)
        if self.content is None:
            return 0
        return len(json.dumps(self.content, ensure_ascii=False, default=str))


# ── ContextPack ──────────────────────────────────────────────────────────────────


@dataclass
class ContextPack:
    """Assembled context for a generation task.

    Holds a list of ContextItems with priority-based ordering.  Serializes to
    text for LLM consumption via :meth:`to_text`.
    """

    items: list[ContextItem] = field(default_factory=list)

    def add(
        self,
        content: Any,
        priority: int = 0,
        mode: str = "full_in_context",
        source: str = "",
    ) -> None:
        """Add an item to the context pack.

        If content is None or empty, the item is silently skipped.
        """
        if content is None:
            return
        if isinstance(content, (list, dict, str)) and not content:
            return
        self.items.append(
            ContextItem(
                content=deepcopy(content) if isinstance(content, (dict, list)) else content,
                priority=priority,
                mode=mode,
                source=source,
            )
        )

    def to_text(self) -> str:
        """Serialize context pack to text for LLM consumption.

        Items are sorted by priority (0 first), then rendered with headers
        indicating source and mode.  ``human_only`` items are excluded from
        the LLM text (they are for human review only).
        """
        sorted_items = sorted(self.items, key=lambda item: item.priority)
        parts: list[str] = []
        for item in sorted_items:
            if item.mode == "human_only":
                continue
            header = f"## [{item.source}] (priority={item.priority}, mode={item.mode})"
            if isinstance(item.content, str):
                body = item.content
            elif isinstance(item.content, (dict, list)):
                body = json.dumps(item.content, ensure_ascii=False, indent=2)
            else:
                body = str(item.content)
            parts.append(f"{header}\n{body}")
        return "\n\n".join(parts)

    def total_chars(self) -> int:
        """Total character count of all items (including human_only)."""
        return sum(item.char_count() for item in self.items)

    def items_by_priority(self, priority: int) -> list[ContextItem]:
        """Return all items with the given priority level."""
        return [item for item in self.items if item.priority == priority]

    def required_items(self) -> list[ContextItem]:
        """Return priority-0 (required) items."""
        return self.items_by_priority(0)

    def to_dict(self) -> dict[str, Any]:
        return {"items": [item.to_dict() for item in self.items]}


# ── Context assembly ─────────────────────────────────────────────────────────────


def pack_context(
    task: dict[str, Any],
    *,
    artifacts_dir: str | Path | None = None,
) -> ContextPack:
    """Assemble context for a story generation task.

    Priority-based assembly:
      - Priority 0 (required): series_bible, task constraints
      - Priority 1 (important): episode_digests for target scope, scene details
      - Priority 2 (supplemental): catalog summaries

    Args:
        task: Task dict with keys: ``scope``, ``constraints``, ``book_id``,
              ``artifacts`` (optional override paths).
        artifacts_dir: Base directory for artifact files.  Defaults to
                       ``{task[book_id]}/artifacts/`` relative to cwd.

    Returns:
        ContextPack ready for LLM consumption.
    """
    pack = ContextPack()

    base = Path(artifacts_dir) if artifacts_dir else Path(f"{task['book_id']}/artifacts")

    # Priority 0: series bible and task constraints (required)
    bible_path = task.get("artifacts", {}).get("series_bible", base / "series-bible.json")
    bible = _try_load(bible_path)
    if bible is not None:
        pack.add(bible, priority=0, mode="full_in_context", source="series_bible")

    if task.get("constraints"):
        pack.add(task["constraints"], priority=0, mode="full_in_context", source="task")

    # Priority 1: scope-specific episode digests and scene details
    scope = task.get("scope")
    if scope:
        digests = _query_episode_digests(task, scope, base)
        if digests:
            pack.add(digests, priority=1, mode="query", source="episode_digests")

        scene_details = _query_scene_details(task, scope, base)
        if scene_details:
            pack.add(scene_details, priority=1, mode="query", source="source_script")

    # Priority 2: catalog summaries (supplemental)
    catalog_path = task.get("artifacts", {}).get("story_catalog", base / "story-catalog.json")
    catalog = _try_load(catalog_path)
    if catalog is not None:
        pack.add(catalog, priority=2, mode="query", source="story_catalog")

    return pack


# ── Consumption mode validation ──────────────────────────────────────────────────


def validate_consumption_contract(
    artifact_name: str,
    consumer: str,
    mode: str,
) -> bool:
    """Check that a consumer is using the declared consumption mode.

    Validates against the known artifact consumption contract.  Unknown
    artifacts are treated as valid (pass-through) to allow new artifacts
    without updating the registry.

    Args:
        artifact_name: Name of the artifact (e.g. ``"series-bible.json"``).
        consumer: Name of the consumer (e.g. ``"story_script_draft"``).
        mode: Actual consumption mode being used.

    Returns:
        True if the mode matches the contract or the artifact is unregistered.
    """
    if artifact_name not in _ARTIFACT_CONSUMPTION_CONTRACT:
        # Unknown artifact — allow but log a warning for future registration
        return True
    expected = _ARTIFACT_CONSUMPTION_CONTRACT[artifact_name]
    return mode == expected


def get_consumption_mode(artifact_name: str) -> str | None:
    """Return the expected consumption mode for a known artifact.

    Returns None if the artifact is not registered in the contract.
    """
    return _ARTIFACT_CONSUMPTION_CONTRACT.get(artifact_name)


# ── Internal helpers ─────────────────────────────────────────────────────────────


def _try_load(path: str | Path) -> Any | None:
    """Try to load a JSON artifact, returning None if not found or invalid."""
    try:
        return load_json(Path(path))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _query_episode_digests(
    task: dict[str, Any],
    scope: dict[str, Any],
    base: Path,
) -> list[dict[str, Any]] | None:
    """Load episode digests for the target scope from disk.

    Falls back to loading the episode_digests.jsonl artifact if no scope-driven
    query is available.
    """
    digest_path = task.get("artifacts", {}).get("episode_digests", base / "episode_digests.jsonl")
    try:
        resolved = Path(digest_path)
        if not resolved.is_file():
            return None
        # Scope filtering: if scope specifies episode_range, only return matching digests
        ep_range = scope.get("episode_range")
        if ep_range and isinstance(ep_range, (list, tuple)) and len(ep_range) == 2:
            lo, hi = ep_range
            # Load as JSONL lines
            lines = resolved.read_text(encoding="utf-8").strip().splitlines()
            matching: list[dict[str, Any]] = []
            for line in lines:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ep_id = record.get("episode_id")
                if ep_id is not None and lo <= int(ep_id) <= hi:
                    matching.append(record)
            return matching if matching else None
        # No scope filter — load all
        return [json.loads(line) for line in
                resolved.read_text(encoding="utf-8").strip().splitlines()
                if line.strip()]
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _query_scene_details(
    task: dict[str, Any],
    scope: dict[str, Any],
    base: Path,
) -> list[dict[str, Any]] | None:
    """Load scene details for the target scope from disk.

    Reads from source_script.json or scene_details.jsonl.
    """
    scene_path = task.get("artifacts", {}).get("scene_details", base / "scene_details.jsonl")
    source_path = task.get("artifacts", {}).get("source_script", base / "source_script.json")
    resolved = Path(scene_path) if Path(scene_path).is_file() else Path(source_path)
    try:
        if not resolved.is_file():
            return None
        if resolved.suffix == ".jsonl":
            return [json.loads(line) for line in
                    resolved.read_text(encoding="utf-8").strip().splitlines()
                    if line.strip()]
        data = load_json(resolved)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return [data]
        return None
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None