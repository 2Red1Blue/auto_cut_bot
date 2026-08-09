"""Interactive approval — CLI-based item-by-item accept/reject for human nodes.

Provides the InteractiveApproval class that handles per-item accept/reject
decisions via CLI input, and saves structured decision artifacts to the
ArtifactBus.  Auto mode is not affected — this module is only invoked
when the pipeline runs in interactive mode and encounters a human node.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from autocut_core.io import utc_now
from autocut_core.logging import get_logger

logger = get_logger(__name__)


def _apply_batch(
    items: list[dict[str, Any]],
    start_idx: int,
    decision: str,
    reason: str,
) -> list[dict[str, Any]]:
    """Apply a batch decision to remaining items from start_idx."""
    results: list[dict[str, Any]] = []
    for j in range(start_idx, len(items)):
        remaining_id = items[j].get("id", str(j))
        results.append(_decision(remaining_id, decision, reason))
    return results


def _print_header(stage_name: str, total: int) -> None:
    """Print the interactive approval header."""
    print(f"\n{'=' * 60}")
    print(f"  Interactive Approval: {stage_name}")
    print(f"  {total} item(s) to review")
    print(f"{'=' * 60}")
    print("  [Y] accept  [n] reject  [a] accept all  [r] reject all  [q] quit")
    print()


def _print_summary(decisions: list[dict[str, Any]]) -> None:
    """Print the approval summary."""
    accepted = sum(1 for d in decisions if d["decision"] == "accepted")
    rejected = sum(1 for d in decisions if d["decision"] == "rejected")
    print(f"\n  Summary: {accepted} accepted, {rejected} rejected\n")


class InteractiveApproval:
    """Handles interactive item-by-item approval for human nodes.

    Presents items to the user via CLI, collects accept/reject decisions,
    and saves structured decision artifacts to the ArtifactBus.
    """

    def __init__(self, job_root: Path, stage_name: str) -> None:
        self.job_root = job_root
        self.stage_name = stage_name

    # ── public API ──────────────────────────────────────────────────────

    def present_and_collect(
        self, items: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Present items to the user and collect accept/reject decisions.

        Each item should have at least an ``id`` key.  The user can:
          - Accept (Y / Enter): accept this item
          - Reject (n): reject this item
          - Accept all (a): accept this and all remaining items
          - Reject all (r): reject this and all remaining items
          - Quit (q): reject remaining items and stop

        Returns a list of decision records, each with:
          ``item_id``, ``decision`` ("accepted" | "rejected"),
          ``reason``, ``timestamp``.
        """
        decisions: list[dict[str, Any]] = []
        total = len(items)
        _print_header(self.stage_name, total)

        for i, item in enumerate(items):
            item_id = item.get("id", str(i))
            summary = item.get("summary", "N/A")
            print(f"  [{i + 1}/{total}] {item_id}")
            print(f"        {summary}")

            response = input("  Accept? [Y/n/a/r/q]: ").strip().lower()

            if response == "q":
                print("  Quit — remaining items will be rejected.")
                decisions.extend(_apply_batch(items, i, "rejected", "quit"))
                break
            elif response == "a":
                print("  Accepting all remaining items.")
                decisions.extend(_apply_batch(items, i, "accepted", "accept all"))
                break
            elif response == "r":
                print("  Rejecting all remaining items.")
                decisions.extend(_apply_batch(items, i, "rejected", "reject all"))
                break
            elif response in ("", "y"):
                decisions.append(
                    _decision(item_id, "accepted", "manual accept")
                )
            else:  # "n" or anything else — reject
                decisions.append(
                    _decision(item_id, "rejected", "manual reject")
                )

        _print_summary(decisions)
        return decisions

    def save_artifact(
        self, bus: Any, decisions: list[dict[str, Any]]
    ) -> Any:
        """Save approval decisions as a structured artifact in the bus.

        The artifact is named ``{stage_name}_approval`` and contains
        the stage name, decisions list, and approval timestamp.
        """
        artifact_name = f"{self.stage_name}_approval"
        payload: dict[str, Any] = {
            "stage": self.stage_name,
            "decisions": decisions,
            "approved_at": utc_now(),
        }
        return bus.put(artifact_name, payload, stage=self.stage_name)


# ── helpers ────────────────────────────────────────────────────────────


def _decision(item_id: str, decision: str, reason: str) -> dict[str, Any]:
    return {
        "item_id": item_id,
        "decision": decision,
        "reason": reason,
        "timestamp": utc_now(),
    }


def extract_items_from_input(
    data: Any, stage_name: str
) -> list[dict[str, Any]]:
    """Extract reviewable items from stage input data.

    Heuristic: looks for a list of dicts (each with an ``id``-like key)
    in the top-level data or under common keys like ``stories``, ``plans``,
    ``items``, ``entries``, ``reports``.
    """
    if isinstance(data, list):
        candidates = data
    elif isinstance(data, dict):
        for key in ("stories", "plans", "items", "entries", "reports"):
            val = data.get(key)
            if isinstance(val, list):
                candidates = val
                break
        else:
            # fallback: wrap the whole dict as a single item
            return [_dict_item(data)]
    else:
        return []

    items: list[dict[str, Any]] = []
    for entry in candidates:
        if not isinstance(entry, dict):
            continue
        item = _dict_item(entry)
        items.append(item)
    return items


def _dict_item(entry: dict[str, Any]) -> dict[str, Any]:
    """Build a reviewable item dict from a raw dict entry."""
    item_id = entry.get("story_id") or entry.get("id") or entry.get("name", "")
    if not item_id:
        item_id = entry.get("stage", "unknown")
    summary_parts = []
    for key in ("title", "status", "feasibility", "summary"):
        val = entry.get(key)
        if val is not None:
            summary_parts.append(f"{key}={val}")
    return {
        "id": str(item_id),
        "summary": ", ".join(summary_parts) if summary_parts else "N/A",
    }
