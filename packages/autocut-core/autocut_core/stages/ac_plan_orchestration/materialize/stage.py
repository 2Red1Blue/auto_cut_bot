"""story_plans_materialize Stage — 将选中的 Story Plan 物化为可 QC 的候选。

输入: story_plans
输出: story_plans_materialized
"""

from __future__ import annotations

from pathlib import Path

from autocut_core import (
    ArtifactBus, Artifact, Stage, StageContract, Task,
)
from autocut_core.contracts.plan_validation import validate as validate_story_plans
from autocut_core.contracts.span_validation import validate as validate_span_candidates
from autocut_core.io import (
    atomic_write_json, atomic_write_text, load_json, sha256_file,
    update_project_stage, utc_now,
)
from autocut_core.logging import get_logger
from autocut_core.libs.editorial_plan import (
    current_plan_generation,
    expand_option_selection,
    materialize_plan,
    plan_filename,
    render_review,
    validate_option_selection,
)
from autocut_core.schema.compat import validate_schema, validate_task_response


logger = get_logger(__name__)


class MaterializeStage(Stage):
    """Materialize — 将选中的 Story Plan 物化为可 QC 的候选。"""

    @property
    def contract(self) -> StageContract:
        return StageContract(
            stage_name="story_plans_materialize",
            input_artifacts=["story_plans", "scene_boundaries"],
            output_artifacts=["story_plans_materialized"],
            description="Story Plan 物化",
            db_reads=[],
            db_writes=[],
        )

    def prepare(self, bus: ArtifactBus) -> list[Task]:
        return [Task(type="materialize", payload={
            "plan_batch": self.resolve_artifact_path(bus, "story_plans", "story_plans"),
        })]

    def execute(self, bus: ArtifactBus, tasks: list[Task]) -> list[Artifact]:
        cfg = self.config
        if cfg.job_root is None:
            raise RuntimeError("job_root 未设置")
        root: Path = cfg.job_root
        p = tasks[0].payload
        batch_path = Path(p["plan_batch"])
        batch = load_json(batch_path) if batch_path.is_file() else {}
        allow_partial = cfg.mode == "auto"

        if not batch.get("jobs"):
            print(f"[{utc_now()}] [story_plans_materialize] 当前 Plan 批次 "
                  "jobs=[]; 历史 Plans 保持非激活")

        # --- 从 main() 提取的核心逻辑 ---
        manifest_path = batch_path.resolve()
        span_report = validate_span_candidates(root)
        if not span_report["ok"]:
            raise ValueError(
                "Span Candidate validation failed: "
                + "; ".join(span_report["errors"][:30])
            )
        manifest = load_json(manifest_path)
        preflight_path = root / "story-plan-preflight.json"
        if not preflight_path.is_file():
            raise FileNotFoundError(preflight_path)
        preflight = load_json(preflight_path)
        generation_sha256 = current_plan_generation(root)
        if (
            preflight.get("plan_generation_sha256") != generation_sha256
            or manifest.get("plan_generation_sha256") != generation_sha256
        ):
            raise ValueError(
                "Story Plan batch/preflight generation is stale; rerun "
                "prepare_story_stages.py plans"
            )

        approval_path = root / "story-approval.json"
        portfolio_path = root / "story-portfolio.json"
        evidence_index_path = root / "story-evidence" / "index.json"
        span_index_path = root / "span-candidates" / "index.json"
        approval = load_json(approval_path)
        evidence_index = load_json(evidence_index_path)
        span_index = load_json(span_index_path)
        approved_entries = {
            item["story_id"]: item
            for item in approval["stories"]
            if item.get("decision") == "approved"
        }
        evidence_entries = {
            item["story_id"]: item for item in evidence_index["packets"]
        }
        bundle_entries = {
            item["story_id"]: item for item in span_index["bundles"]
        }
        jobs = [
            item
            for item in manifest.get("jobs", [])
            if isinstance(item, dict)
            and item.get("task") == "story_plan_selection"
        ]
        job_by_story: dict[str, dict] = {}
        context_by_story: dict[str, dict] = {}
        for job in jobs:
            context_path = Path(job["context_file"]).expanduser().resolve()
            context = load_json(context_path)
            story_id = context.get("story_id")
            if (
                context.get("input_fingerprints", {}).get(
                    "plan_generation_sha256"
                )
                != generation_sha256
            ):
                raise ValueError(
                    "Story Plan context generation is stale: "
                    f"{context_path}"
                )
            if not isinstance(story_id, str) or story_id in job_by_story:
                raise ValueError("Story Plan batch has missing or duplicate story identity")
            job_by_story[story_id] = job
            context_by_story[story_id] = context
        if set(job_by_story) - set(bundle_entries):
            raise ValueError(
                "Story Plan batch references Story IDs missing from Span "
                "Candidate Bundles"
            )
        missing_bundles = set(bundle_entries) - set(job_by_story)
        if missing_bundles and not allow_partial:
            raise ValueError(
                "Story Plan batch does not cover every Span Candidate Bundle "
                "exactly; use --allow-partial to materialize only the Stories "
                "present in the batch. Missing: " + ", ".join(sorted(missing_bundles))
            )
        bundle_entries_to_process = {
            story_id: entry
            for story_id, entry in bundle_entries.items()
            if story_id in job_by_story
        }

        output_dir = root / "story-plans"
        generation_dir = output_dir / "generations" / generation_sha256
        plans: list[dict] = []
        index_entries: list[dict] = []
        common_fingerprints = {
            "story_approval_sha256": sha256_file(approval_path),
            "portfolio_sha256": sha256_file(portfolio_path),
            "story_evidence_index_sha256": sha256_file(evidence_index_path),
            "span_candidate_index_sha256": sha256_file(span_index_path),
            "plan_generation_sha256": generation_sha256,
        }
        for story_id, bundle_entry in sorted(
            bundle_entries_to_process.items(),
            key=lambda item: int(item[1]["production_slot"]),
        ):
            job = job_by_story[story_id]
            output_path = Path(job["output"]).expanduser().resolve()
            if not output_path.is_file():
                raise FileNotFoundError(f"missing Story Plan selection result: {output_path}")
            selection = load_json(output_path)
            selection_errors = validate_task_response("story_plan_selection", selection)
            response_format_value = job.get("response_format")
            if isinstance(response_format_value, dict):
                response_schema = response_format_value.get(
                    "json_schema", {}
                ).get("schema")
                if isinstance(response_schema, dict):
                    selection_errors.extend(
                        validate_schema(selection, response_schema)
                    )
            legal_options = context_by_story[story_id].get("legal_option_contract")
            if not isinstance(legal_options, dict):
                selection_errors.append("Story Plan context has no legal_option_contract")
            else:
                selection_errors.extend(
                    validate_option_selection(selection, legal_options)
                )
            if selection_errors:
                raise ValueError(
                    f"invalid Story Plan selection {output_path}: "
                    + "; ".join(selection_errors[:40])
                )
            expanded_selection = expand_option_selection(selection, legal_options)
            approved = approved_entries.get(story_id)
            evidence_entry = evidence_entries.get(story_id)
            if approved is None or evidence_entry is None:
                raise ValueError(f"{story_id}: missing approved Story inputs")
            script_path = Path(approved["script_path"]).expanduser().resolve()
            packet_path = Path(evidence_entry["path"]).expanduser().resolve()
            bundle_path = Path(bundle_entry["path"]).expanduser().resolve()
            script_sha256 = sha256_file(script_path)
            packet_sha256 = sha256_file(packet_path)
            bundle_sha256 = sha256_file(bundle_path)
            if script_sha256 != approved.get("approved_script_sha256"):
                raise ValueError(f"approved Story Script is stale: {story_id}")
            if packet_sha256 != evidence_entry.get("packet_sha256"):
                raise ValueError(f"Story Evidence Packet is stale: {story_id}")
            if bundle_sha256 != bundle_entry.get("bundle_sha256"):
                raise ValueError(f"Span Candidate Bundle is stale: {story_id}")
            fingerprints = {
                **common_fingerprints,
                "story_script_sha256": script_sha256,
                "story_evidence_packet_sha256": packet_sha256,
                "span_candidate_bundle_sha256": bundle_sha256,
                "selection_result_sha256": sha256_file(output_path),
            }
            plan = materialize_plan(
                expanded_selection,
                script=load_json(script_path),
                bundle=load_json(bundle_path),
                evidence_packet=load_json(packet_path),
                fingerprints=fingerprints,
            )
            
            # 帧级精度校验：验证 clip 边界是否对齐到场景边界
            scene_boundaries = None
            try:
                scene_ref = bus.latest("scene_boundaries")
                if scene_ref is not None:
                    scene_boundaries = bus.get(scene_ref)
                else:
                    # 回退到文件系统
                    scene_boundaries_path = root / "scene_boundaries.json"
                    if scene_boundaries_path.is_file():
                        scene_boundaries = load_json(scene_boundaries_path)
            except Exception as exc:
                logger.warning(
                    "materialize: 加载 scene_boundaries 失败 (非阻塞): %s", exc
                )
            
            boundary_warnings = []
            if scene_boundaries and "episodes" in scene_boundaries:
                from autocut_core.semantic.scene_boundary_fusion import extract_cut_points
                
                # 读取 fusion 参数（与 vlm_analysis 一致）
                fusion_cfg = cfg.extra.get("fusion", {})
                lead_in = float(fusion_cfg.get("lead_in", 0.3))
                lead_out = float(fusion_cfg.get("lead_out", 0.0))
                # 校验 tolerance = fusion tolerance + lead_in/lead_out 余量
                # 因为 fusion 后 start = cut_point + lead_in，距离切点 lead_in 秒
                base_tolerance = float(fusion_cfg.get("tolerance", 0.5))
                
                for block in plan.get("blocks", []):
                    for clip in block.get("clips", []):
                        source_id = clip.get("source_id")
                        source_start = clip.get("source_start")
                        source_end = clip.get("source_end")
                        
                        if not source_id or source_start is None or source_end is None:
                            continue
                        
                        # 从 source_id 提取 episode_id (格式: source-003 -> 3)
                        try:
                            ep_num = int(source_id.split("-")[1])
                            episode_id = str(ep_num)
                        except (IndexError, ValueError):
                            continue
                        
                        if episode_id not in scene_boundaries["episodes"]:
                            continue
                        
                        cut_points = extract_cut_points(scene_boundaries["episodes"][episode_id])
                        if not cut_points:
                            continue
                        
                        # 检查 source_start 是否接近某个 (cut_point + lead_in)
                        # fusion 后 start 应该是 cut_point + lead_in，所以用 lead_in 扩展 tolerance
                        import bisect
                        start_tolerance = base_tolerance + lead_in
                        idx = bisect.bisect_left(cut_points, source_start)
                        start_aligned = False
                        if idx < len(cut_points) and abs(cut_points[idx] - source_start) <= start_tolerance:
                            start_aligned = True
                        elif idx > 0 and abs(cut_points[idx - 1] - source_start) <= start_tolerance:
                            start_aligned = True
                        
                        # 检查 source_end 是否接近某个 (cut_point + lead_out)
                        end_tolerance = base_tolerance + abs(lead_out)
                        idx = bisect.bisect_left(cut_points, source_end)
                        end_aligned = False
                        if idx < len(cut_points) and abs(cut_points[idx] - source_end) <= end_tolerance:
                            end_aligned = True
                        elif idx > 0 and abs(cut_points[idx - 1] - source_end) <= end_tolerance:
                            end_aligned = True
                        
                        if not start_aligned or not end_aligned:
                            boundary_warnings.append({
                                "clip_id": clip.get("id"),
                                "source_id": source_id,
                                "episode": episode_id,
                                "source_start": source_start,
                                "source_end": source_end,
                                "start_aligned": start_aligned,
                                "end_aligned": end_aligned,
                            })
            
            if boundary_warnings:
                logger.warning(
                    "materialize: %s 有 %d 个 clip 边界未对齐到场景边界 (tolerance=0.5s)",
                    story_id, len(boundary_warnings),
                )
                for w in boundary_warnings[:3]:  # 只打印前 3 个
                    logger.warning(
                        "  clip %s (%s): start=%.2f (aligned=%s), end=%.2f (aligned=%s)",
                        w["clip_id"], w["source_id"],
                        w["source_start"], w["start_aligned"],
                        w["source_end"], w["end_aligned"],
                    )
                if len(boundary_warnings) > 3:
                    logger.warning("  ... 还有 %d 个", len(boundary_warnings) - 3)
                
                # 将边界警告添加到 plan 中
                plan["boundary_alignment_warnings"] = boundary_warnings
            
            plan_path = generation_dir / plan_filename(story_id)
            atomic_write_json(plan_path, plan)
            plans.append(plan)
            index_entries.append(
                {
                    "story_id": story_id,
                    "title": plan["title"],
                    "production_slot": plan["production_slot"],
                    "status": plan["status"],
                    "path": str(plan_path),
                    "plan_sha256": sha256_file(plan_path),
                    "story_script_sha256": script_sha256,
                    "span_candidate_bundle_sha256": bundle_sha256,
                    "selection_result_sha256": fingerprints["selection_result_sha256"],
                    "estimated_duration_seconds": plan["estimated_duration_seconds"],
                    "block_count": len(plan["blocks"]),
                    "clip_count": sum(len(item["clips"]) for item in plan["blocks"]),
                }
            )
        ready_count = sum(item["status"] == "ready_for_video_qc" for item in plans)
        if plans and ready_count == len(plans):
            index_status = "ready_for_video_qc"
        elif ready_count:
            index_status = "partially_ready"
        else:
            index_status = "blocked"
        index = {
            "schema_version": "1.0",
            "method": "legal-option-selection-local-materialization-v2",
            "status": index_status,
            "story_approval_sha256": common_fingerprints["story_approval_sha256"],
            "span_candidate_index_sha256": common_fingerprints["span_candidate_index_sha256"],
            "plan_generation_sha256": generation_sha256,
            "plan_count": len(plans),
            "ready_plan_count": ready_count,
            "blocked_plan_count": len(plans) - ready_count,
            "plans": index_entries,
        }
        index_errors = validate_task_response("story_plan_index", index)
        if index_errors:
            raise ValueError(
                "invalid Story Plan Index: " + "; ".join(index_errors[:40])
            )
        index_path = output_dir / "index.json"
        atomic_write_json(index_path, index)
        review_path = root / "story-plan-review.md"
        atomic_write_text(review_path, render_review(plans, index_status))

        update_project_stage(
            root / "project.json",
            "story_plans",
            index_status,
            inputs={
                "story_plan_batch": str(manifest_path),
                "span_candidate_index": str(span_index_path),
            },
            outputs={
                "story_plan_index": str(index_path),
                "story_plan_review": str(review_path),
            },
            note=(
                f"ready={ready_count}/{len(plans)}; "
                "video QC and transitions not generated"
            ),
        )

        validate_story_plans(root, allow_partial=allow_partial)

        index_path_out = (
            root / "story-plan-candidates" / "index.json"
            if batch.get("candidate_arena") is True
            else root / "story-plans" / "index.json"
        )
        ref = bus.put("story_plans_materialized", {"path": str(index_path_out)},
                      stage="story_plans_materialize")
        update_project_stage(root / "project.json", "story_plans_materialize", "completed",
                             outputs={"story_plans_materialized": str(index_path_out)})
        return [ref]