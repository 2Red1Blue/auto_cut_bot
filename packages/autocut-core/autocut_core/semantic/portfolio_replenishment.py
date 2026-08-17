#!/usr/bin/env python3
"""Plan deterministic Reserve Story replenishment after Script rejection.

The original Story Portfolio remains immutable.  This module writes a separate
audit artifact whose individual promotion records can be bound into one fresh
Story Script request without invalidating already-successful sibling Scripts.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from autocut_core.io import (
    atomic_write_json,
    json_sha256,
    load_json,
    sha256_file,
    stable_id,
)
from autocut_core.schema.compat import validate_task_response


REPLENISHMENT_POLICY_VERSION = "story-script-reserve-replenishment-v1"
REPLENISHMENT_SCHEMA_VERSION = "1.0"
NON_TERMINAL_FAILURE_CLASSES = {
    "compile_only",
    "provider",
    "rate_limit",
    "transport",
}


def rank_score(story: dict[str, Any]) -> float:
    scores = story.get("scores", {})
    positive = sum(
        float(scores.get(field, 0))
        for field in (
            "story_completeness",
            "independent_clarity",
            "highlight_relevance",
            "source_sufficiency",
            "causal_clarity",
            "hook_alignment",
        )
    )
    background_cost = float(scores.get("background_cost", 10))
    duration_bonus = {
        "strong": 3.0,
        "viable": 1.5,
        "short": 0.0,
        "insufficient": -10.0,
    }.get(story.get("duration_feasibility"), -10.0)
    return positive - background_cost + duration_bonus


def replenishment_input_fingerprints(
    *,
    story_catalog_path: Path,
    story_portfolio_path: Path,
    series_bible_path: Path,
) -> dict[str, str]:
    return {
        "story_catalog_sha256": sha256_file(story_catalog_path),
        "story_portfolio_sha256": sha256_file(story_portfolio_path),
        "series_bible_sha256": sha256_file(series_bible_path),
    }


def is_terminal_script_rejection(value: Any) -> bool:
    """Only typed, formal Script rejections may open a production slot."""

    if not isinstance(value, dict):
        return False
    failure_codes = value.get("failure_codes")
    return bool(
        value.get("disposition") == "rejected"
        and value.get("feasibility_status") == "not_feasible"
        and value.get("repair_route") == "story_script"
        and isinstance(failure_codes, list)
        and failure_codes
        and all(isinstance(item, str) and item for item in failure_codes)
        and value.get("failure_class") not in NON_TERMINAL_FAILURE_CLASSES
    )


def _promotion_identity(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "policy_version": REPLENISHMENT_POLICY_VERSION,
        "sequence": record["sequence"],
        "story_id": record["story_id"],
        "production_slot": record["production_slot"],
        "replaces_story_id": record["replaces_story_id"],
        "root_primary_story_id": record["root_primary_story_id"],
        "trigger_failure_codes": record["trigger_failure_codes"],
        "missing_thread_beat_ids": record["missing_thread_beat_ids"],
        "recovered_thread_beat_ids": record["recovered_thread_beat_ids"],
    }


def _finalize_promotion(record: dict[str, Any]) -> dict[str, Any]:
    identity = _promotion_identity(record)
    return {
        **record,
        "promotion_id": stable_id("promotion", identity),
        "promotion_fingerprint": json_sha256(identity),
    }


def promotion_for_story(
    replenishment: dict[str, Any] | None,
    story_id: str,
) -> dict[str, Any] | None:
    if not isinstance(replenishment, dict):
        return None
    matches = [
        item
        for item in replenishment.get("promotions", [])
        if isinstance(item, dict) and item.get("story_id") == story_id
    ]
    if len(matches) > 1:
        raise ValueError(f"Reserve Story was promoted more than once: {story_id}")
    return matches[0] if matches else None


def accounted_story_ids(
    portfolio: dict[str, Any],
    replenishment: dict[str, Any] | None,
) -> set[str]:
    """Return every base Primary or promoted Reserve kept in the audit."""

    return {
        item
        for item in portfolio.get("primary_story_ids", [])
        if isinstance(item, str)
    } | {
        item.get("story_id")
        for item in (replenishment or {}).get("promotions", [])
        if isinstance(item, dict) and isinstance(item.get("story_id"), str)
    }


def effective_story_by_slot(
    portfolio: dict[str, Any],
    replenishment: dict[str, Any] | None,
) -> dict[int, str]:
    """Return the final occupant of every production slot.

    Replenishment is an ordered replacement chain.  A replaced Primary or
    earlier Reserve remains in the rejection audit, but it is no longer an
    active Script target once the next promotion owns its slot.
    """

    occupants = {
        item["slot"]: item["story_id"]
        for item in portfolio.get("production_slots", [])
        if isinstance(item, dict)
        and isinstance(item.get("story_id"), str)
        and isinstance(item.get("slot"), int)
    }
    for item in (replenishment or {}).get("promotions", []):
        if (
            isinstance(item, dict)
            and isinstance(item.get("story_id"), str)
            and isinstance(item.get("production_slot"), int)
        ):
            occupants[item["production_slot"]] = item["story_id"]
    return occupants


def effective_story_ids(
    portfolio: dict[str, Any],
    replenishment: dict[str, Any] | None,
) -> set[str]:
    return set(effective_story_by_slot(portfolio, replenishment).values())


def effective_slot_by_story(
    portfolio: dict[str, Any],
    replenishment: dict[str, Any] | None,
) -> dict[str, int]:
    return {
        story_id: slot
        for slot, story_id in effective_story_by_slot(
            portfolio, replenishment
        ).items()
    }


def portfolio_binding_for_story(
    *,
    portfolio: dict[str, Any],
    portfolio_sha256: str,
    replenishment: dict[str, Any] | None,
    story_id: str,
) -> dict[str, Any]:
    slots = effective_slot_by_story(portfolio, replenishment)
    if story_id not in slots:
        raise ValueError(f"Story has no effective production slot: {story_id}")
    binding: dict[str, Any] = {
        "portfolio_sha256": portfolio_sha256,
        "role": "primary",
        "production_slot": slots[story_id],
    }
    promotion = promotion_for_story(replenishment, story_id)
    if promotion is not None:
        binding.update(
            {
                "promotion_id": promotion["promotion_id"],
                "promotion_fingerprint": promotion[
                    "promotion_fingerprint"
                ],
                "replaces_story_id": promotion["replaces_story_id"],
                "root_primary_story_id": promotion[
                    "root_primary_story_id"
                ],
            }
        )
    return binding


def validate_replenishment(
    payload: dict[str, Any],
    *,
    catalog: dict[str, Any],
    portfolio: dict[str, Any],
    input_fingerprints: dict[str, str],
) -> list[str]:
    errors: list[str] = []
    if payload.get("schema_version") != REPLENISHMENT_SCHEMA_VERSION:
        errors.append("unsupported replenishment schema_version")
    if payload.get("policy_version") != REPLENISHMENT_POLICY_VERSION:
        errors.append("unsupported replenishment policy_version")
    if payload.get("input_fingerprints") != input_fingerprints:
        errors.append("replenishment input fingerprints are stale")
    if payload.get("status") not in {
        "promotions_ready",
        "stable",
        "exhausted",
    }:
        errors.append("invalid replenishment status")

    story_by_id = {
        item["story_id"]: item
        for item in catalog.get("stories", [])
        if isinstance(item, dict) and isinstance(item.get("story_id"), str)
    }
    catalog_ids = set(story_by_id)
    primary_ids = list(portfolio.get("primary_story_ids", []))
    reserve_ids = list(portfolio.get("reserve_story_ids", []))
    if payload.get("base_primary_story_ids") != primary_ids:
        errors.append("replenishment Primary Story list differs from Portfolio")
    if payload.get("reserve_story_ids") != reserve_ids:
        errors.append("replenishment Reserve Story list differs from Portfolio")
    slot_roots = {
        item.get("slot"): item.get("story_id")
        for item in portfolio.get("production_slots", [])
        if isinstance(item, dict)
    }
    promotions = payload.get("promotions", [])
    if not isinstance(promotions, list):
        return [*errors, "replenishment promotions must be a list"]
    seen: set[str] = set()
    last_occupant_by_slot = dict(slot_roots)
    for index, record in enumerate(promotions, start=1):
        where = f"replenishment.promotions[{index - 1}]"
        if not isinstance(record, dict):
            errors.append(f"{where} must be an object")
            continue
        story_id = record.get("story_id")
        if record.get("sequence") != index:
            errors.append(f"{where}.sequence must be contiguous")
        if story_id not in reserve_ids or story_id not in catalog_ids:
            errors.append(f"{where}.story_id is not a Portfolio Reserve")
        if story_id in seen:
            errors.append(f"{where}.story_id was promoted more than once")
        if isinstance(story_id, str):
            seen.add(story_id)
        slot = record.get("production_slot")
        if slot not in slot_roots:
            errors.append(f"{where}.production_slot is not a base slot")
        if record.get("root_primary_story_id") != slot_roots.get(slot):
            errors.append(f"{where}.root_primary_story_id does not own the slot")
        if record.get("replaces_story_id") != last_occupant_by_slot.get(slot):
            errors.append(f"{where}.replaces_story_id breaks the slot chain")
        for field in (
            "trigger_failure_codes",
            "missing_thread_beat_ids",
            "recovered_thread_beat_ids",
        ):
            value = record.get(field)
            if not isinstance(value, list) or not all(
                isinstance(item, str) and item for item in value
            ):
                errors.append(f"{where}.{field} must contain strings")
        recovered = set(record.get("recovered_thread_beat_ids", []))
        missing = set(record.get("missing_thread_beat_ids", []))
        story_beats = set(
            story_by_id.get(story_id, {}).get("source_thread_beat_ids", [])
        )
        if not recovered:
            errors.append(f"{where}.recovered_thread_beat_ids must not be empty")
        if not recovered <= missing:
            errors.append(f"{where} recovers a Beat that was not missing")
        if not recovered <= story_beats:
            errors.append(f"{where} recovers a Beat outside the Reserve Story")
        try:
            expected = _finalize_promotion(
                {
                    key: value
                    for key, value in record.items()
                    if key not in {"promotion_id", "promotion_fingerprint"}
                }
            )
        except (KeyError, TypeError):
            errors.append(f"{where} is missing promotion identity fields")
        else:
            if record.get("promotion_id") != expected["promotion_id"]:
                errors.append(f"{where}.promotion_id is invalid")
            if (
                record.get("promotion_fingerprint")
                != expected["promotion_fingerprint"]
            ):
                errors.append(f"{where}.promotion_fingerprint is invalid")
        if isinstance(story_id, str) and slot in slot_roots:
            last_occupant_by_slot[slot] = story_id
    return errors


def _coverage_summary(
    *,
    target_beats: set[str],
    covered_beats: set[str],
    active_story_ids: set[str],
) -> dict[str, Any]:
    return {
        "target_thread_beat_ids": sorted(target_beats),
        "covered_thread_beat_ids": sorted(covered_beats & target_beats),
        "missing_thread_beat_ids": sorted(target_beats - covered_beats),
        "active_story_ids": sorted(active_story_ids),
    }


def plan_replenishment(
    *,
    catalog: dict[str, Any],
    portfolio: dict[str, Any],
    series_bible: dict[str, Any],
    story_index: dict[str, Any],
    input_fingerprints: dict[str, str],
    existing: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Return the updated audit artifact and the one promotion to attempt."""

    catalog_errors = validate_task_response("story_catalog", catalog)
    portfolio_errors = validate_task_response("story_portfolio", portfolio)
    if catalog_errors:
        raise ValueError("invalid Story Catalog: " + "; ".join(catalog_errors[:20]))
    if portfolio_errors:
        raise ValueError(
            "invalid Story Portfolio: " + "; ".join(portfolio_errors[:20])
        )
    story_by_id = {
        item["story_id"]: item
        for item in catalog.get("stories", [])
        if isinstance(item, dict) and isinstance(item.get("story_id"), str)
    }
    primary_ids = list(portfolio.get("primary_story_ids", []))
    reserve_ids = list(portfolio.get("reserve_story_ids", []))
    payload = existing or {
        "schema_version": REPLENISHMENT_SCHEMA_VERSION,
        "policy_version": REPLENISHMENT_POLICY_VERSION,
        "input_fingerprints": input_fingerprints,
        "status": "stable",
        "base_primary_story_ids": primary_ids,
        "reserve_story_ids": reserve_ids,
        "promotions": [],
        "coverage_summary": {},
    }
    errors = validate_replenishment(
        payload,
        catalog=catalog,
        portfolio=portfolio,
        input_fingerprints=input_fingerprints,
    )
    if errors:
        raise ValueError("invalid Story Portfolio replenishment: " + "; ".join(errors))

    promotions = payload["promotions"]
    indexed_active_ids = {
        item.get("story_id")
        for item in story_index.get("stories", [])
        if isinstance(item, dict) and isinstance(item.get("story_id"), str)
    }
    active_ids = indexed_active_ids & effective_story_ids(portfolio, payload)
    rejection_by_id = {
        item["story_id"]: item
        for item in story_index.get("story_rejections", [])
        if isinstance(item, dict) and isinstance(item.get("story_id"), str)
    }
    accounted_ids = indexed_active_ids | set(rejection_by_id)
    pending = next(
        (
            item
            for item in promotions
            if item.get("story_id") not in accounted_ids
        ),
        None,
    )

    target_beats = {
        beat_id
        for story_id in primary_ids
        for beat_id in story_by_id.get(story_id, {}).get(
            "source_thread_beat_ids", []
        )
        if isinstance(beat_id, str)
    }
    covered_beats = {
        beat_id
        for story_id in active_ids
        for beat_id in story_by_id.get(story_id, {}).get(
            "source_thread_beat_ids", []
        )
        if isinstance(beat_id, str)
    }
    missing_beats = target_beats - covered_beats
    payload["coverage_summary"] = _coverage_summary(
        target_beats=target_beats,
        covered_beats=covered_beats,
        active_story_ids=active_ids,
    )
    if pending is not None:
        payload["status"] = "promotions_ready"
        return payload, pending

    base_slot_story = {
        item["slot"]: item["story_id"]
        for item in portfolio.get("production_slots", [])
        if isinstance(item, dict)
    }
    active_slots = {
        effective_slot_by_story(portfolio, payload).get(story_id)
        for story_id in active_ids
    }
    trigger_by_slot: dict[int, tuple[str, dict[str, Any]]] = {}
    for slot, root_story_id in base_slot_story.items():
        if slot in active_slots:
            continue
        occupants = [
            item["story_id"]
            for item in promotions
            if item.get("production_slot") == slot
        ]
        failed_story_id = occupants[-1] if occupants else root_story_id
        rejection = rejection_by_id.get(failed_story_id)
        if is_terminal_script_rejection(rejection):
            trigger_by_slot[slot] = (failed_story_id, rejection)

    if not missing_beats or not trigger_by_slot:
        payload["status"] = "stable"
        return payload, None

    thread_beat_by_id = {
        item["id"]: item
        for item in series_bible.get("thread_beats", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    required = {
        beat_id
        for beat_id, item in thread_beat_by_id.items()
        if item.get("importance") == "required"
    }
    payoff = {
        beat_id
        for beat_id, item in thread_beat_by_id.items()
        if item.get("phase") in {"turn", "reveal", "payoff", "consequence"}
    }
    non_coda = {
        beat_id
        for beat_id, item in thread_beat_by_id.items()
        if item.get("phase") != "coda"
    }
    attempted_reserves = {
        item.get("story_id")
        for item in promotions
        if isinstance(item, dict)
    }
    reserve_order = {story_id: index for index, story_id in enumerate(reserve_ids)}
    eligible = []
    for story_id in reserve_ids:
        if story_id in attempted_reserves:
            continue
        story_beats = set(
            story_by_id.get(story_id, {}).get("source_thread_beat_ids", [])
        )
        recovered = story_beats & missing_beats
        if not recovered:
            continue
        score = (
            len(recovered & required),
            len(recovered & payoff),
            len(recovered & non_coda),
            len(recovered),
            rank_score(story_by_id[story_id]),
            -reserve_order[story_id],
        )
        eligible.append((score, story_id, story_beats, recovered))
    if not eligible:
        payload["status"] = "exhausted"
        return payload, None

    _, story_id, story_beats, recovered = max(eligible)
    slot = max(
        trigger_by_slot,
        key=lambda item: (
            len(
                story_beats
                & set(
                    story_by_id.get(base_slot_story[item], {}).get(
                        "source_thread_beat_ids", []
                    )
                )
                & missing_beats
            ),
            -item,
        ),
    )
    replaces_story_id, rejection = trigger_by_slot[slot]
    record = _finalize_promotion(
        {
            "sequence": len(promotions) + 1,
            "story_id": story_id,
            "production_slot": slot,
            "replaces_story_id": replaces_story_id,
            "root_primary_story_id": base_slot_story[slot],
            "trigger_failure_codes": sorted(set(rejection["failure_codes"])),
            "missing_thread_beat_ids": sorted(missing_beats),
            "recovered_thread_beat_ids": sorted(recovered),
        }
    )
    promotions.append(record)
    payload["status"] = "promotions_ready"
    return payload, record


def load_validated_replenishment(
    path: Path,
    *,
    story_catalog_path: Path,
    story_portfolio_path: Path,
    series_bible_path: Path,
) -> dict[str, Any] | None:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        return None
    catalog = load_json(story_catalog_path)
    portfolio = load_json(story_portfolio_path)
    payload = load_json(resolved)
    errors = validate_replenishment(
        payload,
        catalog=catalog,
        portfolio=portfolio,
        input_fingerprints=replenishment_input_fingerprints(
            story_catalog_path=story_catalog_path,
            story_portfolio_path=story_portfolio_path,
            series_bible_path=series_bible_path,
        ),
    )
    if errors:
        raise ValueError("invalid Story Portfolio replenishment: " + "; ".join(errors))
    return payload


def plan_from_paths(
    *,
    story_catalog_path: Path,
    story_portfolio_path: Path,
    series_bible_path: Path,
    story_index_path: Path,
    output_path: Path,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    existing = load_json(output_path) if output_path.is_file() else None
    return plan_replenishment(
        catalog=load_json(story_catalog_path),
        portfolio=load_json(story_portfolio_path),
        series_bible=load_json(series_bible_path),
        story_index=load_json(story_index_path),
        input_fingerprints=replenishment_input_fingerprints(
            story_catalog_path=story_catalog_path,
            story_portfolio_path=story_portfolio_path,
            series_bible_path=series_bible_path,
        ),
        existing=existing,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--story-catalog", type=Path, required=True)
    parser.add_argument("--story-portfolio", type=Path, required=True)
    parser.add_argument("--series-bible", type=Path, required=True)
    parser.add_argument("--story-index", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload, promotion = plan_from_paths(
        story_catalog_path=args.story_catalog.expanduser().resolve(),
        story_portfolio_path=args.story_portfolio.expanduser().resolve(),
        series_bible_path=args.series_bible.expanduser().resolve(),
        story_index_path=args.story_index.expanduser().resolve(),
        output_path=args.output.expanduser().resolve(),
    )
    atomic_write_json(args.output, payload)
    if promotion is None:
        print(f"REPLENISHMENT\t{payload['status']}")
    else:
        print(
            "PROMOTION\t"
            f"{promotion['story_id']}\tslot={promotion['production_slot']}\t"
            f"id={promotion['promotion_id']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
