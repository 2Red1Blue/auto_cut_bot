"""story_preflight Stage — 本地素材可行性预检。

输入: story_scripts, story_portfolio, series_bible, event_cards, highlight_hook_catalog
输出: story_preflight
"""

from __future__ import annotations

from pathlib import Path

from autocut_core import ArtifactBus, Artifact, Stage, StageContract, Task
from autocut_core.contracts.genre_router import (
    has_explicit_genre_contract, route_bible, validate_story_route,
)
from autocut_core.io import (
    atomic_write_json, atomic_write_text, load_json, load_jsonl,
    sha256_file, update_project_stage,
)
from autocut_core.libs.script_preflight import by_id, preflight_script, render_story_review
from autocut_core.schema.compat import validate_task_response


class PreflightStage(Stage):
    """本地可行性预检。

    对 Story Script 逐条做素材覆盖率、结构一致性、Teaser 合同校验。
    """

    @property
    def contract(self) -> StageContract:
        return StageContract(stage_name="story_preflight",
            input_artifacts=["story_scripts", "story_portfolio", "series_bible",
                             "event_cards", "highlight_hook_catalog"],
            output_artifacts=["story_preflight"],
            description="素材可行性预检",
            db_reads=[],
            db_writes=[])

    def prepare(self, bus: ArtifactBus) -> list[Task]:
        return [Task(type="local", payload={
            "story_index": self.resolve_artifact_path(bus, "story_scripts", "story_scripts"),
            "portfolio": self.resolve_artifact_path(bus, "story_portfolio", "story_portfolio"),
            "bible": self.resolve_artifact_path(bus, "series_bible", "series_bible"),
            "event_cards": self.resolve_artifact_path(bus, "event_cards", "event_cards"),
            "candidate_catalog": self.resolve_artifact_path(
                bus, "event_cards", "highlight_hook_catalog",
            ),
        })]

    def execute(self, bus: ArtifactBus, tasks: list[Task]) -> list[Artifact]:
        root: Path = self.config.job_root  # type: ignore
        p = tasks[0].payload

        index_path = Path(p["story_index"])
        index = load_json(index_path)

        bible_path = Path(p["bible"])
        bible = load_json(bible_path)
        genre_route = route_bible(bible)
        typed_genre_route = has_explicit_genre_contract(bible)

        portfolio_path = Path(p["portfolio"])
        portfolio = load_json(portfolio_path)
        portfolio_errors = validate_task_response("story_portfolio", portfolio)
        if portfolio_errors:
            raise ValueError("invalid Story Portfolio: " + "; ".join(portfolio_errors[:30]))
        portfolio_sha256 = sha256_file(portfolio_path)
        slot_by_story = {
            item["story_id"]: item["slot"]
            for item in portfolio.get("production_slots", [])
        }

        event_records = load_jsonl(Path(p["event_cards"]))
        candidate_payload = load_json(Path(p["candidate_catalog"]))

        events = by_id(event_records)
        candidates = by_id(candidate_payload.get("candidates", []))
        characters = by_id(bible.get("characters", []))
        relationships = by_id(bible.get("relationships", []))
        facts = by_id(bible.get("facts", []))
        threads = by_id(bible.get("story_threads", []))
        thread_beats = by_id(bible.get("thread_beats", []))
        questions = by_id(bible.get("open_questions", []))
        source_durations: dict[str, float] = {}

        scripts: list[dict[str, Any]] = []
        updated_entries: list[dict[str, Any]] = []
        for entry in index.get("stories", []):
            path_value = entry.get("path")
            if not isinstance(path_value, str):
                raise ValueError("story index entry is missing path")
            script_path = Path(path_value).expanduser().resolve()
            raw_script = load_json(script_path)

            binding = raw_script.get("portfolio", {})
            expected_binding = {
                "portfolio_sha256": portfolio_sha256,
                "role": "primary",
                "production_slot": slot_by_story.get(raw_script.get("story_id")),
            }
            if binding != expected_binding:
                raise ValueError(
                    f"Story Script portfolio binding is stale or invalid: {script_path}"
                )

            if typed_genre_route:
                route_errors = validate_story_route(raw_script, genre_route)
                if route_errors:
                    raise ValueError(
                        f"Story Script genre route mismatch: {script_path}: "
                        + "; ".join(route_errors)
                    )

            script = preflight_script(
                raw_script,
                events=events,
                candidates=candidates,
                characters=characters,
                relationships=relationships,
                facts=facts,
                threads=threads,
                thread_beats=thread_beats,
                questions=questions,
                source_durations=source_durations,
                context_padding_seconds=12.0,
                usable_ratio=0.6,
            )

            if typed_genre_route:
                script["genre_profile"] = genre_route["genre_profile"]
                script["golden_case_ids"] = list(genre_route["golden_case_ids"])
                script["editorial_contract"]["golden_sample_reference"] = ",".join(
                    genre_route["golden_case_ids"]
                )
                route_schema_errors = validate_task_response("story_script", script)
                if route_schema_errors:
                    raise ValueError(
                        "typed Story Script failed schema after route materialization: "
                        + "; ".join(route_schema_errors[:30])
                    )

            if script["story_id"] != entry.get("story_id"):
                raise ValueError(f"story identity mismatch: {script_path}")

            atomic_write_json(script_path, script)
            scripts.append(script)
            feasibility = script["feasibility"]
            updated_entries.append({
                "story_id": script["story_id"],
                "title": script["title"],
                "production_slot": script["portfolio"]["production_slot"],
                "portfolio_sha256": script["portfolio"]["portfolio_sha256"],
                "path": str(script_path),
                "script_sha256": sha256_file(script_path),
                "feasibility_status": feasibility["status"],
                "estimated_source_duration_min_seconds": feasibility[
                    "estimated_source_duration_min_seconds"
                ],
                "estimated_source_duration_max_seconds": feasibility[
                    "estimated_source_duration_max_seconds"
                ],
            })

        if not scripts:
            raise ValueError("story index contains no scripts")

        updated_index = {"schema_version": "1.1", "stories": updated_entries}
        atomic_write_json(index_path, updated_index)

        summary_path = root / "story-feasibility.json"
        summary = {
            "schema_version": "1.0",
            "portfolio_sha256": portfolio_sha256,
            "method": "functional-evidence-duration-v3-story-coherence",
            "assumptions": {
                "context_padding_seconds": 12.0,
                "usable_ratio": 0.6,
                "context_entity_expansion_counts_toward_duration": False,
            },
            "stories": [
                {"story_id": s["story_id"], **s["feasibility"]}
                for s in scripts
            ],
        }
        atomic_write_json(summary_path, summary)

        script_preflight_stories: list[dict[str, Any]] = []
        for script in scripts:
            feasibility = script["feasibility"]
            status = feasibility["status"]
            max_seconds = feasibility["estimated_source_duration_max_seconds"]
            min_seconds = feasibility["estimated_source_duration_min_seconds"]
            entry: dict[str, Any] = {
                "story_id": script["story_id"],
                "feasibility_status": status,
                "failure_codes": [],
            }
            if status == "awaiting_scope_merge":
                entry["failure_codes"] = ["insufficient_editorial_surplus"]
                entry["editorial_surplus_diagnostics"] = {
                    "minimum_total_duration_seconds": 0.0,
                    "available_candidate_unique_duration_seconds": max_seconds,
                    "editorial_surplus_seconds": round(max_seconds, 3),
                    "editorial_surplus_ratio": 0.0,
                }
                hint = feasibility.get("auto_merge_hint")
                if isinstance(hint, dict):
                    entry["auto_merge_hint"] = hint
            entry["estimated_source_duration_min_seconds"] = min_seconds
            entry["estimated_source_duration_max_seconds"] = max_seconds
            script_preflight_stories.append(entry)

        script_preflight_payload = {
            "schema_version": "1.0",
            "trigger_source": "script_preflight",
            "portfolio_sha256": portfolio_sha256,
            "assumptions": summary["assumptions"],
            "stories": script_preflight_stories,
        }
        script_preflight_path = root / "story-script-preflight.json"
        atomic_write_json(script_preflight_path, script_preflight_payload)

        review_path = root / "story-review.md"
        atomic_write_text(review_path, render_story_review(scripts))

        ref = bus.put("story_preflight",
                       {"path": str(root / "story-preflight.json")},
                       stage="story_preflight")
        update_project_stage(root / "project.json", "story_preflight", "completed")
        return [ref]