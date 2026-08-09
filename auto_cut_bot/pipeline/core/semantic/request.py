"""请求组装 — 从 semantic_handlers.py 提取的 request_assembly 函数组。

原位置: semantic_handlers.py, 7 funcs, ~645L
依赖: semantic_engine, semantic_backend, story_schemas, story_granularity, etc.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import urllib.parse
from pathlib import Path
from typing import Any


from autocut_core.backends._base import get_backend
from autocut_core.semantic.engine import (
    JUNCTION_CONTENT_SIGNATURE_VERSION,
    SYSTEM_PROMPT,
    sanitize_url,
)
from autocut_core.io import (
    json_sha256,
    load_json,
    sha256_file,
)

from autocut_core.semantic.granularity import (
    BROAD,
    broad_catalog_prompt,
    validate_broad_catalog,
)
from autocut_core.libs.editorial_plan import validate_option_selection
from autocut_core.schema.compat import (
    response_format,
    task_prompt,
)
from autocut_core.contracts.teaser_contract import (
    TEASER_MAXIMUM_SECONDS,
    TEASER_PREFERRED_MINIMUM_SECONDS,
)
from autocut_core.libs.script_preflight import story_script_model_findings

# 从 autocut_core.semantic.utils 导入
from autocut_core.semantic.utils import records_by_id as _records_by_id

# ── 技能/知识加载 ──────────────────────────────────────────────────────────

TASK_SKILL_MAP: dict[str, list[str]] = {
    "window_analysis": [
        "ac_source_prep/SKILL.md",
        "ac_source_prep/references/source-analysis.md",
    ],
    "episode_digest": [
        "ac_series_knowledge/SKILL.md",
        "ac_series_knowledge/references/bible-schema.md",
    ],
    "chapter_digest": [
        "ac_series_knowledge/SKILL.md",
        "ac_series_knowledge/references/bible-schema.md",
    ],
    "series_registry": [
        "ac_series_knowledge/SKILL.md",
    ],
    "series_assignment": [
        "ac_series_knowledge/SKILL.md",
    ],
    "story_catalog": [
        "ac_story_generation/SKILL.md",
        "ac_story_generation/references/portfolio-design.md",
    ],
    "story_script_draft": [
        "ac_story_generation/SKILL.md",
        "ac_story_generation/references/script-schema.md",
    ],
    "story_plan_selection": [
        "ac_plan_orchestration/SKILL.md",
        "ac_plan_orchestration/references/plan-design.md",
    ],
    "story_video_qc": [
        "ac_qc/SKILL.md",
        "ac_qc/references/qc-design.md",
        "ac_qc/references/qc-rules.json",
    ],
}


def load_skill_for_task(task: str, context: dict[str, Any] | None = None) -> str:
    """Load relevant skills/knowledge for a task and return as prompt text.

    Dynamically loads genre-specific adapters when context contains genre info.
    Returns empty string if no skills found for the task.
    """
    skill_paths = TASK_SKILL_MAP.get(task, [])
    if not skill_paths:
        return ""

    skills_root = Path(__file__).resolve().parents[2] / "skills"
    parts: list[str] = []

    for rel_path in skill_paths:
        full_path = skills_root / rel_path
        if not full_path.is_file():
            continue
        content = full_path.read_text(encoding="utf-8")
        # Trim to reasonable size per skill (~2000 chars max)
        if len(content) > 2000:
            content = content[:2000] + "\n... (truncated)"
        parts.append(f"== {rel_path} ==\n{content}")

    # Load genre-specific adapter if context has genre info
    if task in ("story_catalog", "story_script_draft") and context:
        genre = context.get("genre") or context.get("genre_profile", "")
        if genre:
            adapter_path = (
                skills_root / "ac_shared_contracts" / "references"
                / "editorial-knowledge" / f"{genre}-v1.json"
            )
            if adapter_path.is_file():
                adapter = json.loads(adapter_path.read_text(encoding="utf-8"))
                # Extract key fields (not full JSON — too large)
                summary = {
                    "genre_profile": adapter.get("genre_profile", ""),
                    "transferable_contract": adapter.get("transferable_contract", {}),
                    "approved_story_sequence": adapter.get("approved_story_sequence", [])[:3],
                }
                parts.append(
                    f"== 流派适配器: {genre} ==\n"
                    f"{json.dumps(summary, ensure_ascii=False, indent=2)}"
                )

    return "\n\n".join(parts) if parts else ""


# ── VLM 多源字幕仲裁 ──────────────────────────────────────────────────────

AGREEMENT_TYPES: list[str] = [
    "both_match",
    "minor_divergence",
    "major_divergence",
    "asr_only",
    "api_only",
    "script_only",
    "all_diverge",
    "vlm_override",
]

_MULTISOURCE_ARBITRATION_SYSTEM_PROMPT = (
    "你是短剧语义分析器。下面提供了多个数据源的字幕/对白信息。"
    "以你实际看到/听到的视频内容为准，判断每个数据源的准确性，"
    "并在 dialogue_and_text 每条记录的 source_accuracy 字段中输出仲裁结果。"
    "不要编造不存在的内容，不要偏信任一数据源。"
)

_MULTISOURCE_ARBITRATION_TASK = (
    "任务: 对每个对话事件 (dialogue_and_text)，对比 ASR、API 字幕、剧本对白三源，"
    "输出 source_accuracy 仲裁结果。\n"
    "agreement 取值: both_match (一致), minor_divergence (小差异), "
    "major_divergence (大差异), asr_only (仅ASR有), api_only (仅API有), "
    "script_only (仅剧本有), all_diverge (三源分歧), vlm_override (以VLM判断为准)。\n"
    "chosen_source 取值: asr, api, script, both, vlm。\n"
    "vlm_override_text: 当 chosen_source 为 vlm 时填写你听到的准确文本。\n"
    "reason: 简要说明仲裁理由。"
)

# 延迟导入 — 避免循环依赖
_broad_script_prompt = None


def _get_broad_script_prompt():
    global _broad_script_prompt
    if _broad_script_prompt is None:
        from autocut_core.semantic.story_logic import broad_script_prompt as _fn
        _broad_script_prompt = _fn
    return _broad_script_prompt


def media_item(
    job: dict[str, Any], max_inline_mb: float, *, encode_payload: bool = True
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    media_file = job.get("media_file")
    media_url = job.get("media_url")
    if media_file and media_url:
        raise ValueError("job cannot include both media_file and media_url")
    if isinstance(media_file, str):
        path = Path(media_file).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"media file not found: {path}")
        size = path.stat().st_size
        limit = max_inline_mb * 1024 * 1024
        if size > limit:
            raise ValueError(
                f"media file is {size / 1024 / 1024:.1f} MiB, above inline limit "
                f"{max_inline_mb:.1f} MiB"
            )
        mime, _ = mimetypes.guess_type(path.name)
        mime = mime if mime and mime.startswith("video/") else "video/mp4"
        encoded = (
            base64.b64encode(path.read_bytes()).decode("ascii")
            if encode_payload
            else f"<omitted-{size}-bytes>"
        )
        return (
            {"type": "video_url", "video_url": {"url": f"data:{mime};base64,{encoded}"}},
            {
                "mode": "inline_file",
                "reference": str(path),
                "size_bytes": size,
                "sha256": sha256_file(path),
            },
        )
    if isinstance(media_url, str):
        parsed = urllib.parse.urlsplit(media_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("media_url must be http(s)")
        return (
            {"type": "video_url", "video_url": {"url": media_url}},
            {
                "mode": "url",
                "reference": sanitize_url(media_url),
                "url_sha256": json_sha256(media_url),
            },
        )
    return None, {"mode": "none"}


def request_signature_media_identity(
    job: dict[str, Any],
    context: dict[str, Any],
    media_identity: dict[str, Any],
) -> dict[str, Any]:
    """Make Junction QC cache identity portable across Candidate workspaces.

    The full context hash and dynamic response schema remain in the outer
    request signature.  Here we remove only the local filesystem reference
    and bind the logical source ranges/effect input alongside the media bytes.
    """

    identity = dict(media_identity)
    if not (
        job.get("task") == "story_video_qc"
        and job.get("review_kind") == "junction"
    ):
        return identity
    if identity.get("mode") != "inline_file" or not isinstance(
        identity.get("sha256"), str
    ):
        return identity
    left = context.get("left_clip")
    right = context.get("right_clip")
    if not isinstance(left, dict) or not isinstance(right, dict):
        raise ValueError(
            "Junction content-addressed signature requires left/right Clip context"
        )
    identity.pop("reference", None)
    identity["content_addressing"] = {
        "version": JUNCTION_CONTENT_SIGNATURE_VERSION,
        "junction_input_id": context.get("junction_input_id"),
        "left": {
            "source_id": left.get("source_id"),
            "source_start": left.get("source_start"),
            "source_end": left.get("source_end"),
        },
        "right": {
            "source_id": right.get("source_id"),
            "source_start": right.get("source_start"),
            "source_end": right.get("source_end"),
        },
        "transition_preview": context.get("transition_preview"),
        "junction_edit": context.get("junction_edit"),
    }
    return identity


def load_context(job: dict[str, Any], max_context_chars: int) -> tuple[dict[str, Any], str]:
    path_value = job.get("context_file")
    if not isinstance(path_value, str):
        raise ValueError("job.context_file is required")
    path = Path(path_value).expanduser().resolve()
    context = load_json(path)
    if not isinstance(context, dict):
        raise ValueError(f"context must be an object: {path}")
    text = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    if len(text) > max_context_chars:
        raise ValueError(
            f"context has {len(text)} characters, above {max_context_chars}; shard the stage"
        )
    return context, text


def identity_prompt(task: str, job: dict[str, Any], context: dict[str, Any]) -> str:
    if task == "window_analysis":
        source_id = job.get("source_id")
        episode = job.get("episode")
        window_id = job.get("window_id")
        start = float(job.get("start", 0))
        end = float(job["end"])
        media_mode = (
            "完整源视频"
            if job.get("media_url_mode") == "full_source"
            else "物理连续窗口"
        )
        recovery_example = ""
        if job.get("media_url_mode") == "physical_window_recovery":
            recovery_example = (
                f"本次是失败窗口的物理媒体恢复；例如片段内 12.500 秒必须输出为"
                f"原片 {start + 12.5:.3f} 秒，禁止输出 12.500 秒，也禁止根据"
                "模型上一响应猜测或平移时间码。"
            )
        return (
            f"素材 source_id={source_id}，episode={episode}，window_id={window_id}。"
            f"输入是{media_mode}；只分析原视频绝对时间 [{start:.3f}, {end:.3f}] 秒。"
            "若输入是物理窗口，画面从 0 秒播放，但输出必须加上窗口起点。"
            "source_id、episode、window_id 和 window.start/end 必须逐字复制。"
            f"story_beats / dialogue_and_text / visual_events / candidates 中每一条的 start 和 end 都必须落在闭区间 [{start:.3f}, {end:.3f}] 秒内（允许 ±0.05 秒 rounding），且 end > start。"
            f"每条 type=highlight 的 Candidate 必须是单一连续子区间；在不截断核心表达和可见兑现的前提下，优选 {TEASER_PREFERRED_MINIMUM_SECONDS:g}–{TEASER_MAXIMUM_SECONDS:g} 秒。"
            f"若自然完整边界超过 {TEASER_MAXIMUM_SECONDS:g} 秒，保留完整范围，不要为了 Teaser 资格硬截；该 Candidate 仍可作为剧情/正文证据，但不能直接作为 Teaser。"
            f"即使在 {end:.3f} 秒之后仍观察到对白、动作、字幕或事件，也必须整条丢弃：不要写入任何字段、不要 clip 到 {end:.3f}、不要移动到别的窗口——严格视为不在本次分析范围。"
            "请在结构化输出前自检每一条时间码。核心 Story Beat、Visual Event 或 Timeline 的非法时间会触发局部纠错；"
            "非法的辅助对白/字幕或 Candidate 会被逐条隔离；有效但超过 Teaser 时长的 Highlight 会保留为剧情证据。"
            f"{recovery_example}"
        )
    if task == "story_video_qc":
        return (
            f"story_id={context.get('story_id')}，"
            f"review_id={context.get('review_id')}，"
            f"review_kind={context.get('review_kind')}。"
            "story_id、review_id 和 review_kind 必须逐字复制。"
            f"代理视频时间范围为 [0, {float(context.get('duration_seconds', 0)):.3f}] 秒。"
        )
    identities = {
        "episode_digest": f"episode={context.get('episode')}",
        "chapter_digest": f"chapter_id={context.get('chapter_id')}",
        "series_registry": "series_id=series",
        "series_registry_relationship_repair": (
            "subject_character_id="
            f"{context.get('repair_contract', {}).get('subject_character_id')}"
        ),
        "series_registry_identity_audit": (
            "subject_character_id="
            f"{context.get('audit_contract', {}).get('subject_character_id')}"
        ),
        "series_assignment": f"chapter_id={context.get('chapter_id')}",
        "story_catalog": (
            "story_inventory_policy="
            f"{context.get('story_inventory_policy', 'discover_all_evidence_backed_subarcs')}"
        ),
        "story_script_draft": f"story_id={context.get('story', {}).get('story_id')}",
        "story_plan_selection": (
            f"story_id={context.get('story_id')}, "
            f"production_slot={context.get('production_slot')}"
        ),
        "story_plan_orientation_fallback": (
            f"story_id={context.get('story_id')}, "
            f"production_slot={context.get('production_slot')}. "
            "candidate_orientations 必须覆盖动态 Schema 锁定的全部"
            " Candidate ID，不得选择 Winner。"
        ),
    }
    return identities[task]


def validate_identity(
    task: str, value: dict[str, Any], job: dict[str, Any], context: dict[str, Any]
) -> list[str]:
    errors: list[str] = []
    if task == "window_analysis":
        expected = {
            "source_id": job.get("source_id"),
            "episode": job.get("episode"),
            "window_id": job.get("window_id"),
        }
        for key, wanted in expected.items():
            if value.get(key) != wanted:
                errors.append(f"{key}: expected {wanted!r}, got {value.get(key)!r}")
        window = value.get("window")
        if isinstance(window, dict):
            for key in ("start", "end"):
                wanted = float(job[key])
                actual = window.get(key)
                if not isinstance(actual, (int, float)) or abs(float(actual) - wanted) > 0.05:
                    errors.append(f"window.{key}: expected {wanted:.3f}, got {actual!r}")
    elif task == "episode_digest":
        if value.get("episode") != context.get("episode"):
            errors.append("episode_digest.episode does not match context")
        if set(value.get("window_ids", [])) != set(context.get("window_ids", [])):
            errors.append("episode_digest.window_ids must cover the context exactly")
        if set(value.get("source_ids", [])) != set(context.get("source_ids", [])):
            errors.append("episode_digest.source_ids must match the context")
    elif task == "chapter_digest":
        if value.get("chapter_id") != context.get("chapter_id"):
            errors.append("chapter_digest.chapter_id does not match context")
        if value.get("episodes") != context.get("episodes"):
            errors.append("chapter_digest.episodes must match the context")
        if context.get("chapter_evidence_contract", {}).get(
            "all_rollup_evidence_ids_must_reference_event_index"
        ):
            allowed_event_ids = {
                item.get("id")
                for item in context.get("event_index", [])
                if isinstance(item, dict)
                and isinstance(item.get("id"), str)
            }
            referenced_event_ids = {
                event_id
                for field, evidence_field in (
                    ("character_rollup", "evidence_event_ids"),
                    ("relationship_rollup", "evidence_event_ids"),
                    ("story_threads", "event_ids"),
                )
                for item in value.get(field, []) or []
                if isinstance(item, dict)
                for event_id in item.get(evidence_field, []) or []
                if isinstance(event_id, str)
            }
            referenced_event_ids.update(
                event_id
                for event_id in value.get("event_ids", []) or []
                if isinstance(event_id, str)
            )
            unknown_event_ids = sorted(
                referenced_event_ids - allowed_event_ids
            )
            if unknown_event_ids:
                errors.append(
                    "chapter_digest references Event IDs outside its "
                    "event_index: " + ", ".join(unknown_event_ids)
                )
    elif task == "series_registry":
        if value.get("schema_version") != "1.3":
            errors.append("series_registry.schema_version must be 1.3")
    elif task == "series_registry_relationship_repair":
        expected_subject = context.get("repair_contract", {}).get(
            "subject_character_id"
        )
        if value.get("subject_character_id") != expected_subject:
            errors.append(
                "series_registry_relationship_repair.subject_character_id "
                "does not match context"
            )
    elif task == "series_registry_identity_audit":
        expected_subject = context.get("audit_contract", {}).get(
            "subject_character_id"
        )
        if value.get("subject_character_id") != expected_subject:
            errors.append(
                "series_registry_identity_audit.subject_character_id "
                "does not match context"
            )
    elif task == "series_assignment":
        if value.get("chapter_id") != context.get("chapter_id"):
            errors.append("series_assignment.chapter_id does not match context")
        if value.get("episodes") != context.get("episodes"):
            errors.append("series_assignment.episodes must match context")
    elif task == "story_catalog":
        if not value.get("stories"):
            errors.append("story_catalog must contain at least one real Story")
        if context.get("story_granularity") != BROAD:
            errors.append(
                "story_catalog context must use story_granularity=broad; "
                "Legacy Catalog jobs must be regenerated"
            )
        else:
            bible = context.get("series_bible", {})
            thread_beats = [
                item
                for item in bible.get("thread_beats", []) or []
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            ]
            validation_scope = context.get("broad_validation_scope", {})
            scoped_required = validation_scope.get(
                "required_thread_beat_ids"
            )
            scoped_non_coda = validation_scope.get(
                "non_coda_thread_beat_ids"
            )
            errors.extend(
                validate_broad_catalog(
                    value,
                    {
                        "options": context.get("subarc_options", []),
                        "required_thread_beat_ids": (
                            scoped_required
                            if isinstance(scoped_required, list)
                            else [
                                item["id"]
                                for item in thread_beats
                                if item.get("importance") == "required"
                            ]
                        ),
                        "non_coda_thread_beat_ids": (
                            scoped_non_coda
                            if isinstance(scoped_non_coda, list)
                            else [
                                item["id"]
                                for item in thread_beats
                                if item.get("phase") != "coda"
                            ]
                        ),
                    },
                    option_catalog_sha256=context.get(
                        "subarc_option_catalog_sha256"
                    ),
                )
            )
            expected_option_ids = set(
                validation_scope.get("expected_option_ids", []) or []
            )
            actual_option_ids = {
                item.get("subarc_option_id")
                for item in value.get("stories", []) or []
                if isinstance(item, dict)
            }
            if expected_option_ids and actual_option_ids != expected_option_ids:
                errors.append(
                    "story_catalog option ids must exactly match the "
                    f"single-option request: expected={sorted(expected_option_ids)}, "
                    f"actual={sorted(actual_option_ids)}"
                )
    elif task == "story_script_draft":
        if context.get("story_granularity") != BROAD:
            errors.append(
                "story_script_draft context must use story_granularity=broad; "
                "Legacy Script jobs must be regenerated from Broad Catalog"
            )
        story = context.get("story", {})
        if value.get("story_id") != story.get("story_id"):
            errors.append("story_script_draft.story_id does not match context")
        if value.get("portfolio") != context.get("portfolio_binding"):
            errors.append(
                "story_script_draft.portfolio does not match portfolio_binding"
            )
        contract = context.get("thread_beat_contract", {})
        source_ids = set(contract.get("source_thread_beat_ids", []))
        required_ids = set(contract.get("required_thread_beat_ids", []))
        selected_values = value.get("selected_thread_beat_ids", [])
        required_values = value.get("required_thread_beat_ids", [])
        selected_ids = set(selected_values)
        omitted_ids = {
            item.get("thread_beat_id")
            for item in value.get("omitted_thread_beats", [])
            if isinstance(item, dict)
        }
        if set(value.get("required_thread_beat_ids", [])) != required_ids:
            errors.append(
                "story_script_draft.required_thread_beat_ids must match the contract"
            )
        if selected_ids | omitted_ids != source_ids or selected_ids & omitted_ids:
            errors.append(
                "story_script_draft must account for every source Thread Beat "
                "exactly once as selected or omitted"
            )
        if not required_ids <= selected_ids:
            errors.append(
                "story_script_draft cannot omit required Thread Beats"
            )
        if len(selected_values) != len(selected_ids):
            errors.append(
                "Broad story_script_draft.selected_thread_beat_ids "
                "cannot contain duplicates"
            )
        if len(required_values) != len(set(required_values)):
            errors.append(
                "Broad story_script_draft.required_thread_beat_ids "
                "cannot contain duplicates"
            )
        retrieved_ids = {
            thread_beat_id
            for beat in value.get("beats", [])
            if isinstance(beat, dict)
            for thread_beat_id in beat.get("retrieval_requirements", {}).get(
                "thread_beat_ids", []
            )
            if isinstance(thread_beat_id, str)
        }
        if not selected_ids <= retrieved_ids:
            errors.append(
                "every selected Thread Beat must be referenced by a Script Beat"
            )
        event_by_id = {
            item["id"]: item
            for item in context.get("events", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        candidate_by_id = {
            item["id"]: item
            for item in context.get("candidates", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        errors.extend(
            f"story_script_draft.{code}: {message}"
            for code, message in story_script_model_findings(
                value,
                events=event_by_id,
                candidates=candidate_by_id,
                facts=_records_by_id(context.get("facts", [])),
                thread_beats=_records_by_id(
                    context.get("thread_beats", [])
                ),
            )
        )
    elif task == "story_plan_selection":
        if value.get("story_id") != context.get("story_id"):
            errors.append("story_plan_selection.story_id does not match context")
        if value.get("production_slot") != context.get("production_slot"):
            errors.append(
                "story_plan_selection.production_slot does not match context"
            )
        legal_options = context.get("legal_option_contract")
        if isinstance(legal_options, dict):
            errors.extend(validate_option_selection(value, legal_options))
        else:
            errors.append(
                "story_plan_selection context has no legal_option_contract"
            )
    elif task == "story_plan_orientation_fallback":
        if value.get("story_id") != context.get("story_id"):
            errors.append(
                "story_plan_orientation_fallback.story_id does not match context"
            )
        if value.get("production_slot") != context.get("production_slot"):
            errors.append(
                "story_plan_orientation_fallback.production_slot does not "
                "match context"
            )
        contract = context.get("orientation_fallback_contract")
        expected_ids = {
            item.get("plan_candidate_id")
            for item in contract.get("candidates", [])
            if isinstance(contract, dict) and isinstance(item, dict)
        } if isinstance(contract, dict) else set()
        actual = value.get("candidate_orientations")
        actual_ids = set(actual) if isinstance(actual, dict) else set()
        if not expected_ids or actual_ids != expected_ids:
            errors.append(
                "story_plan_orientation_fallback Candidate coverage differs "
                "from context"
            )
    elif task == "story_video_qc":
        for field in ("story_id", "review_id", "review_kind"):
            if value.get(field) != context.get(field):
                errors.append(
                    f"story_video_qc.{field} does not match context"
                )
        kind = context.get("review_kind")
        checks = value.get("checks", {})
        if isinstance(checks, dict):
            if kind in {"boundary_start", "boundary_end"}:
                for field in ("coverage", "flow"):
                    if checks.get(field) != "not_assessed":
                        errors.append(
                            f"story_video_qc.{field} must be not_assessed "
                            f"for {kind}"
                        )
                if checks.get("cut_safety") == "not_assessed":
                    errors.append(
                        f"story_video_qc.cut_safety must be assessed for {kind}"
                    )
            elif kind == "junction":
                if checks.get("coverage") != "not_assessed":
                    errors.append(
                        "story_video_qc.coverage must be not_assessed for junction"
                    )
                for field in ("flow", "cut_safety"):
                    if checks.get(field) == "not_assessed":
                        errors.append(
                            f"story_video_qc.{field} must be assessed for junction"
                        )
            elif kind == "story_flow":
                for field in ("coverage", "flow", "cut_safety"):
                    if checks.get(field) == "not_assessed":
                        errors.append(
                            f"story_video_qc.{field} must be assessed for story_flow"
                        )
        duration = context.get("duration_seconds")
        if isinstance(duration, (int, float)):
            for index, finding in enumerate(value.get("findings", [])):
                if not isinstance(finding, dict):
                    continue
                start = finding.get("proxy_start_seconds")
                end = finding.get("proxy_end_seconds")
                if (
                    not isinstance(start, (int, float))
                    or not isinstance(end, (int, float))
                    or start < 0
                    or end < start
                    or end > float(duration) + 0.1
                ):
                    errors.append(
                        f"story_video_qc.findings[{index}] has invalid proxy range"
                    )
        rank = {
            "not_assessed": 0,
            "pass": 0,
            "info": 0,
            "review": 1,
            "block": 2,
        }
        applicable = [
            item
            for item in checks.values()
            if item != "not_assessed"
        ] if isinstance(checks, dict) else []
        applicable.extend(
            finding.get("severity", "block")
            for finding in value.get("findings", [])
            if isinstance(finding, dict)
        )
        expected_overall = {
            0: "pass",
            1: "review",
            2: "block",
        }[
            max(
                (rank.get(item, 2) for item in applicable),
                default=0,
            )
        ]
        if value.get("overall_status") != expected_overall:
            errors.append(
                "story_video_qc.overall_status is inconsistent with "
                "checks/findings"
            )
        verified = value.get("verified_boundary")
        if kind in {"boundary_start", "boundary_end"}:
            if expected_overall == "pass" and verified != "yes":
                errors.append(
                    "passing boundary review must set verified_boundary=yes"
                )
            if verified == "yes" and expected_overall != "pass":
                errors.append(
                    "verified_boundary=yes requires a passing boundary review"
                )
        elif verified != "not_applicable":
            errors.append(
                "non-boundary review must set "
                "verified_boundary=not_applicable"
            )
    return errors


def build_request(
    backend_name: str,
    task: str,
    job: dict[str, Any],
    context: dict[str, Any],
    context_text: str,
    media: dict[str, Any] | None,
    response_format_value: dict[str, Any],
    *,
    temperature: float,
    max_tokens: int,
    context_injection: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], Any]:
    backend = get_backend(backend_name)
    task_instructions = task_prompt(task)
    if task == "story_catalog":
        task_instructions += broad_catalog_prompt(context)
    elif (
        task == "story_script_draft"
        and context.get("story_granularity") == BROAD
    ):
        task_instructions += _get_broad_script_prompt()(context)
    skill_knowledge = load_skill_for_task(task, context)
    prompt = "\n\n".join(
        [
            identity_prompt(task, job, context),
            task_instructions,
            skill_knowledge,
            "本次上下文是完整证据范围：\n" + context_text,
        ]
    )
    # ── 注入高置信度上下文 (角色名、场景描述) ──
    if context_injection:
        injection_parts: list[str] = []

        # ── 多源字幕仲裁模式 ──
        if task == "window_analysis" and _has_multisource_data(context_injection):
            injection_parts.append(_MULTISOURCE_ARBITRATION_SYSTEM_PROMPT)
            # 身份信息
            injection_parts.append(
                f"素材: source_id={job.get('source_id', '')}, "
                f"episode={job.get('episode', '')}, "
                f"window=[{job.get('start', 0):.3f}, {float(job.get('end', 0)):.3f}] 秒"
            )
            # 已知角色
            characters = context_injection.get("characters")
            if characters:
                lines = ["已知角色:"]
                for c in characters:
                    if isinstance(c, dict):
                        traits = c.get("traits", "")
                        lines.append(
                            f"  - {c.get('name', '')} ({c.get('role', '')})"
                            + (f": {traits}" if traits else "")
                        )
                    else:
                        lines.append(f"  - {c}")
                injection_parts.append("\n".join(lines))
            # 剧本场景
            scene = context_injection.get("scene")
            if scene and isinstance(scene, dict):
                injection_parts.append(
                    f"剧本场景: {scene.get('location', '')}, {scene.get('time_of_day', '')}, "
                    f"出场角色: {', '.join(scene.get('characters_present', []))}"
                )
            # ASR 语音识别结果
            asr_subtitles = context_injection.get("asr_subtitles")
            if asr_subtitles:
                lines = ["ASR 语音识别结果 (可能有同音字错误):"]
                for s in asr_subtitles:
                    if isinstance(s, dict):
                        lines.append(
                            f"  [{s.get('start', 0):.1f}-{s.get('end', 0):.1f}] "
                            f"{s.get('speaker', '')}: {s.get('text', '')}"
                        )
                injection_parts.append("\n".join(lines))
            # API 平台字幕
            api_subtitles = context_injection.get("api_subtitles")
            if api_subtitles:
                lines = ["API 平台字幕 (可能版本不匹配):"]
                for s in api_subtitles:
                    if isinstance(s, dict):
                        lines.append(
                            f"  [{s.get('start', 0):.1f}-{s.get('end', 0):.1f}] "
                            f"{s.get('speaker', '')}: {s.get('text', '')}"
                        )
                injection_parts.append("\n".join(lines))
            # 剧本对白
            script_dialogues = context_injection.get("script_dialogues")
            if script_dialogues:
                lines = ["剧本对白 (参考):"]
                for s in script_dialogues:
                    if isinstance(s, dict):
                        lines.append(
                            f"  [{s.get('start', 0):.1f}-{s.get('end', 0):.1f}] "
                            f"{s.get('speaker', '')}: {s.get('text', '')}"
                        )
                injection_parts.append("\n".join(lines))
            # 任务说明
            injection_parts.append(_MULTISOURCE_ARBITRATION_TASK)
        else:
            # 普通上下文注入 (角色 + 场景)
            characters = context_injection.get("characters")
            if characters:
                injection_parts.append(
                    "已知角色: "
                    + ", ".join(
                        f"{c.get('name', '')}({c.get('role', '')})" if isinstance(c, dict)
                        else str(c)
                        for c in characters
                    )
                )
            scene = context_injection.get("scene")
            if scene and isinstance(scene, dict):
                injection_parts.append(
                    f"当前场景: {scene.get('location', '')}, {scene.get('time_of_day', '')}, "
                    f"角色: {', '.join(scene.get('characters_present', []))}"
                )

        if injection_parts:
            prompt = "\n\n".join([*injection_parts, prompt])
    content: list[dict[str, Any]] = []
    if media is not None:
        content.append(media)
    content.append({"type": "text", "text": prompt})
    payload: dict[str, Any] = {
        "model": (
            backend.multimodal_model
            if media is not None
            else backend.model_for_task(task)
        ),
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        "temperature": temperature,
        "max_tokens": backend.effective_max_tokens(max_tokens),
        "response_format": response_format_value,
        "stream": False,
    }
    if backend.include_enable_thinking:
        payload["enable_thinking"] = False
    return payload, backend


def response_format_for_job(
    task: str, job: dict[str, Any]
) -> dict[str, Any]:
    custom = job.get("response_format")
    if custom is None:
        return response_format(task)
    # M3 A1: story_script_draft 加入动态 schema 白名单——mode=none
    # 推荐的 story 需要 per-job schema 把 teaser_contract.mode 锁到 const="none"，
    # 让 Qwen 无法覆盖回 single_highlight。
    if task not in {
        "series_assignment",
        "series_registry_relationship_repair",
        "series_registry_identity_audit",
        "story_plan_selection",
        "story_plan_orientation_fallback",
        "story_video_qc",
        "story_script_draft",
        "story_catalog",
    }:
        raise ValueError(
            "custom response_format is only supported for "
            "series_assignment, series_registry_relationship_repair, "
            "series_registry_identity_audit, "
            "story_plan_selection, story_plan_orientation_fallback, "
            "story_video_qc, story_script_draft and "
            "story_catalog"
        )
    if not isinstance(custom, dict) or custom.get("type") != "json_schema":
        raise ValueError("job.response_format must be a json_schema object")
    descriptor = custom.get("json_schema")
    if (
        not isinstance(descriptor, dict)
        or descriptor.get("strict") is not True
        or not isinstance(descriptor.get("name"), str)
        or not isinstance(descriptor.get("schema"), dict)
    ):
        raise ValueError(
            "job.response_format.json_schema must include name, strict=true, "
            "and schema"
        )
    return custom


def _has_multisource_data(context_injection: dict[str, Any]) -> bool:
    """Check if context_injection contains multi-source arbitration data."""
    return any(
        context_injection.get(key)
        for key in ("asr_subtitles", "api_subtitles", "script_dialogues")
    )