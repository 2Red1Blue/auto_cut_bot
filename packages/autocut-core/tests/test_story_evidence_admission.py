"""Offline admission tests for the legacy Story Evidence boundary."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest
from autocut_core import ArtifactBus, PipelineConfig, Task
from autocut_core.contracts.evidence_validation import (
    selected_manifest_path,
)
from autocut_core.contracts.evidence_validation import (
    validate as validate_story_evidence,
)
from autocut_core.io import sha256_file
from autocut_core.libs.evidence_builder import build_packet
from autocut_core.stages.ac_plan_orchestration.evidence import stage as evidence_stage


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def _approval(
    script_path: Path,
    script_sha256: str,
    portfolio_sha256: str,
    **overrides: Any,
) -> dict[str, Any]:
    story = {
        "story_id": "story-1",
        "decision": "approved",
        "script_path": str(script_path),
        "approved_script_sha256": script_sha256,
        "portfolio_sha256": portfolio_sha256,
        "decided_at": "2026-08-22T00:00:00Z",
    }
    approval = {
        "fulfillment_status": "ready",
        "selected_story_ids": ["story-1"],
        "stories": [story],
    }
    approval.update(overrides)
    return approval


def _make_job_root(tmp_path: Path) -> tuple[Path, Path, dict[str, Any]]:
    portfolio_path = tmp_path / "story-portfolio.json"
    _write_json(portfolio_path, {"stories": []})
    script_path = tmp_path / "story-1.json"
    # The actual Story Script schema is deliberately not duplicated here.  The
    # success test injects the stage's schema gate; malformed-script coverage
    # uses this minimal object against the real gate below.
    _write_json(script_path, {"story_id": "story-1", "portfolio": {}})
    portfolio_sha256 = sha256_file(portfolio_path)
    approval = _approval(script_path, sha256_file(script_path), portfolio_sha256)
    return tmp_path, script_path, approval


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda root, script, approval: approval.update(fulfillment_status="pending"), "not ready"),
        (
            lambda root, script, approval: approval.update(selected_story_ids=[]),
            "selects no Stories",
        ),
        (
            lambda root, script, approval: approval.update(selected_story_ids=["other"]),
            "match approved Stories",
        ),
        (
            lambda root, script, approval: approval.update(
                selected_story_ids=["story-1", "story-1"]
            ),
            "contains duplicates",
        ),
        (
            lambda root, script, approval: approval["stories"].append(dict(approval["stories"][0])),
            "duplicate story_id",
        ),
        (
            lambda root, script, approval: approval["stories"][0].update(
                script_path=str(root / "missing.json")
            ),
            "Script is missing",
        ),
        (
            lambda root, script, approval: approval["stories"][0].update(
                approved_script_sha256="0" * 64
            ),
            "Script SHA-256 is stale",
        ),
        (
            lambda root, script, approval: approval["stories"][0].update(portfolio_sha256="0" * 64),
            "Portfolio SHA-256 is stale",
        ),
    ],
)
def test_rejected_admission_creates_no_evidence_output(
    tmp_path: Path,
    mutate: Any,
    expected: str,
) -> None:
    root, script_path, approval = _make_job_root(tmp_path)
    mutate(root, script_path, approval)
    approval_path = root / "story-approval.json"
    _write_json(approval_path, approval)
    stage = evidence_stage.EvidenceStage(PipelineConfig(job_root=root))

    with pytest.raises((FileNotFoundError, ValueError), match=expected):
        stage.execute(
            ArtifactBus(root),
            [Task(type="assemble", payload={"story_approval": str(approval_path)})],
        )

    assert not (root / "story-evidence").exists()


def test_invalid_script_is_rejected_before_evidence_output(tmp_path: Path) -> None:
    root, _, approval = _make_job_root(tmp_path)
    approval_path = root / "story-approval.json"
    _write_json(approval_path, approval)
    stage = evidence_stage.EvidenceStage(PipelineConfig(job_root=root))

    with pytest.raises(ValueError, match="invalid approved Story Script"):
        stage.execute(
            ArtifactBus(root),
            [Task(type="assemble", payload={"story_approval": str(approval_path)})],
        )

    assert not (root / "story-evidence").exists()


@pytest.mark.parametrize(
    ("canonical", "legacy"),
    [
        ("source_manifest.json", "source-manifest.json"),
        ("window_manifest.json", "window-manifest.json"),
    ],
)
def test_manifest_alias_resolution_is_deterministic(
    tmp_path: Path, canonical: str, legacy: str
) -> None:
    legacy_path = tmp_path / legacy
    legacy_path.write_text("{}", encoding="utf-8")
    assert selected_manifest_path(tmp_path, canonical, legacy) == legacy_path

    canonical_path = tmp_path / canonical
    canonical_path.write_text("{}", encoding="utf-8")
    assert selected_manifest_path(tmp_path, canonical, legacy) == canonical_path


def _minimal_teaser_contract() -> dict[str, Any]:
    return {
        "mode": "none",
        "primary_highlight_candidate_id": "candidate-none",
        "maximum_span_count": 1,
        "preferred_minimum_seconds": 8,
        "preferred_maximum_seconds": 15,
        "maximum_seconds": 15,
        "maximum_reaction_tail_seconds": 2,
        "treatment_option_id": "treatment-1",
        "strategy": "chronological_compression",
        "reprise_policy": "not_applicable",
        "selection_reason": "No teaser candidate is needed for this fixture.",
        "explanation_beat_ids": [],
        "reprise_beat_ids": [],
        "reprise_delay_minimum_progression_beats": 0,
        "reprise_function": "not_applicable",
    }


def _minimal_script(portfolio_sha256: str) -> dict[str, Any]:
    beat = {
        "id": "beat-1",
        "role": "setup",
        "must_have": False,
        "temporal_position": "mainline",
        "event_ids": [],
        "candidate_suggestions": [],
        "must_show": [
            {
                "id": "must-show-1",
                "description": "The fixture deliberately has no physical evidence.",
                "observable_via": "visual",
                "evidence_event_ids": [],
                "evidence_fact_ids": [],
            }
        ],
        "retrieval_requirements": {
            "search_intent": "Verify deterministic empty-catalog handling.",
            "character_ids": [],
            "relationship_ids": [],
            "story_thread_ids": [],
            "thread_beat_ids": [],
            "fact_ids": [],
            "event_ids": [],
            "candidate_ids": [],
            "continuity": "continuous_scene",
            "lookback": "same_episode",
        },
        "evidence_status": "missing",
        "material_risks": [],
        "must_not_reveal_fact_ids": [],
        "required_before_fact_ids": [],
        "introduced_fact_ids": [],
        "resolved_question_ids": [],
    }
    beats = []
    for position, role in enumerate(("setup", "escalation", "turn_or_reveal", "payoff"), start=1):
        item = dict(beat)
        item.update(
            {
                "id": f"beat-{position}",
                "role": role,
                "dramatic_purpose": "Fixture purpose.",
                "narrative_description": "Fixture narrative.",
                "concrete_story_content": "Fixture content.",
                "must_not_reveal_fact_ids": [],
                "required_before_fact_ids": [],
                "introduced_fact_ids": [],
                "resolved_question_ids": [],
                "viewer_state_before": ["state"],
                "viewer_state_after": ["state"],
                "emotional_change": {"from": "", "to": ""},
                "causal_role": "context",
                "thread_role": "primary",
                "estimated_source_duration_seconds": {"minimum": 0, "maximum": 0},
                "causal_dependency": {
                    "explains_opening_highlight": False,
                    "required_before_fact_ids": [],
                    "required_relationship_ids": [],
                    "required_event_ids": [],
                    "required_thread_beat_ids": [],
                    "causal_ancestor_episode_range": {
                        "min_episode": 1,
                        "max_episode": 1,
                        "reason": "No cross-unit explanation is required.",
                    },
                    "cross_unit_retrieval": {
                        "required": False,
                        "source_unit_ids": [],
                        "retrieval_status": "covered",
                    },
                },
                "physical_evidence": {
                    "physical_ranges": [],
                    "source_count": 0,
                    "atomic_event_count": 0,
                    "physical_union_duration_seconds": 0,
                    "physical_envelope_duration_seconds": 0,
                    "internal_gap_seconds": 0,
                    "timeline_segment_count": 0,
                    "compaction_status": "atomic",
                },
                "continuity_required": False,
            }
        )
        beats.append(item)
    return {
        "schema_version": "1.6",
        "story_id": "story-1",
        "title": "Approved Story",
        "logline": "Fixture logline.",
        "story_promise": "Fixture promise.",
        "central_question": "Fixture question?",
        "character_ids": ["char-fixture"],
        "relationship_ids": [],
        "story_thread_ids": ["thread-fixture"],
        "primary_story_thread_id": "thread-fixture",
        "treatment_options_sha256": "a" * 64,
        "selected_thread_beat_ids": ["thread-beat-1"],
        "required_thread_beat_ids": ["thread-beat-1"],
        "omitted_thread_beats": [],
        "start_state": "start",
        "end_state": "end",
        "local_payoff": "payoff",
        "target_duration": {
            "minimum_seconds": 0,
            "preferred_minimum_seconds": 0,
            "preferred_target_seconds": 1,
            "maximum_seconds": 1200,
        },
        "required_fact_ids": [],
        "intentional_mystery_fact_ids": [],
        "beats": beats,
        "ending_hook_intent": {
            "question": "",
            "event_ids": [],
            "candidate_ids": [],
            "story_thread_ids": [],
            "may_be_empty": True,
        },
        "teaser_contract": _minimal_teaser_contract(),
        "portfolio": {
            "production_slot": 1,
            "portfolio_sha256": portfolio_sha256,
            "role": "primary",
        },
        "evidence_event_ids": ["event-000000000000"],
        "primary_story_thread_id_source": "model",
        "genre_profile": "fixture",
        "golden_case_ids": [],
        "integrated_support_thread_ids": [],
        "editorial_contract": {
            "primary_story_thread_id": "thread-fixture",
            "secondary_thread_ids": [],
            "integrated_support_thread_ids": [],
            "mainline_type": "fixture",
            "required_bridge_beat_ids": [],
            "same_line_extension_only": True,
            "future_arc_injection_forbidden": True,
            "continuity_contract": {
                "same_primary_thread_across_opening_body_ending": True,
                "cross_segment_bridge_required": True,
                "allowed_bridge_types": [],
                "lookback_allowed_only_for": [],
                "lookback_must_return_to_mainline": True,
                "future_complete_arc_injection_forbidden": True,
                "unexplained_jump_status": "blocked",
            },
            "ending_policy": {
                "preferred_landing": "same_primary_thread_hook",
                "hook_types": [],
                "no_hook_fallback": "current_story_line_episode_tail",
                "no_hook_is_allowed": True,
                "invented_hook_forbidden": True,
                "future_arc_after_hook_forbidden": True,
            },
            "duration_extension_policy": {
                "trigger": "below_minimum_duration",
                "minimum_seconds": 0,
                "order": [],
                "after_threshold": "current_episode_tail",
                "same_primary_thread_only": True,
                "must_be_forward_chronological": True,
                "no_cross_thread_fill": True,
                "no_duplicate_or_functionless_fill": True,
                "stop_without_evidence": True,
            },
            "ending_hook_type": "unresolved_outcome",
            "golden_sample_reference": "fixture",
        },
        "causal_ancestor_episode_range": {
            "min_episode": 1,
            "max_episode": 1,
            "reason": "No cross-unit explanation is required.",
        },
        "required_context_ids": [],
        "scope_policy": {
            "analysis_unit_policy": "processing_only",
            "story_scope_policy": "series_global",
            "cross_unit_retrieval_allowed": True,
            "cross_unit_retrieval_required_for_montage": True,
            "unresolved_dependency_action": "blocked",
            "policy_version": "fixture-v1",
        },
        "feasibility": {
            "status": "feasible",
            "method": "functional-evidence-duration-v3-story-coherence",
            "assumptions": {
                "context_padding_seconds": 0,
                "usable_ratio": 0,
                "context_entity_expansion_counts_toward_duration": False,
            },
            "estimated_source_duration_min_seconds": 0,
            "estimated_source_duration_max_seconds": 0,
            "meets_5_minimum": False,
            "meets_10_preferred": False,
            "soft_target_seconds": 0,
            "meets_soft_target": False,
            "soft_target_gap_seconds": 0,
            "editorial_diagnostics": {
                "policy_version": "fixture-v1",
                "status": "pass",
                "mainline": "thread-fixture",
                "primary_story_thread_id": "thread-fixture",
                "thread_sequence": ["thread-fixture"],
                "thread_switch_count": 0,
                "secondary_line_share": 0,
                "secondary_line_shares": {},
                "independent_secondary_line_share": 0,
                "integrated_support_line_share": 0,
                "integrated_support_thread_ids": [],
                "arc_nodes_present": [],
                "hook_strength": {
                    "conflict_is_observable": False,
                    "relationship_and_stakes_are_understandable": False,
                    "open_question_remains": False,
                    "signals": 0,
                },
                "opening_signal_audit": {
                    "signal_types": [],
                    "first_three_seconds_signal": None,
                    "action_or_speech_complete": None,
                    "context_within_8_seconds": None,
                    "lead_in_artifact": None,
                    "lead_in_duration_seconds": None,
                    "source_start_is_effective_opening_frame": None,
                    "effective_opening_frame_note": "fixture",
                    "cut_risk": None,
                },
                "opening_strategy": {
                    "strategy": "fixture",
                    "findings": [],
                    "status": "pass",
                },
                "continuity_contract": {
                    "status": "pass",
                    "enforced": True,
                    "lookback_positions": [],
                    "findings": [],
                },
                "findings": [],
                "failure_codes": [],
                "repair_routes": [],
                "duration_policy_applied": "fixture",
            },
            "covered_beat_ids": [],
            "partial_beat_ids": [],
            "missing_beat_ids": [],
            "conflicting_beat_ids": [],
            "needs_video_review_beat_ids": [],
            "review_event_ids": [],
            "highlight_candidate_ids": [],
            "hook_candidate_ids": [],
            "material_risks": [],
            "teaser_diagnostics": {
                "mode": "none",
                "primary_highlight_candidate_id": "",
                "candidate_duration_seconds": 0,
                "physical_obligation_duration_seconds": 0,
                "mandatory_reprise_event_ids": [],
                "maximum_repeat_seconds": 0,
                "repeat_contract_status": "feasible",
                "must_show_ids": [],
                "outside_candidate_must_show_ids": [],
                "status": "feasible",
                "failure_codes": [],
                "repair_route": "story_script",
            },
        },
        "status": "awaiting_approval",
    }


def _write_empty_global_inputs(root: Path) -> None:
    bible = {
        "characters": [
            {
                "id": "char-fixture",
                "canonical_name": "Character",
                "aliases": [],
                "entity_type": "individual",
                "identity": "Character",
                "identity_evidence": {"episode": 1, "quote": "fixture quote"},
                "goals": [],
                "first_event_id": "event-000000000000",
                "evidence_event_ids": ["event-000000000000"],
            }
        ],
        "relationships": [],
        "facts": [],
        "story_threads": [
            {
                "id": "thread-fixture",
                "thread_key": "thread",
                "thread_kind": "arc",
                "title": "Thread",
                "premise": "Premise",
                "summary": "Summary",
                "character_ids": ["char-fixture"],
                "event_ids": ["event-000000000000"],
                "setup_event_ids": [],
                "escalation_event_ids": [],
                "reveal_event_ids": [],
                "payoff_event_ids": [],
                "open_question_ids": [],
                "thread_beat_ids": ["thread-beat-1"],
                "episode_ids": [1],
                "status": "open",
            }
        ],
        "thread_beats": [
            {
                "id": "thread-beat-1",
                "thread_id": "thread-fixture",
                "episode": 1,
                "phase": "setup",
                "importance": "required",
                "summary": "Beat",
                "event_ids": ["event-000000000000"],
                "requires_beat_ids": [],
            }
        ],
        "open_questions": [],
    }
    _write_json(
        root / "series-bible.json",
        bible,
    )
    digest_dir = root / "episode-digests"
    digest_dir.mkdir()
    _write_json(digest_dir / "episode-001.json", {"thread_beats": bible["thread_beats"]})
    _write_json(root / "highlight-hook-catalog.json", {"candidates": []})
    _write_json(root / "source_manifest.json", {"sources": []})
    _write_json(root / "window_manifest.json", {"windows": []})
    (root / "event-cards.jsonl").write_text("", encoding="utf-8")
    (root / "window-summaries.jsonl").write_text("", encoding="utf-8")


def test_real_builder_and_validator_accept_exact_v4_bindings(tmp_path: Path) -> None:
    root = tmp_path
    _write_empty_global_inputs(root)
    portfolio_path = root / "story-portfolio.json"
    _write_json(portfolio_path, {"stories": []})
    portfolio_sha256 = sha256_file(root / "story-portfolio.json")
    script_path = root / "story-1.json"
    _write_json(script_path, _minimal_script(portfolio_sha256))
    approval = _approval(script_path, sha256_file(script_path), portfolio_sha256)
    approval_path = root / "story-approval.json"
    _write_json(approval_path, approval)
    fingerprints = {
        "series_bible_sha256": sha256_file(root / "series-bible.json"),
        "event_cards_sha256": sha256_file(root / "event-cards.jsonl"),
        "candidate_catalog_sha256": sha256_file(root / "highlight-hook-catalog.json"),
        "source_manifest_sha256": sha256_file(root / "source_manifest.json"),
        "window_manifest_sha256": sha256_file(root / "window_manifest.json"),
        "window_summaries_sha256": sha256_file(root / "window-summaries.jsonl"),
    }
    bible = json.loads((root / "series-bible.json").read_text(encoding="utf-8"))
    characters = {item["id"]: item for item in bible["characters"]}
    threads = {item["id"]: item for item in bible["story_threads"]}
    thread_beats = {item["id"]: item for item in bible["thread_beats"]}
    packet = build_packet(
        approval_item=approval["stories"][0],
        approval_sha256=sha256_file(approval_path),
        script=json.loads(script_path.read_text(encoding="utf-8")),
        events={},
        candidates={},
        characters=characters,
        relationships={},
        facts={},
        threads=threads,
        thread_beats=thread_beats,
        questions={},
        sources={},
        manifest_windows={},
        window_summaries={},
        neighbors={},
        fingerprints=fingerprints,
        adjacent_window_hops=2,
        units_by_episode={},
    )
    evidence_dir = root / "story-evidence"
    evidence_dir.mkdir()
    packet_path = evidence_dir / "story-1.json"
    _write_json(packet_path, packet)
    index = {
        "schema_version": "1.1",
        "method": "structured-thread-beat-recall-v4",
        "status": packet["status"],
        "story_approval_sha256": sha256_file(approval_path),
        "portfolio_sha256": portfolio_sha256,
        "selected_story_count": 1,
        "packets": [
            {
                "story_id": "story-1",
                "title": packet["title"],
                "production_slot": packet["production_slot"],
                "status": packet["status"],
                "path": str(packet_path),
                "packet_sha256": sha256_file(packet_path),
                "story_script_sha256": sha256_file(script_path),
            }
        ],
    }
    _write_json(evidence_dir / "index.json", index)

    report = validate_story_evidence(root)
    assert report["ok"]
    assert report["errors"] == []
    assert index["portfolio_sha256"] == portfolio_sha256
    assert index["story_approval_sha256"] == sha256_file(approval_path)
    assert index["selected_story_count"] == 1
    assert index["packets"][0]["story_script_sha256"] == sha256_file(script_path)
    assert index["packets"][0]["packet_sha256"] == sha256_file(packet_path)

    packet["method"] = "structured-thread-beat-recall-v3"
    _write_json(packet_path, packet)
    index["packets"][0]["packet_sha256"] = sha256_file(packet_path)
    _write_json(evidence_dir / "index.json", index)
    assert not validate_story_evidence(root)["ok"]

    packet["method"] = "structured-thread-beat-recall-v4"
    packet["input_fingerprints"].pop("window_summaries_sha256")
    _write_json(packet_path, packet)
    index["packets"][0]["packet_sha256"] = sha256_file(packet_path)
    _write_json(evidence_dir / "index.json", index)
    assert not validate_story_evidence(root)["ok"]


def _stage_ready_root(tmp_path: Path, story_ids: list[str]) -> tuple[Path, Path]:
    _write_empty_global_inputs(tmp_path)
    portfolio_path = tmp_path / "story-portfolio.json"
    _write_json(portfolio_path, {"stories": []})
    portfolio_sha256 = sha256_file(portfolio_path)
    stories: list[dict[str, Any]] = []
    for story_id in story_ids:
        script_path = tmp_path / f"{story_id}.json"
        script = _minimal_script(portfolio_sha256)
        script["story_id"] = story_id
        script["title"] = story_id
        _write_json(script_path, script)
        entry = _approval(script_path, sha256_file(script_path), portfolio_sha256)["stories"][0]
        entry["story_id"] = story_id
        stories.append(entry)
    approval = {
        "fulfillment_status": "ready",
        "selected_story_ids": story_ids,
        "stories": stories,
    }
    approval_path = tmp_path / "story-approval.json"
    _write_json(approval_path, approval)
    return tmp_path, approval_path


def test_evidence_stage_publishes_real_builder_output_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, approval_path = _stage_ready_root(tmp_path, ["story-1"])
    monkeypatch.setattr(evidence_stage, "update_project_stage", lambda *args, **kwargs: None)

    result = evidence_stage.EvidenceStage(PipelineConfig(job_root=root)).execute(
        ArtifactBus(root),
        [Task(type="assemble", payload={"story_approval": str(approval_path)})],
    )

    assert len(result) == 1
    assert (root / "story-evidence" / "index.json").is_file()
    assert validate_story_evidence(root)["ok"]


def test_second_story_failure_leaves_no_published_evidence_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, approval_path = _stage_ready_root(tmp_path, ["story-1", "story-2"])
    original_build_packet = evidence_stage.build_packet
    calls = 0

    def fail_second_packet(**kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError("synthetic second Story build failure")
        return original_build_packet(**kwargs)

    monkeypatch.setattr(evidence_stage, "build_packet", fail_second_packet)
    with pytest.raises(ValueError, match="second Story"):
        evidence_stage.EvidenceStage(PipelineConfig(job_root=root)).execute(
            ArtifactBus(root),
            [Task(type="assemble", payload={"story_approval": str(approval_path)})],
        )

    assert not (root / "story-evidence").exists()


def test_candidate_validation_failure_never_publishes_final_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, approval_path = _stage_ready_root(tmp_path, ["story-1"])
    actual_validate = evidence_stage.validate_story_evidence
    observed_candidate_dir: Path | None = None

    def reject_after_real_candidate_validation(
        job_root: Path, *, evidence_dir: Path | None = None
    ) -> dict[str, Any]:
        nonlocal observed_candidate_dir
        assert evidence_dir is not None
        observed_candidate_dir = evidence_dir
        report = actual_validate(job_root, evidence_dir=evidence_dir)
        assert report["ok"]
        return {"ok": False, "errors": ["synthetic final gate rejection"], "warnings": []}

    monkeypatch.setattr(
        evidence_stage,
        "validate_story_evidence",
        reject_after_real_candidate_validation,
    )
    with pytest.raises(ValueError, match="synthetic final gate rejection"):
        evidence_stage.EvidenceStage(PipelineConfig(job_root=root)).execute(
            ArtifactBus(root),
            [Task(type="assemble", payload={"story_approval": str(approval_path)})],
        )

    assert observed_candidate_dir is not None
    assert not observed_candidate_dir.exists()
    assert not (root / "story-evidence").exists()


def test_existing_published_output_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, approval_path = _stage_ready_root(tmp_path, ["story-1"])
    existing_output = root / "story-evidence"
    existing_output.mkdir()
    existing_index = existing_output / "index.json"
    original_bytes = b'{"legacy":"must remain unchanged"}\n'
    existing_index.write_bytes(original_bytes)

    with pytest.raises(FileExistsError, match="will not be overwritten"):
        evidence_stage.EvidenceStage(PipelineConfig(job_root=root)).execute(
            ArtifactBus(root),
            [Task(type="assemble", payload={"story_approval": str(approval_path)})],
        )

    assert existing_index.read_bytes() == original_bytes


def test_competing_stage_cannot_bypass_publication_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, approval_path = _stage_ready_root(tmp_path, ["story-1"])
    monkeypatch.setattr(evidence_stage, "update_project_stage", lambda *args, **kwargs: None)
    original_build_packet = evidence_stage.build_packet
    first_builder_entered = threading.Event()
    release_first_builder = threading.Event()
    first_error: list[BaseException] = []

    def paused_first_build(**kwargs: Any) -> dict[str, Any]:
        if threading.current_thread().name == "first-evidence-stage":
            first_builder_entered.set()
            assert release_first_builder.wait(timeout=5)
        return original_build_packet(**kwargs)

    monkeypatch.setattr(evidence_stage, "build_packet", paused_first_build)

    def run_first() -> None:
        try:
            evidence_stage.EvidenceStage(PipelineConfig(job_root=root)).execute(
                ArtifactBus(root),
                [Task(type="assemble", payload={"story_approval": str(approval_path)})],
            )
        except BaseException as exc:  # asserted by the parent test thread
            first_error.append(exc)

    first = threading.Thread(target=run_first, name="first-evidence-stage")
    first.start()
    assert first_builder_entered.wait(timeout=5)
    with pytest.raises(FileExistsError, match="already reserved"):
        evidence_stage.EvidenceStage(PipelineConfig(job_root=root)).execute(
            ArtifactBus(root),
            [Task(type="assemble", payload={"story_approval": str(approval_path)})],
        )
    release_first_builder.set()
    first.join(timeout=5)
    assert not first.is_alive()
    assert first_error == []
    assert validate_story_evidence(root)["ok"]
    assert not (root / ".story-evidence-publication.lock").exists()
