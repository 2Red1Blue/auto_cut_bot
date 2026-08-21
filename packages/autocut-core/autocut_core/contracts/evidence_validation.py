"""Validate Story Evidence Packets against current approved inputs."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from autocut_core.io import atomic_write_json, load_json, load_jsonl, sha256_file
from autocut_core.libs.evidence_builder import (
    event_range_refs,
    filter_expanded_events,
    source_snapshot,
)
from autocut_core.schema.compat import validate_task_response


def selected_manifest_path(job_root: Path, canonical_name: str, legacy_name: str) -> Path:
    """Return the one deterministic manifest path used by all consumers.

    The underscore spelling is canonical.  The hyphen spelling is a backwards
    compatible alias, not an invitation to silently proceed without a
    manifest.  Keeping this resolution here prevents the stage and the
    validator from fingerprinting different files for the same job.
    """
    canonical_path = job_root / canonical_name
    if canonical_path.is_file():
        return canonical_path
    legacy_path = job_root / legacy_name
    if legacy_path.is_file():
        return legacy_path
    raise FileNotFoundError(f"missing manifest: expected {canonical_path} or {legacy_path}")


def indexed(
    records: list[dict[str, Any]], *, field: str, where: str, errors: list[str]
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(records):
        value = item.get(field)
        if not isinstance(value, str) or not value:
            errors.append(f"{where}[{index}].{field} must be non-empty")
        elif value in result:
            errors.append(f"{where} contains duplicate {field}: {value}")
        else:
            result[value] = item
    return result


def check_subset(values: Any, known: set[str], where: str, errors: list[str]) -> set[str]:
    if not isinstance(values, list):
        errors.append(f"{where} must be an array")
        return set()
    selected = {item for item in values if isinstance(item, str) and item}
    unknown = sorted(selected - known)
    if unknown:
        errors.append(f"{where} contains unknown IDs: {unknown}")
    return selected


def validate(job_root: Path, *, evidence_dir: Path | None = None) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    try:
        source_manifest_path = selected_manifest_path(
            job_root, "source_manifest.json", "source-manifest.json"
        )
    except FileNotFoundError:
        source_manifest_path = job_root / "source_manifest.json"
    try:
        window_manifest_path = selected_manifest_path(
            job_root, "window_manifest.json", "window-manifest.json"
        )
    except FileNotFoundError:
        window_manifest_path = job_root / "window_manifest.json"
    published_evidence_dir = job_root / "story-evidence"
    active_evidence_dir = evidence_dir or published_evidence_dir
    paths = {
        "index": active_evidence_dir / "index.json",
        "approval": job_root / "story-approval.json",
        "portfolio": job_root / "story-portfolio.json",
        "bible": job_root / "series-bible.json",
        "events": job_root / "event-cards.jsonl",
        "candidates": job_root / "highlight-hook-catalog.json",
        "sources": source_manifest_path,
        "window_manifest": window_manifest_path,
        "window_summaries": job_root / "window-summaries.jsonl",
    }
    for label, path in paths.items():
        if not path.is_file():
            errors.append(f"missing {label}: {path}")
    if errors:
        return {"ok": False, "errors": errors, "warnings": warnings}
    index = load_json(paths["index"])
    errors.extend(
        f"story_evidence.index: {item}"
        for item in validate_task_response("story_evidence_index", index)
    )
    approval = load_json(paths["approval"])
    portfolio = load_json(paths["portfolio"])
    if not isinstance(portfolio, dict):
        errors.append("Story Portfolio must be an object")
    bible = load_json(paths["bible"])
    global_events = indexed(
        load_jsonl(paths["events"]),
        field="id",
        where="global_events",
        errors=errors,
    )
    candidate_payload = load_json(paths["candidates"])
    global_candidates = indexed(
        candidate_payload.get("candidates", []),
        field="id",
        where="global_candidates",
        errors=errors,
    )
    source_payload = load_json(paths["sources"])
    global_sources = indexed(
        source_payload.get("sources", []),
        field="id",
        where="global_sources",
        errors=errors,
    )
    manifest_payload = load_json(paths["window_manifest"])
    global_manifest_windows = indexed(
        manifest_payload.get("windows", []),
        field="id",
        where="global_manifest_windows",
        errors=errors,
    )
    global_windows = indexed(
        load_jsonl(paths["window_summaries"]),
        field="window_id",
        where="global_window_summaries",
        errors=errors,
    )
    global_characters = indexed(
        bible.get("characters", []),
        field="id",
        where="global_characters",
        errors=errors,
    )
    global_relationships = indexed(
        bible.get("relationships", []),
        field="id",
        where="global_relationships",
        errors=errors,
    )
    global_facts = indexed(
        bible.get("facts", []),
        field="id",
        where="global_facts",
        errors=errors,
    )
    global_threads = indexed(
        bible.get("story_threads", []),
        field="id",
        where="global_story_threads",
        errors=errors,
    )
    global_thread_beats = indexed(
        bible.get("thread_beats", []),
        field="id",
        where="global_thread_beats",
        errors=errors,
    )
    global_questions = indexed(
        bible.get("open_questions", []),
        field="id",
        where="global_open_questions",
        errors=errors,
    )
    current_fingerprints = {
        "story_approval_sha256": sha256_file(paths["approval"]),
        "series_bible_sha256": sha256_file(paths["bible"]),
        "event_cards_sha256": sha256_file(paths["events"]),
        "candidate_catalog_sha256": sha256_file(paths["candidates"]),
        "source_manifest_sha256": sha256_file(paths["sources"]),
        "window_manifest_sha256": sha256_file(paths["window_manifest"]),
        "window_summaries_sha256": sha256_file(paths["window_summaries"]),
    }
    current_portfolio_sha256 = sha256_file(paths["portfolio"])
    if index.get("story_approval_sha256") != current_fingerprints["story_approval_sha256"]:
        errors.append("Story Evidence Index approval SHA-256 is stale")
    if index.get("portfolio_sha256") != current_portfolio_sha256:
        errors.append("Story Evidence Index Portfolio SHA-256 is stale")
    if not isinstance(approval, dict):
        errors.append("Story Approval must be an object")
        return {"ok": False, "errors": errors, "warnings": warnings}
    stories = approval.get("stories")
    if not isinstance(stories, list):
        errors.append("Story Approval stories must be an array")
        return {"ok": False, "errors": errors, "warnings": warnings}
    approved_items: dict[str, dict[str, Any]] = {}
    story_ids: set[str] = set()
    for item_index, item in enumerate(stories):
        where = f"Story Approval stories[{item_index}]"
        if not isinstance(item, dict):
            errors.append(f"{where} must be an object")
            continue
        story_id = item.get("story_id")
        if not isinstance(story_id, str) or not story_id:
            errors.append(f"{where}.story_id must be non-empty")
            continue
        if story_id in story_ids:
            errors.append(f"Story Approval contains duplicate story_id: {story_id}")
            continue
        story_ids.add(story_id)
        if item.get("decision") == "approved":
            approved_items[story_id] = item
    if approval.get("fulfillment_status") != "ready":
        errors.append("Story Approval fulfillment_status is not ready")
    selected_story_ids = approval.get("selected_story_ids")
    if not isinstance(selected_story_ids, list) or not all(
        isinstance(story_id, str) and story_id for story_id in selected_story_ids
    ):
        errors.append("Story Approval selected_story_ids must be non-empty strings")
        selected_story_id_set: set[str] = set()
    else:
        selected_story_id_set = set(selected_story_ids)
        if len(selected_story_id_set) != len(selected_story_ids):
            errors.append("Story Approval selected_story_ids contains duplicates")
    if not selected_story_id_set:
        errors.append("Story Approval selects no Stories")
    if selected_story_id_set != set(approved_items):
        errors.append("Story Approval selected_story_ids is stale")
    index_packets = index.get("packets", [])
    packet_entries = indexed(
        index_packets,
        field="story_id",
        where="story_evidence.index.packets",
        errors=errors,
    )
    if set(packet_entries) != set(approved_items):
        errors.append(
            "Story Evidence Packet selection differs from approved Stories: "
            f"missing={sorted(set(approved_items) - set(packet_entries))}, "
            f"extra={sorted(set(packet_entries) - set(approved_items))}"
        )
    if index.get("selected_story_count") != len(packet_entries):
        errors.append("Story Evidence Index selected_story_count is inconsistent")
    for story_id, entry in packet_entries.items():
        path_value = entry.get("path")
        if not isinstance(path_value, str):
            errors.append(f"{story_id}: packet path must be a string")
            continue
        declared_packet_path = Path(path_value).expanduser().resolve()
        packet_path = declared_packet_path
        if evidence_dir is not None and declared_packet_path.parent == published_evidence_dir:
            packet_path = active_evidence_dir / declared_packet_path.name
        if not packet_path.is_file():
            errors.append(f"{story_id}: missing packet file {packet_path}")
            continue
        if entry.get("packet_sha256") != sha256_file(packet_path):
            errors.append(f"{story_id}: packet SHA-256 is stale")
        packet = load_json(packet_path)
        errors.extend(
            f"{story_id}.packet: {item}"
            for item in validate_task_response("story_evidence_packet", packet)
        )
        if packet.get("story_id") != story_id:
            errors.append(f"{story_id}: packet story identity mismatch")
        if packet.get("title") != entry.get("title"):
            errors.append(f"{story_id}: packet title differs from index")
        if packet.get("production_slot") != entry.get("production_slot"):
            errors.append(f"{story_id}: packet production slot differs from index")
        if packet.get("status") != entry.get("status"):
            errors.append(f"{story_id}: packet status differs from index")
        cross_report_path_value = entry.get("cross_unit_context_report_path")
        if isinstance(cross_report_path_value, str):
            cross_report_path = Path(cross_report_path_value).expanduser().resolve()
            if not cross_report_path.is_file():
                errors.append(f"{story_id}: missing cross-unit context report")
            else:
                cross_report = load_json(cross_report_path)
                errors.extend(
                    f"{story_id}.cross_unit_context_report: {item}"
                    for item in validate_task_response(
                        "story_cross_unit_context_report", cross_report
                    )
                )
                if packet.get("cross_unit_context_report") != cross_report:
                    errors.append(
                        f"{story_id}: packet cross-unit context report differs from report file"
                    )
        approval_item = approved_items.get(story_id)
        if not isinstance(approval_item, dict):
            continue
        script_path_value = approval_item.get("script_path")
        if not isinstance(script_path_value, str):
            errors.append(f"{story_id}: approved Story lacks script_path")
            continue
        script_path = Path(script_path_value).expanduser().resolve()
        if not script_path.is_file():
            errors.append(f"{story_id}: approved Story Script is missing")
            continue
        current_script_sha256 = sha256_file(script_path)
        script = load_json(script_path)
        script_beats = {
            item.get("id"): item
            for item in script.get("beats", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        if entry.get("story_script_sha256") != current_script_sha256:
            errors.append(f"{story_id}: index Story Script SHA-256 is stale")
        binding = packet.get("approval_binding", {})
        if binding.get("story_script_sha256") != current_script_sha256:
            errors.append(f"{story_id}: packet Story Script SHA-256 is stale")
        if binding.get("story_script_sha256") != approval_item.get("approved_script_sha256"):
            errors.append(f"{story_id}: packet differs from approved Script SHA-256")
        if binding.get("portfolio_sha256") != current_portfolio_sha256:
            errors.append(f"{story_id}: packet Portfolio SHA-256 is stale")
        if approval_item.get("portfolio_sha256") != current_portfolio_sha256:
            errors.append(f"{story_id}: approved Story Portfolio SHA-256 is stale")
        script_portfolio = script.get("portfolio")
        if (
            not isinstance(script_portfolio, dict)
            or script_portfolio.get("portfolio_sha256") != current_portfolio_sha256
        ):
            errors.append(f"{story_id}: approved Story Script Portfolio binding is stale")
        if packet.get("input_fingerprints") != current_fingerprints:
            errors.append(f"{story_id}: packet input fingerprints are stale")
        scope_policy = script.get("scope_policy", {})
        cross_report = packet.get("cross_unit_context_report", {})
        if (
            isinstance(scope_policy, dict)
            and scope_policy.get("story_scope_policy") == "series_global"
            and script.get("edit_mode") == "montage"
            and cross_report.get("status") not in {"covered"}
        ):
            errors.append(
                f"{story_id}: montage requires a covered cross-unit causal context report"
            )
        if (
            isinstance(scope_policy, dict)
            and scope_policy.get("story_scope_policy") == "series_global"
            and scope_policy.get("analysis_unit_policy") != "processing_only"
        ):
            errors.append(
                f"{story_id}: analysis units must be processing partitions, not story scope"
            )
        catalog = packet.get("evidence_catalog", {})
        packet_sources = indexed(
            catalog.get("sources", []),
            field="id",
            where=f"{story_id}.sources",
            errors=errors,
        )
        packet_windows = indexed(
            catalog.get("windows", []),
            field="window_id",
            where=f"{story_id}.windows",
            errors=errors,
        )
        packet_events = indexed(
            catalog.get("events", []),
            field="id",
            where=f"{story_id}.events",
            errors=errors,
        )
        packet_candidates = indexed(
            catalog.get("candidates", []),
            field="id",
            where=f"{story_id}.candidates",
            errors=errors,
        )
        packet_characters = indexed(
            catalog.get("characters", []),
            field="id",
            where=f"{story_id}.characters",
            errors=errors,
        )
        packet_relationships = indexed(
            catalog.get("relationships", []),
            field="id",
            where=f"{story_id}.relationships",
            errors=errors,
        )
        packet_facts = indexed(
            catalog.get("facts", []),
            field="id",
            where=f"{story_id}.facts",
            errors=errors,
        )
        packet_threads = indexed(
            catalog.get("story_threads", []),
            field="id",
            where=f"{story_id}.story_threads",
            errors=errors,
        )
        packet_thread_beats = indexed(
            catalog.get("thread_beats", []),
            field="id",
            where=f"{story_id}.thread_beats",
            errors=errors,
        )
        packet_questions = indexed(
            catalog.get("open_questions", []),
            field="id",
            where=f"{story_id}.open_questions",
            errors=errors,
        )
        exact_catalogs = (
            ("events", packet_events, global_events),
            ("candidates", packet_candidates, global_candidates),
            ("windows", packet_windows, global_windows),
            ("characters", packet_characters, global_characters),
            ("relationships", packet_relationships, global_relationships),
            ("facts", packet_facts, global_facts),
            ("story_threads", packet_threads, global_threads),
            ("thread_beats", packet_thread_beats, global_thread_beats),
            ("open_questions", packet_questions, global_questions),
        )
        for label, selected, global_records in exact_catalogs:
            for item_id, item in selected.items():
                if global_records.get(item_id) != item:
                    errors.append(f"{story_id}: {label}[{item_id}] differs from source artifact")
        for source_id, snapshot in packet_sources.items():
            source = global_sources.get(source_id)
            if source is None:
                errors.append(f"{story_id}: unknown source snapshot {source_id}")
            elif source_snapshot(source) != snapshot:
                errors.append(f"{story_id}: source snapshot {source_id} differs from manifest")
        beat_ids: set[str] = set()
        for beat_index, beat in enumerate(packet.get("beat_evidence", [])):
            where = f"{story_id}.beat_evidence[{beat_index}]"
            beat_id = beat.get("beat_id")
            if not isinstance(beat_id, str) or not beat_id:
                errors.append(f"{where}.beat_id must be non-empty")
            elif beat_id in beat_ids:
                errors.append(f"{story_id}: duplicate Beat evidence {beat_id}")
            else:
                beat_ids.add(beat_id)
            direct_event_ids = check_subset(
                beat.get("direct_event_ids"),
                set(packet_events),
                f"{where}.direct_event_ids",
                errors,
            )
            fact_context_event_ids = check_subset(
                beat.get("fact_context_event_ids"),
                set(packet_events),
                f"{where}.fact_context_event_ids",
                errors,
            )
            expanded_event_ids = check_subset(
                beat.get("expanded_event_ids"),
                set(packet_events),
                f"{where}.expanded_event_ids",
                errors,
            )
            if not direct_event_ids.issubset(expanded_event_ids):
                errors.append(f"{where}.direct_event_ids must be included in expanded_event_ids")
            if not fact_context_event_ids.issubset(expanded_event_ids):
                errors.append(
                    f"{where}.fact_context_event_ids must be included in expanded_event_ids"
                )
            check_subset(
                beat.get("candidate_ids"),
                set(packet_candidates),
                f"{where}.candidate_ids",
                errors,
            )
            check_subset(
                beat.get("evidence_window_ids"),
                set(packet_windows),
                f"{where}.evidence_window_ids",
                errors,
            )
            check_subset(
                beat.get("context_window_ids"),
                set(packet_windows),
                f"{where}.context_window_ids",
                errors,
            )
            check_subset(
                beat.get("source_ids"),
                set(packet_sources),
                f"{where}.source_ids",
                errors,
            )
            requested = beat.get("requested_ids", {})
            for key, known in (
                ("character_ids", set(packet_characters)),
                ("relationship_ids", set(packet_relationships)),
                ("story_thread_ids", set(packet_threads)),
                ("thread_beat_ids", set(packet_thread_beats)),
                ("fact_ids", set(packet_facts)),
                ("event_ids", set(packet_events)),
                ("candidate_ids", set(packet_candidates)),
            ):
                check_subset(
                    requested.get(key),
                    known,
                    f"{where}.requested_ids.{key}",
                    errors,
                )
            check_subset(
                beat.get("resolved_thread_beat_ids"),
                set(packet_thread_beats),
                f"{where}.resolved_thread_beat_ids",
                errors,
            )
            script_beat = script_beats.get(beat_id)
            expected_direct_event_ids: set[str] = set()
            expected_fact_context_event_ids: set[str] = set()
            expected_must_shows: dict[str, dict[str, Any]] = {}
            if isinstance(script_beat, dict):
                retrieval = script_beat.get("retrieval_requirements", {})
                expected_direct_event_ids.update(
                    item for item in script_beat.get("event_ids", []) if isinstance(item, str)
                )
                expected_direct_event_ids.update(
                    item for item in retrieval.get("event_ids", []) if isinstance(item, str)
                )
                direct_candidate_ids = {
                    item
                    for item in (
                        *script_beat.get("candidate_suggestions", []),
                        *retrieval.get("candidate_ids", []),
                    )
                    if isinstance(item, str)
                }
                for candidate_id in direct_candidate_ids:
                    candidate = global_candidates.get(candidate_id, {})
                    expected_direct_event_ids.update(
                        item for item in candidate.get("event_ids", []) if isinstance(item, str)
                    )
                raw_fact_context_event_ids: set[str] = set()
                for item in script_beat.get("must_show", []):
                    if not isinstance(item, dict):
                        continue
                    must_show_id = item.get("id")
                    if isinstance(must_show_id, str):
                        expected_must_shows[must_show_id] = item
                    expected_direct_event_ids.update(
                        value
                        for value in item.get("evidence_event_ids", [])
                        if isinstance(value, str)
                    )
                    for fact_id in item.get("evidence_fact_ids", []):
                        fact = global_facts.get(fact_id, {})
                        raw_fact_context_event_ids.update(
                            value for value in fact.get("event_ids", []) if isinstance(value, str)
                        )
                anchor_episodes = {
                    int(global_events[event_id]["episode"])
                    for event_id in expected_direct_event_ids
                    if event_id in global_events
                    and isinstance(global_events[event_id].get("episode"), int)
                }
                anchor_episodes.update(
                    int(global_candidates[candidate_id]["episode"])
                    for candidate_id in direct_candidate_ids
                    if candidate_id in global_candidates
                    and isinstance(global_candidates[candidate_id].get("episode"), int)
                )
                expected_fact_context_event_ids = filter_expanded_events(
                    raw_fact_context_event_ids,
                    events=global_events,
                    anchor_episodes=anchor_episodes,
                    lookback=retrieval.get("lookback", "same_episode"),
                )
                if direct_event_ids != expected_direct_event_ids:
                    errors.append(
                        f"{where}.direct_event_ids differ from explicit "
                        "Beat/must-show/Retrieval/Candidate Event evidence"
                    )
                if fact_context_event_ids != expected_fact_context_event_ids:
                    errors.append(
                        f"{where}.fact_context_event_ids differ from "
                        "lookback-filtered must-show Fact context"
                    )
            observed_must_show_ids: set[str] = set()
            for show_index, must_show in enumerate(beat.get("must_show_evidence", [])):
                show_where = f"{where}.must_show_evidence[{show_index}]"
                must_show_id = must_show.get("must_show_id")
                if isinstance(must_show_id, str):
                    observed_must_show_ids.add(must_show_id)
                direct_show_events = check_subset(
                    must_show.get("direct_event_ids"),
                    set(packet_events),
                    f"{show_where}.direct_event_ids",
                    errors,
                )
                fact_show_events = check_subset(
                    must_show.get("fact_context_event_ids"),
                    set(packet_events),
                    f"{show_where}.fact_context_event_ids",
                    errors,
                )
                resolved_show_events = check_subset(
                    must_show.get("resolved_event_ids"),
                    set(packet_events),
                    f"{show_where}.resolved_event_ids",
                    errors,
                )
                if resolved_show_events != (direct_show_events | fact_show_events):
                    errors.append(
                        f"{show_where}.resolved_event_ids must equal direct "
                        "plus Fact-context Event IDs"
                    )
                expected_show = expected_must_shows.get(must_show_id)
                if isinstance(expected_show, dict):
                    expected_direct_show_events = {
                        item
                        for item in expected_show.get("evidence_event_ids", [])
                        if isinstance(item, str)
                    }
                    raw_fact_show_events = {
                        event_id
                        for fact_id in expected_show.get("evidence_fact_ids", [])
                        for event_id in global_facts.get(fact_id, {}).get("event_ids", [])
                        if isinstance(event_id, str)
                    }
                    expected_fact_show_events = (
                        raw_fact_show_events & expected_fact_context_event_ids
                    )
                    if direct_show_events != expected_direct_show_events:
                        errors.append(
                            f"{show_where}.direct_event_ids differ from "
                            "explicit must-show Event evidence"
                        )
                    if fact_show_events != expected_fact_show_events:
                        errors.append(
                            f"{show_where}.fact_context_event_ids differ "
                            "from Fact-linked Context evidence"
                        )
                    has_direct_range = any(
                        event_range_refs(global_events[event_id])
                        for event_id in expected_direct_show_events
                        if event_id in global_events
                    )
                    expected_status = (
                        "covered" if expected_direct_show_events and has_direct_range else "missing"
                    )
                    if must_show.get("status") != expected_status:
                        errors.append(
                            f"{show_where}.status cannot be covered by Fact-only evidence"
                        )
            if isinstance(script_beat, dict) and observed_must_show_ids != set(expected_must_shows):
                errors.append(f"{where}.must_show_evidence differs from approved Script")
            tiered_refs: list[dict[str, Any]] = []
            for range_field in (
                "direct_range_refs",
                "candidate_range_refs",
                "context_range_refs",
            ):
                for range_index, range_ref in enumerate(beat.get(range_field, [])):
                    range_where = f"{where}.{range_field}[{range_index}]"
                    tiered_refs.append(range_ref)
                    if range_ref.get("source_id") not in packet_sources:
                        errors.append(f"{range_where} has unknown source_id")
                    origin = range_ref.get("origin")
                    origin_id = range_ref.get("origin_id")
                    if origin == "event" and origin_id not in packet_events:
                        errors.append(f"{range_where} has unknown Event origin")
                    if origin == "candidate" and origin_id not in packet_candidates:
                        errors.append(f"{range_where} has unknown Candidate origin")
                    check_subset(
                        range_ref.get("evidence_window_ids"),
                        set(packet_windows),
                        f"{range_where}.evidence_window_ids",
                        errors,
                    )

            def normalized(item: dict[str, Any]) -> tuple[Any, ...]:
                return (
                    item.get("source_id"),
                    float(item.get("start", -1)),
                    float(item.get("end", -1)),
                    item.get("origin"),
                    item.get("origin_id"),
                )

            legacy_keys = {normalized(item) for item in beat.get("range_refs", [])}
            tiered_keys = {normalized(item) for item in tiered_refs}
            if legacy_keys != tiered_keys:
                errors.append(f"{where}.range_refs must equal the union of tiered refs")
        expected_beat_ids = {
            beat.get("id") for beat in script.get("beats", []) if isinstance(beat.get("id"), str)
        }
        if beat_ids != expected_beat_ids:
            errors.append(f"{story_id}: packet Beat coverage differs from approved Script")
        coverage = packet.get("coverage_summary", {})
        required_thread_beat_ids = set(script.get("required_thread_beat_ids", []))
        if set(coverage.get("required_thread_beat_ids", [])) != required_thread_beat_ids:
            errors.append(f"{story_id}: packet required Thread Beat coverage is stale")
        missing_required = set(coverage.get("missing_required_thread_beat_ids", []))
        covered_thread_beats = set(coverage.get("covered_thread_beat_ids", []))
        if missing_required != required_thread_beat_ids - covered_thread_beats:
            errors.append(
                f"{story_id}: packet missing required Thread Beat coverage is inconsistent"
            )
        if missing_required and packet.get("status") != "incomplete":
            errors.append(f"{story_id}: missing required Thread Beats must make packet incomplete")
        if not set(packet_windows).issubset(global_manifest_windows):
            errors.append(f"{story_id}: packet contains windows outside manifest")
    if index.get("status") == "partially_ready":
        warnings.append(
            "Story Evidence is partially ready; incomplete Stories remain selected for repair"
        )
    elif index.get("status") == "incomplete":
        warnings.append("Story Evidence contains incomplete must-have coverage")
    elif index.get("status") == "needs_video_review":
        warnings.append("Story Evidence is complete but requires targeted video review")
    return {"ok": not errors, "errors": errors, "warnings": warnings}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_root", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    job_root = args.job_root.expanduser().resolve()
    report = validate(job_root)
    report_path = (
        args.report.expanduser().resolve()
        if args.report
        else job_root / "story-evidence-validation.json"
    )
    atomic_write_json(report_path, report)
    print(f"STATUS\t{'OK' if report['ok'] else 'FAILED'}")
    print(f"ERRORS\t{len(report['errors'])}")
    print(f"WARNINGS\t{len(report['warnings'])}")
    print(f"REPORT\t{report_path}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
