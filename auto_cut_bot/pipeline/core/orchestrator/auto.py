"""auto 模式决策函数 — 四个人工节点的自动裁决。

  - auto_process_story_approval   — Story 审批 (human node: story_approval)
  - auto_process_plan_selection   — Plan 选择 (human node: story_plans_preflight)
  - auto_process_scope_expansion  — 范围扩展 (F4, plan_preflight 触发)
  - auto_process_qc_report        — QC 报告裁决 (human node: story_qc_review)

产生变更的操作通过 CLI 子进程执行, 保持审计轨迹与 interactive
流程完全相同; 只读判断则直接在本地完成。
每个函数返回结构化记录（含 ``log_lines``）供编排器原样落日志。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from autocut_core.errors import AutoDecisionError
from autocut_core.io import load_json, utc_now

# 变更类操作委托的 CLI 脚本目录
_LEGACY_SCRIPTS = (
    Path(__file__).resolve().parents[2]
    / "_legacy_v4"
    / "scripts"
)
PYTHON = sys.executable


def _run(cmd: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """运行 CLI 子进程并捕获输出; 非零退出抛 AutoDecisionError。"""
    result = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AutoDecisionError(
            "auto helper subprocess failed: "
            + " ".join(cmd)
            + f"\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
    return result
from autocut_core.libs.scope_expander import expand as expand_story_scope

# AutoDecisionError 定义在统一异常体系 errors.py 中,
# 上方导入即为重新导出 — 既有导入路径 (orchestrator.auto) 保持兼容。


# QC review 白名单 — 命中这些 finding code 的 review 状态可自动渲染
# 与 autocut_core/libs/qc_review_policy.py
# 中的同名常量同步维护 (防漂移测试: tests/unit/test_review_policy_sync.py)
AUTO_RENDERABLE_REVIEW_FINDING_CODES = frozenset(
    {
        "local-audio-fade-fallback-source_start",
        "local-audio-fade-fallback-source_end",
    }
)


# ── 内部工具 ──────────────────────────────────────────────────────────────


def _script_feasibility_status(script_path: Path) -> str:
    """读取 Story Script 的可行性状态 (feasible/partial/not_feasible)。

    文件缺失或状态值非法时抛 AutoDecisionError — 审批决策不能基于
    不可信数据做出。
    """
    if not script_path.is_file():
        raise AutoDecisionError(f"missing Story Script: {script_path}")
    script = load_json(script_path)
    feasibility = script.get("feasibility") or {}
    status = feasibility.get("status")
    if status not in {"feasible", "partial", "not_feasible"}:
        raise AutoDecisionError(
            f"{script_path}: unknown feasibility.status={status!r}"
        )
    return status


def _auto_review_render_decision(report: dict[str, Any]) -> tuple[bool, str]:
    """判断 review 状态的 QC 报告能否自动渲染 (返回 (可否, 原因))。

    与 autocut_core/libs/qc_review_policy.py
    中的 auto_review_render_decision 同步维护。
    """
    status = report.get("status")
    if status == "approved":
        return True, "approved"
    if status != "review":
        return False, f"status={status}"
    material_findings = [
        item
        for item in report.get("findings", [])
        if isinstance(item, dict)
        and item.get("severity") in {"review", "block"}
    ]
    if not material_findings:
        return False, "review has no typed render-safe finding"
    if any(item.get("severity") == "block" for item in material_findings):
        return False, "review contains a blocking finding"
    codes = {
        str(item.get("code") or "") for item in material_findings
    }
    unsafe_codes = sorted(
        code
        for code in codes
        if code not in AUTO_RENDERABLE_REVIEW_FINDING_CODES
    )
    if unsafe_codes:
        return False, f"review requires human decision: {unsafe_codes}"
    return True, f"render-safe review findings: {sorted(codes)}"


# ── 1. Story 审批 ─────────────────────────────────────────────────────────


def auto_process_story_approval(
    job_root: Path,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Story 审批自动决策（human node: story_approval）。

    - feasibility=feasible → approved（无风险）
    - feasibility=partial  → approved --accept-risks（附说明）
    - feasibility=not_feasible → rejected

    变更仍通过 ``story_approval.py decide`` CLI 执行, 保持审计一致。
    """
    approval_path = job_root / "story-approval.json"
    if not approval_path.is_file():
        raise AutoDecisionError(f"missing approval file: {approval_path}")
    approval = load_json(approval_path)
    decisions: list[dict[str, Any]] = []
    log_lines: list[str] = []
    for entry in approval.get("stories", []):
        story_id = entry.get("story_id")
        script_path_value = entry.get("script_path")
        if not isinstance(story_id, str) or not isinstance(script_path_value, str):
            continue
        script_path = Path(script_path_value).expanduser().resolve()
        feasibility_status = _script_feasibility_status(script_path)
        if feasibility_status == "feasible":
            decision = "approved"
            accept_risks = False
            notes = None
        elif feasibility_status == "partial":
            decision = "approved"
            accept_risks = True
            notes = (
                f"auto mode: {feasibility_status} risks accepted "
                "based on preflight report"
            )
        else:  # not_feasible
            decision = "rejected"
            accept_risks = False
            script_raw = load_json(script_path)
            failure_codes = (
                script_raw.get("feasibility", {}).get("failure_codes", []) or []
            )
            needs_regen = any(
                str(code).startswith("needs_regeneration")
                for code in failure_codes
            )
            if needs_regen:
                notes = (
                    "auto mode: needs_regeneration — P2 detected class-B "
                    "semantic gap(s) or class-A deletion(s); "
                    "re-run story_script_draft (see auto_detected_semantic_gaps)"
                )
            else:
                notes = "auto mode: preflight marks story as not_feasible"
        record = {
            "story_id": story_id,
            "feasibility_status": feasibility_status,
            "decision": decision,
            "accept_risks": accept_risks,
            "notes": notes,
        }
        decisions.append(record)
        log_lines.append(
            f"[{utc_now()}] [approval] {story_id} decision={decision} "
            f"feasibility={feasibility_status} "
            f"risks_accepted={accept_risks}"
        )
        if dry_run:
            continue
        cmd = [
            PYTHON,
            str(_LEGACY_SCRIPTS / "story_approval.py"),
            "decide",
            str(approval_path),
            story_id,
            decision,
        ]
        if accept_risks:
            cmd.append("--accept-risks")
        if notes:
            cmd.extend(["--notes", notes])
        _run(cmd)
    return {"decisions": decisions, "log_lines": log_lines}


