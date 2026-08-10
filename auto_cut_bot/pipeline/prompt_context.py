"""Agent-native prompt context injection.

三层优先级:
  1. Agent 传参 (prompt_context 参数) → Agent 实时决定
  2. Agent 文件 (.agent-prompt-overrides.json) → Agent 提前写入
  3. 程序 fallback (load_skill_for_task) → Agent 不可用时

Agent 工作流:
  1. Read skills/ac_*/references/*.md 了解可用的 prompt 模板
  2. 根据当前 job 特点决定注入什么
  3. 调用 tool 时传 prompt_context 参数，或写入 .agent-prompt-overrides.json
  4. 跑完 stage 后检查输出质量
  5. 不满意 → 改 prompt_context → 重跑
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class PromptContext:
    """Agent-provided prompt overrides for a pipeline stage.

    When the agent provides this, the stage uses it instead of the
    hardcoded SYSTEM_PROMPT + task_prompt + load_skill_for_task().
    """

    def __init__(
        self,
        system_prompt: str | None = None,
        task_instructions: str | None = None,
        reference_keys: list[str] | None = None,
        extra_context: dict[str, Any] | None = None,
    ):
        self.system_prompt = system_prompt
        self.task_instructions = task_instructions
        self.reference_keys = reference_keys or []
        self.extra_context = extra_context or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "system_prompt": self.system_prompt,
            "task_instructions": self.task_instructions,
            "reference_keys": self.reference_keys,
            "extra_context": self.extra_context,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PromptContext:
        return cls(
            system_prompt=data.get("system_prompt"),
            task_instructions=data.get("task_instructions"),
            reference_keys=data.get("reference_keys", []),
            extra_context=data.get("extra_context", {}),
        )

    @classmethod
    def from_json_str(cls, json_str: str) -> PromptContext:
        return cls.from_dict(json.loads(json_str))

    def is_empty(self) -> bool:
        return not any([
            self.system_prompt,
            self.task_instructions,
            self.reference_keys,
            self.extra_context,
        ])


def resolve_prompt_context(
    task: str,
    prompt_context_json: str | None = None,
    job_root: str | None = None,
    context: dict[str, Any] | None = None,
) -> PromptContext:
    """Resolve prompt context with three-tier fallback.

    Priority:
      1. prompt_context_json (agent passed as parameter)
      2. .agent-prompt-overrides.json in job_root (agent wrote to file)
      3. load_skill_for_task() (programmatic fallback)

    Returns PromptContext — never None. If all tiers are empty,
    returns an empty PromptContext (caller uses hardcoded defaults).
    """
    # Tier 1: agent passed as parameter
    if prompt_context_json:
        pc = PromptContext.from_json_str(prompt_context_json)
        if not pc.is_empty():
            return pc

    # Tier 2: agent wrote to file
    if job_root:
        overrides_path = Path(job_root) / ".agent-prompt-overrides.json"
        if overrides_path.is_file():
            try:
                data = json.loads(overrides_path.read_text(encoding="utf-8"))
                task_overrides = data.get(task) or data.get("_default") or {}
                if task_overrides:
                    pc = PromptContext.from_dict(task_overrides)
                    if not pc.is_empty():
                        return pc
            except (json.JSONDecodeError, KeyError):
                pass

    # Tier 3: programmatic fallback
    skill_content = _load_skill_fallback(task, context)
    if skill_content:
        return PromptContext(
            task_instructions=skill_content,
            reference_keys=[],
        )

    return PromptContext()


def _load_skill_fallback(
    task: str, context: dict[str, Any] | None = None
) -> str:
    """Programmatic fallback: load skill/reference content for a task.

    Mirrors autocut_core.semantic.request.load_skill_for_task().
    """
    # Map task names to skill files (same as TASK_SKILL_MAP in request.py)
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

    skill_paths = TASK_SKILL_MAP.get(task, [])
    if not skill_paths:
        return ""

    # Try to find skills from ac_auto_cut project
    # _PROJECT_ROOT in autocut_core is autocut_core/../ = ac_auto_cut/
    try:
        from autocut_core.semantic.request import load_skill_for_task as _core_load
        return _core_load(task, context)
    except ImportError:
        pass

    # Fallback: search relative to auto_cut_bot
    skills_root = Path(__file__).resolve().parents[2] / "skills"
    parts: list[str] = []
    for rel_path in skill_paths:
        full_path = skills_root / rel_path
        if full_path.is_file():
            content = full_path.read_text(encoding="utf-8")
            if len(content) > 2000:
                content = content[:2000] + "\n... (truncated)"
            parts.append(f"== {rel_path} ==\n{content}")

    return "\n\n".join(parts) if parts else ""


def write_agent_overrides(
    job_root: str,
    task: str,
    system_prompt: str | None = None,
    task_instructions: str | None = None,
    reference_keys: list[str] | None = None,
    extra_context: dict[str, Any] | None = None,
) -> Path:
    """Write agent prompt overrides to job_root/.agent-prompt-overrides.json.

    Agent can call this before running a stage to set custom prompts.
    The file persists across tool calls — clear it when done iterating.
    """
    overrides_path = Path(job_root) / ".agent-prompt-overrides.json"

    # Read existing
    existing: dict[str, Any] = {}
    if overrides_path.is_file():
        try:
            existing = json.loads(overrides_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass

    # Merge
    existing[task] = {
        "system_prompt": system_prompt,
        "task_instructions": task_instructions,
        "reference_keys": reference_keys or [],
        "extra_context": extra_context or {},
    }

    overrides_path.write_text(
        json.dumps(existing, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return overrides_path


def clear_agent_overrides(job_root: str) -> None:
    """Remove agent prompt overrides file."""
    overrides_path = Path(job_root) / ".agent-prompt-overrides.json"
    if overrides_path.is_file():
        overrides_path.unlink()


def is_cache_valid(
    job_root: str,
    script_sha: str,
    expected_count: int | None = None,
    force_reparse: bool = False,
) -> bool:
    """Check if cached script parse result is valid.

    Returns False (cache invalid) if:
    - force_reparse is True
    - Cached episodes < expected_count * 0.5 (clearly truncated)
    - parse_meta status is "parse_error" or "failed"
    - Cache file is missing or corrupt

    This prevents the "45-episode script cached as 2 episodes" bug.
    """
    if force_reparse:
        return False

    cache_path = Path(job_root) / ".sd-cache" / "source_script" / f"{script_sha[:16]}.json"
    if not cache_path.is_file():
        return False

    try:
        import json as _json
        cached = _json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, _json.JSONDecodeError):
        return False

    # Check parse status
    meta = cached.get("_parse_meta") or cached.get("parse_metadata") or {}
    if meta.get("status") in ("parse_error", "failed"):
        return False

    # Check for truncation
    episodes = cached.get("episodes", [])
    if expected_count and len(episodes) < expected_count * 0.5:
        return False  # Clearly truncated — less than half the expected count

    return True


def invalidate_cache(job_root: str, script_sha: str) -> bool:
    """Remove a cached parse result. Returns True if cache was deleted."""
    cache_path = Path(job_root) / ".sd-cache" / "source_script" / f"{script_sha[:16]}.json"
    if cache_path.is_file():
        cache_path.unlink()
        return True
    return False