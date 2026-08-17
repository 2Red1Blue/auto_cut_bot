"""span_candidates Stage — 从证据包编译时间跨度 (Span) 候选。

输入: story_evidence
输出: span_candidates (span-candidates/index.json)
"""

from __future__ import annotations

from pathlib import Path

from autocut_core import (
    ArtifactBus, Artifact, Stage, StageContract, Task,
)
from autocut_core.contracts.evidence_validation import validate as validate_story_evidence
from autocut_core.contracts.span_validation import validate as validate_span_candidates
from autocut_core.io import (
    atomic_write_json, atomic_write_text, load_json, sha256_file, update_project_stage,
)
from autocut_core.logging import get_logger
from autocut_core.libs.span_compiler import (
    SPAN_COMPILER_METHOD,
    build_bundle,
    render_review,
    span_filename,
)
from autocut_core.schema.compat import validate_task_response


logger = get_logger(__name__)


class SpanCandidatesStage(Stage):
    """Span Candidates — 从证据包编译时间跨度候选。"""

    @property
    def contract(self) -> StageContract:
        return StageContract(
            stage_name="span_candidates",
            input_artifacts=["story_evidence", "scene_boundaries"],
            output_artifacts=["span_candidates"],
            description="编译 Span 候选",
            db_reads=["boundaries", "episodes", "shots"],
            db_writes=[],
        )

    def prepare(self, bus: ArtifactBus) -> list[Task]:
        return [Task(type="compile", payload={
            "evidence_index": self.resolve_artifact_path(bus, "story_evidence", "story_evidence"),
        })]

    def execute(self, bus: ArtifactBus, tasks: list[Task]) -> list[Artifact]:
        cfg = self.config
        if cfg.job_root is None:
            raise RuntimeError("job_root 未设置")
        root: Path = cfg.job_root
        p = tasks[0].payload

        evidence_index_path = Path(p["evidence_index"])
        evidence_report = validate_story_evidence(root)
        if not evidence_report["ok"]:
            raise ValueError(
                "Story Evidence validation failed: "
                + "; ".join(evidence_report["errors"][:30])
            )
        if not evidence_index_path.is_file():
            raise FileNotFoundError(evidence_index_path)
        evidence_index = load_json(evidence_index_path)
        index_errors = validate_task_response("story_evidence_index", evidence_index)
        if index_errors:
            raise ValueError(
                "invalid Story Evidence Index: " + "; ".join(index_errors[:30])
            )
        evidence_index_sha256 = sha256_file(evidence_index_path)

        # 尝试加载 scene_boundaries（graceful fallback）
        scene_cut_points_by_source: dict[str, list[float]] | None = None
        try:
            scene_boundaries = None
            # 优先从 bus 获取
            scene_ref = bus.latest("scene_boundaries")
            if scene_ref is not None:
                scene_boundaries = bus.get(scene_ref)
            # 回退到文件系统
            elif (root / "scene_boundaries.json").is_file():
                scene_boundaries = load_json(root / "scene_boundaries.json")

            if scene_boundaries and "episodes" in scene_boundaries:
                from autocut_core.semantic.scene_boundary_fusion import extract_cut_points
                scene_cut_points_by_source = {}
                # 从source_manifest读取episode→source_id映射，不硬编码source-XXX格式
                source_episode_map: dict[str, str] = {}
                # 兼容两种文件名：source_manifest.json（下划线，官方命名）和source-manifest.json（中划线，旧版本）
                source_manifest_path = root / "source_manifest.json"
                if not source_manifest_path.is_file():
                    source_manifest_path = root / "source-manifest.json"
                if source_manifest_path.is_file():
                    try:
                        source_manifest = load_json(source_manifest_path)
                        source_list = source_manifest.get("sources", source_manifest) if isinstance(source_manifest, dict) else source_manifest
                        for src in source_list:
                            if isinstance(src, dict) and src.get("id") and isinstance(src.get("episode"), int):
                                source_episode_map[str(src["episode"])] = src["id"]
                    except Exception as exc:
                        logger.warning("span_candidates: 加载source-manifest失败，回退到默认命名: %s", exc)
                for ep_id, ep_scenes in scene_boundaries["episodes"].items():
                    cut_points = extract_cut_points(ep_scenes)
                    if cut_points:
                        # 优先使用manifest映射，缺失时才回退到f"source-{ep_num:03d}"
                        source_id = source_episode_map.get(str(ep_id))
                        if source_id is None:
                            try:
                                ep_num = int(ep_id)
                                source_id = f"source-{ep_num:03d}"
                                logger.debug("span_candidates: ep %s 未在manifest中，使用默认: %s", ep_id, source_id)
                            except (ValueError, TypeError):
                                logger.warning(
                                    "span_candidates: episode_id '%s' 无法解析，跳过",
                                    ep_id,
                                )
                                continue
                        scene_cut_points_by_source[source_id] = cut_points
                if scene_cut_points_by_source:
                    logger.info(
                        "span_candidates: 加载场景边界 %d 个 source",
                        len(scene_cut_points_by_source),
                    )
        except Exception as exc:
            logger.warning(
                "span_candidates: 加载场景边界失败 (非阻塞): %s", exc
            )

        output_dir = root / "span-candidates"
        output_dir.mkdir(parents=True, exist_ok=True)
        bundles: list[dict] = []
        index_entries: list[dict] = []
        # 从 config.yaml 的 fusion 段读取 lead_in/lead_out 参数
        fusion_cfg = cfg.extra.get("fusion", {})
        default_compiler = dict(
            anchor_merge_gap=1.5,
            tight_padding=0.75,
            scene_padding=1.5,
            reaction_tail=4.0,
            maximum_context_extension=45.0,
            maximum_span_seconds=180.0,
            lead_in=float(fusion_cfg.get("lead_in", 0.3)),
            lead_out=float(fusion_cfg.get("lead_out", 0.0)),
        )

        for entry in sorted(
            evidence_index["packets"],
            key=lambda item: int(item["production_slot"]),
        ):
            packet_path = Path(entry["path"]).expanduser().resolve()
            if not packet_path.is_file():
                raise FileNotFoundError(packet_path)
            packet_sha256 = sha256_file(packet_path)
            if packet_sha256 != entry["packet_sha256"]:
                raise ValueError(
                    f"Story Evidence Packet is stale: {entry['story_id']}"
                )
            packet = load_json(packet_path)
            packet_errors = validate_task_response("story_evidence_packet", packet)
            if packet_errors:
                raise ValueError(
                    f"invalid Story Evidence Packet {packet_path}: "
                    + "; ".join(packet_errors[:30])
                )
            if packet["status"] == "incomplete":
                continue
            bundle = build_bundle(
                packet,
                evidence_index_sha256=evidence_index_sha256,
                evidence_packet_sha256=packet_sha256,
                scene_cut_points_by_source=scene_cut_points_by_source,
                **default_compiler,
            )
            bundle_path = output_dir / span_filename(packet["story_id"])
            atomic_write_json(bundle_path, bundle)
            bundles.append(bundle)
            index_entries.append(
                {
                    "story_id": bundle["story_id"],
                    "title": bundle["title"],
                    "production_slot": bundle["production_slot"],
                    "status": bundle["status"],
                    "path": str(bundle_path),
                    "bundle_sha256": sha256_file(bundle_path),
                    "story_evidence_packet_sha256": packet_sha256,
                    "candidate_count": len(bundle["candidates"]),
                }
            )

        if not bundles:
            raise ValueError("Story Evidence contains no Story eligible for Span compilation")
        if any(bundle["status"] == "incomplete" for bundle in bundles):
            index_status = "incomplete"
        elif any(bundle["status"] == "needs_video_review" for bundle in bundles):
            index_status = "needs_video_review"
        else:
            index_status = "ready"
        candidate_reference_count = sum(len(bundle["candidates"]) for bundle in bundles)
        unique_ids = {
            item["span_candidate_id"]
            for bundle in bundles
            for item in bundle["candidates"]
        }
        index = {
            "schema_version": "1.2",
            "method": SPAN_COMPILER_METHOD,
            "status": index_status,
            "story_evidence_index_sha256": evidence_index_sha256,
            "story_count": len(bundles),
            "candidate_reference_count": candidate_reference_count,
            "unique_span_candidate_count": len(unique_ids),
            "bundles": index_entries,
        }
        schema_errors = validate_task_response("span_candidate_index", index)
        if schema_errors:
            raise ValueError(
                "invalid Span Candidate Index: " + "; ".join(schema_errors[:40])
            )
        index_path = output_dir / "index.json"
        atomic_write_json(index_path, index)
        review_path = root / "span-candidate-review.md"
        atomic_write_text(review_path, render_review(bundles))

        update_project_stage(
            root / "project.json",
            "span_candidates",
            (
                "ready_for_story_plan"
                if index_status != "incomplete"
                else "span_candidates_incomplete"
            ),
            inputs={"story_evidence_index": str(evidence_index_path)},
            outputs={
                "span_candidate_index": str(index_path),
                "span_candidate_review": str(review_path),
            },
            note=(
                f"status={index_status}; verified_boundaries=0; "
                "Story Plan not generated"
            ),
        )

        validate_span_candidates(root)
        ref = bus.put("span_candidates", {"path": str(index_path)}, stage="span_candidates")
        return [ref]