# ── 2. Plan 选择 ──────────────────────────────────────────────────────────


def auto_process_plan_selection(
    job_root: Path,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Plan 选择自动决策（human node: story_plans_preflight）。

    读取 ``story-plan-preflight.json``:
    - status=ready 的 Story 进入后续 Plan 生成;
    - 其余记录为 blocked, 并识别需要 scope 扩展（F4）的 Story
      （repair_route=story_scope 或素材不足类失败码）。

    返回 ``{"selected_story_ids", "blocked_story_ids",
    "needs_scope_expansion", "log_lines"}``。
    """
    preflight_path = job_root / "story-plan-preflight.json"
    if not preflight_path.is_file():
        raise AutoDecisionError(f"missing plan preflight report: {preflight_path}")
    payload = load_json(preflight_path)

    selected: list[str] = []
    blocked: list[str] = []
    needs_scope_expansion = False
    log_lines: list[str] = []
    for record in payload.get("stories", []):
        if not isinstance(record, dict):
            continue
        story_id = record.get("story_id")
        if not isinstance(story_id, str):
            continue
        status = record.get("status")
        if status == "ready":
            selected.append(story_id)
            log_lines.append(
                f"[{utc_now()}] [plan_selection] {story_id} ready → proceed"
            )
            continue
        blocked.append(story_id)
        failure_codes = {
            str(item)
            for item in (record.get("failure_codes", []) or [])
            if isinstance(item, str)
        }
        viability = record.get("treatment_viability", {})
        if isinstance(viability, dict):
            failure_codes |= {
                str(item)
                for item in (viability.get("failure_codes", []) or [])
                if isinstance(item, str)
            }
        scope_route = (
            record.get("repair_route") == "story_scope"
            or (
                isinstance(viability, dict)
                and viability.get("repair_route") == "story_scope"
            )
            or "insufficient_material" in failure_codes
        )
        if scope_route:
            needs_scope_expansion = True
        log_lines.append(
            f"[{utc_now()}] [plan_selection] {story_id} status={status} → blocked"
            + (" (scope expansion candidate)" if scope_route else "")
        )
    if dry_run:
        log_lines.append(
            f"[{utc_now()}] [plan_selection] DRY-RUN selected={len(selected)} "
            f"blocked={len(blocked)}"
        )
    return {
        "selected_story_ids": selected,
        "blocked_story_ids": blocked,
        "needs_scope_expansion": needs_scope_expansion,
        "log_lines": log_lines,
    }


# ── 3. 范围扩展 (F4) ──────────────────────────────────────────────────────


def auto_process_scope_expansion(
    job_root: Path,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """范围扩展自动决策（F4, 素材总量不足时由 plan_preflight 触发）。

    运行 ``expand_story_scope.py <job_root> --source plan_preflight --apply``,
    不批准草稿 Script（批准后必须重新 preflight）。
    返回 ``{"applied_story_ids", "log_lines"}``。
    """
    expansion_path = job_root / "story-scope-expansion.json"
    log_lines: list[str] = []
    if dry_run:
        log_lines.append(
            f"[{utc_now()}] [f4_expand] DRY-RUN would run "
            "expand_story_scope --apply"
        )
        return {"applied_story_ids": [], "log_lines": log_lines}

    _run(
        [
            PYTHON,
            str(_LEGACY_SCRIPTS / "expand_story_scope.py"),
            str(job_root),
            "--source",
            "plan_preflight",
            "--apply",
        ]
    )
    if not expansion_path.is_file():
        raise AutoDecisionError(
            f"expand_story_scope did not produce {expansion_path}"
        )
    expansion = load_json(expansion_path)
    applied_ids = list(expansion.get("applied_story_ids", []))
    log_lines.append(
        f"[{utc_now()}] [f4_expand] applied to {applied_ids or 'none'}"
    )
    return {"applied_story_ids": applied_ids, "log_lines": log_lines}


# ── 4. QC 报告裁决 ────────────────────────────────────────────────────────


def auto_process_qc_report(job_root: Path) -> dict[str, Any]:
    """QC 报告自动决策（human node: story_qc_review）。

    - approved → 进入渲染
    - review   → 仅当全部 material findings 命中 auto-render-safe 白名单时渲染
    - blocked/其他 → 丢弃, 不渲染

    返回 ``{"render_story_ids", "dropped_story_ids", "log_lines"}``。
    """
    index_path = job_root / "story-qc" / "index.json"
    log_lines: list[str] = []
    if not index_path.is_file():
        raise AutoDecisionError(f"missing QC index: {index_path}")
    index = load_json(index_path)
    render: list[str] = []
    dropped: list[str] = []
    for report in index.get("reports", []):
        story_id = report.get("story_id")
        status = report.get("status")
        if not isinstance(story_id, str):
            continue
        if status == "approved":
            render.append(story_id)
            log_lines.append(
                f"[{utc_now()}] [qc_report] {story_id} approved → render"
            )
        elif status == "review":
            report_path_value = report.get("path")
            report_value: dict[str, Any] | None = None
            if isinstance(report_path_value, str):
                report_path = Path(report_path_value).expanduser().resolve()
                if report_path.is_file():
                    loaded = load_json(report_path)
                    if isinstance(loaded, dict):
                        report_value = loaded
            if report_value is None:
                dropped.append(story_id)
                log_lines.append(
                    f"[{utc_now()}] [qc_report] {story_id} review → drop "
                    "(typed QC report unavailable)"
                )
                continue
            allowed, reason = _auto_review_render_decision(report_value)
            if allowed:
                render.append(story_id)
                log_lines.append(
                    f"[{utc_now()}] [qc_report] {story_id} review → render "
                    f"({reason})"
                )
            else:
                dropped.append(story_id)
                log_lines.append(
                    f"[{utc_now()}] [qc_report] {story_id} review → drop "
                    f"({reason})"
                )
        else:  # blocked / other
            dropped.append(story_id)
            log_lines.append(
                f"[{utc_now()}] [qc_report] {story_id} status={status} → drop"
            )
    return {
        "render_story_ids": render,
        "dropped_story_ids": dropped,
        "log_lines": log_lines,
    }


# ── 自动节点处理器注册表 ───────────────────────────────────────────────────
# 每个 human node 映射到 (handler_name, post_handler_name | None)。
# 处理器名按字符串存储, 调用时通过 getattr 动态解析,
# 保证 monkeypatch 等测试技术能正确拦截。
# 新增 human node 只需在此注册 + 实现 handler 函数, 无需修改编排器。

_AUTO_NODE_HANDLERS: dict[str, tuple[str, str | None]] = {
    "story_approval": ("auto_process_story_approval", None),
    "story_plans_preflight": ("auto_process_plan_selection", "auto_process_scope_expansion"),
    "story_plans_qc_admission": ("auto_process_plan_selection", None),
    "story_qc_review": ("auto_process_qc_report", None),
}


def auto_node_handler(name: str) -> tuple[Any, Any | None]:
    """返回 human node 的自动处理器 (handler, post_handler | None)。

    通过 getattr 动态解析处理器函数, 保证测试时 monkeypatch 能正确拦截。
    未注册的 name 抛出 KeyError — 调用方负责捕获并转为 StageExecutionError。
    """
    import autocut_core.orchestrator.auto as _mod

    handler_name, post_name = _AUTO_NODE_HANDLERS[name]
    handler = getattr(_mod, handler_name)
    post_handler = getattr(_mod, post_name) if post_name else None
    return handler, post_handler


__all__ = [
    "AutoDecisionError",
    "AUTO_RENDERABLE_REVIEW_FINDING_CODES",
    "_AUTO_NODE_HANDLERS",
    "auto_node_handler",
    "auto_process_story_approval",
    "auto_process_plan_selection",
    "auto_process_scope_expansion",
    "auto_process_qc_report",
]
