"""Auto-Recovery 反馈回路 — 在 auto 模式下, 被拒绝的 Story 自动回溯到上游 Stage 重新生成。

核心组件:
  - RecoveryPlan: 描述一次恢复操作 (触发阶段、目标阶段、Story ID、错误上下文)
  - RecoveryRecord: 单次 recovery 的审计记录
  - RecoveryResolver: 分析 rejection 并构建 RecoveryPlan
  - RecoveryLogger: 将 recovery 记录写入 recovery-log.json

设计文档: docs/design/auto-recovery-loop.md
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from autocut_core.io import atomic_write_json, load_json, utc_now
from autocut_core.logging import get_logger
from autocut_core.registry import _PIPELINE_ORDER

logger = get_logger(__name__)

# ── HUMAN NODE → 上游目标 Stage 的映射 ──────────────────────────────────────
# 每个 human node 被拒绝后, 回溯到哪个上游 Stage 重新生成。
# 从 _PIPELINE_ORDER 推导索引, 保证与 registry 一致。
_HUMAN_TO_TARGET: dict[str, str] = {
    "story_approval": "story_scripts",
    "story_plans_preflight": "story_scripts",       # 两级恢复: scope expansion → story_scripts
    "story_plans_qc_admission": "story_plans",
    "story_qc_review": "story_plans",                # 默认回 Plan; 部分 finding code 回 story_scripts
}

# QC review finding code → 恢复目标的路由表
# audio boundary 类 → story_plans (调整边界, 不改 script)
# content/structural 类 → story_scripts (根本性的内容问题)
QC_FINDING_ROUTES: dict[str, str] = {
    "audio-boundary-mismatch": "story_plans",
    "teaser-evidence-missing": "story_scripts",
    "beat-evidence-gap": "story_scripts",
    "cross-unit-dependency-unmet": "story_scripts",
}

DEFAULT_QC_ROUTE = "story_plans"

# 全局 recovery 上限
GLOBAL_MAX_ATTEMPTS = 2
QC_MAX_ATTEMPTS = 1  # QC 阶段 LLM 调用昂贵, 只重试一次


# ── 数据结构 ──────────────────────────────────────────────────────────────────


@dataclass
class RecoveryPlan:
    """一次恢复操作的描述。

    Attributes:
        trigger_stage: 触发恢复的 HUMAN NODE 名称
        target_stage:  要回溯到的上游 Stage
        story_ids:     需要重新处理的故事 ID 列表
        error_context: story_id → 人类可读的错误描述, 将注入 LLM prompt
        max_attempts:  此节点的最大重试次数
        attempt:       当前是第几次尝试 (0-indexed, 执行前 +1)
    """

    trigger_stage: str
    target_stage: str
    story_ids: list[str]
    error_context: dict[str, str]
    max_attempts: int = 2
    attempt: int = 0

    @property
    def exhausted(self) -> bool:
        return self.attempt >= self.max_attempts


@dataclass
class RecoveryRecord:
    """单次 recovery 的审计记录。"""

    attempt: int
    trigger_stage: str
    target_stage: str
    story_ids: list[str]
    error_context: dict[str, str]
    triggered_at: str
    outcome: str  # "approved" | "still_rejected" | "exhausted" | "error"
    details: dict[str, Any] = field(default_factory=dict)


# ── RecoveryResolver ──────────────────────────────────────────────────────────


class RecoveryResolver:
    """分析 rejection 并构建 RecoveryPlan。

    根据 human node 的类型和 rejection 的具体原因, 决定:
      - 回溯到哪个上游 Stage
      - 哪些 Story 需要重跑
      - 哪些错误上下文需要注入 LLM prompt
    """

    # ── 公共入口 ──────────────────────────────────────────────────────────

    def analyze_rejection(
        self,
        auto_result: dict[str, Any],
        human_node: str,
        *,
        job_root: Path | None = None,
        scope_expansion_done: bool = False,
    ) -> RecoveryPlan | None:
        """分析 auto 决策结果, 为被拒绝的 Story 构建恢复计划。

        Args:
            auto_result: auto.py 决策函数返回的字典
            human_node: 触发决策的 HUMAN NODE 名称
            job_root: 工作目录 (读取 artifact 文件时需要)
            scope_expansion_done: story_plans_preflight 场景下 scope expansion 是否已执行

        Returns:
            RecoveryPlan 如果有被拒绝的 Story 且未超过重试上限; None 表示无需恢复
        """
        if human_node == "story_approval":
            return self._analyze_story_approval(auto_result, job_root)
        if human_node == "story_plans_preflight":
            return self._analyze_plan_preflight(
                auto_result, job_root, scope_expansion_done=scope_expansion_done
            )
        if human_node == "story_plans_qc_admission":
            return self._analyze_qc_admission(auto_result, job_root)
        if human_node == "story_qc_review":
            return self._analyze_qc_review(auto_result, job_root)
        return None

    def resolve_target_stage(self, human_node: str) -> str:
        """将 human node 映射到上游恢复目标 Stage。

        story_qc_review 需要根据 finding code 进一步路由,
        调用方应使用 analyze_rejection 返回的 plan.target_stage。
        此方法仅提供默认映射。
        """
        return _HUMAN_TO_TARGET.get(human_node, human_node)

    def inject_error_context(self, plan: RecoveryPlan) -> dict[str, str]:
        """从 RecoveryPlan 构建 recovery context 字典, 供 Stage 执行时读取。

        返回的字典可直接注入到 PipelineConfig.recovery_context,
        各 Stage 据此决定是否只重跑被拒 Story 并注入 prior_failure_error。
        """
        return {
            "trigger_stage": plan.trigger_stage,
            "target_stage": plan.target_stage,
            "story_ids": plan.story_ids,
            "error_context": plan.error_context,
            "attempt": plan.attempt,
            "max_attempts": plan.max_attempts,
        }

    # ── story_approval → rejected (not_feasible) ─────────────────────────

    def _analyze_story_approval(
        self,
        auto_result: dict[str, Any],
        job_root: Path | None = None,
    ) -> RecoveryPlan | None:
        """分析 story_approval 中被 rejected 的 Story。

        目标: story_scripts。读取 Script 的 feasibility 信息构建错误上下文。
        """
        decisions = auto_result.get("decisions", [])
        rejected_entries = [
            d for d in decisions
            if isinstance(d, dict) and d.get("decision") == "rejected"
        ]
        if not rejected_entries:
            return None

        error_context: dict[str, str] = {}
        for entry in rejected_entries:
            sid = entry.get("story_id", "unknown")
            feasibility_status = entry.get("feasibility_status", "not_feasible")
            notes = entry.get("notes", "")

            parts = [f"Story {sid} was rejected by preflight as {feasibility_status}."]
            if notes:
                parts.append(f"Notes: {notes}.")

            # 如果 job_root 可用, 尝试读取 Script 的 feasibility 详情
            if job_root is not None:
                script_path = job_root / "story-scripts" / f"{sid}.json"
                if script_path.is_file():
                    try:
                        script = load_json(script_path)
                        feasibility = script.get("feasibility", {})
                        failure_codes = feasibility.get("failure_codes", [])
                        material_risks = feasibility.get("material_risks", [])
                        est_min = feasibility.get("estimated_source_duration_min_seconds", 0)
                        est_max = feasibility.get("estimated_source_duration_max_seconds", 0)

                        if failure_codes:
                            parts.append(f"Failure codes: {failure_codes}.")
                        if material_risks:
                            parts.append(f"Material risks: {material_risks}.")
                        parts.append(
                            f"Estimated source duration: {est_min}-{est_max}s. "
                            f"Please regenerate with a different beat structure: "
                            f"adjust beat selection, reduce estimated duration, "
                            f"or choose different thread beats to address these gaps."
                        )
                    except (OSError, ValueError, KeyError):
                        parts.append("(Unable to read detailed feasibility report.)")

            error_context[sid] = " ".join(parts)

        return RecoveryPlan(
            trigger_stage="story_approval",
            target_stage=_HUMAN_TO_TARGET["story_approval"],
            story_ids=[e.get("story_id", "unknown") for e in rejected_entries],
            error_context=error_context,
            max_attempts=GLOBAL_MAX_ATTEMPTS,
        )

    # ── story_plans_preflight → blocked ───────────────────────────────────

    def _analyze_plan_preflight(
        self,
        auto_result: dict[str, Any],
        job_root: Path | None = None,
        *,
        scope_expansion_done: bool = False,
    ) -> RecoveryPlan | None:
        """分析 plan preflight 中被 blocked 的 Story。

        两级恢复:
          - 第一级: scope expansion (本地操作), 由 auto.py 的现有逻辑处理
          - 第二级: 回到 story_scripts 重新生成

        当 scope_expansion 已执行但仍 blocked 时, 触发第二级恢复。
        """
        blocked_ids = auto_result.get("blocked_story_ids", [])
        if not blocked_ids:
            return None

        # 检查是否有 scope expansion 候选
        needs_scope_expansion = auto_result.get("needs_scope_expansion", False)
        scope_applied = auto_result.get("scope_expansion", {}).get("applied_story_ids", [])

        if needs_scope_expansion and not scope_expansion_done and scope_applied:
            # 第一级 scope expansion 已通过 post_handler 执行
            # 需要重新 preflight 以判断是否仍有 blocked
            return None

        if not scope_expansion_done and needs_scope_expansion:
            # 第一级: 不应由 recovery 处理, 由 auto.py 的 scope expansion 处理
            return None

        # 第二级: 回到 story_scripts
        error_context: dict[str, str] = {}
        for sid in blocked_ids:
            parts = [f"Story {sid} was blocked at plan preflight."]

            # 尝试从 preflight 文件读取详细信息
            if job_root is not None:
                preflight_path = job_root / "story-plan-preflight.json"
                if preflight_path.is_file():
                    try:
                        payload = load_json(preflight_path)
                        for record in payload.get("stories", []):
                            if isinstance(record, dict) and record.get("story_id") == sid:
                                failure_codes = record.get("failure_codes", [])
                                viability = record.get("treatment_viability", {})
                                if failure_codes:
                                    parts.append(f"Failure codes: {failure_codes}.")
                                if isinstance(viability, dict):
                                    parts.append(f"Treatment viability: {viability}.")
                                break
                    except (OSError, ValueError):
                        pass

            if scope_expansion_done:
                parts.append("Scope expansion was already attempted but insufficient.")
            parts.append(
                "Please regenerate the Story Script with a treatment "
                "that requires less source material or has better temporal coverage."
            )
            error_context[sid] = " ".join(parts)

        return RecoveryPlan(
            trigger_stage="story_plans_preflight",
            target_stage=_HUMAN_TO_TARGET["story_plans_preflight"],
            story_ids=blocked_ids,
            error_context=error_context,
            max_attempts=1,  # 第二级只试一次 (scope expansion 已经失败)
        )

    # ── story_plans_qc_admission → blocked ────────────────────────────────

    def _analyze_qc_admission(
        self,
        auto_result: dict[str, Any],
        job_root: Path | None = None,
    ) -> RecoveryPlan | None:
        """分析 QC admission 中被 blocked 的 Plan。

        目标: story_plans。读取 admission 文件获取 blocked reasons 和 repair routes。
        """
        blocked_ids = auto_result.get("blocked_story_ids", [])
        if not blocked_ids:
            return None

        error_context: dict[str, str] = {}
        if job_root is not None:
            admission_path = job_root / "story-plan-qc-admission.json"
            if admission_path.is_file():
                try:
                    admission = load_json(admission_path)
                    for entry in admission.get("stories", []):
                        if not isinstance(entry, dict):
                            continue
                        sid = entry.get("story_id")
                        if sid not in blocked_ids:
                            continue
                        blocked_reasons = entry.get("blocked_reasons", [])
                        repair_routes = entry.get("repair_routes", [])

                        parts = [f"Story {sid}'s Plan was blocked at QC admission."]
                        if blocked_reasons:
                            parts.append(f"Blocked reasons: {blocked_reasons}.")
                        if repair_routes:
                            parts.append(f"Suggested repair routes: {repair_routes}.")
                        parts.append(
                            "Please regenerate the Story Plan with adjusted span "
                            "selections or boundary adjustments to resolve these blocks."
                        )
                        error_context[sid] = " ".join(parts)
                except (OSError, ValueError):
                    pass

        # 对无法读取详情的 story 使用默认错误描述
        for sid in blocked_ids:
            if sid not in error_context:
                error_context[sid] = (
                    f"Story {sid}'s Plan was blocked at QC admission. "
                    "Please regenerate with adjusted span selections."
                )

        return RecoveryPlan(
            trigger_stage="story_plans_qc_admission",
            target_stage=_HUMAN_TO_TARGET["story_plans_qc_admission"],
            story_ids=blocked_ids,
            error_context=error_context,
            max_attempts=GLOBAL_MAX_ATTEMPTS,
        )

    # ── story_qc_review → drop ────────────────────────────────────────────

    def _analyze_qc_review(
        self,
        auto_result: dict[str, Any],
        job_root: Path | None = None,
    ) -> RecoveryPlan | None:
        """分析 QC review 中被 drop 的 Story。

        按 finding code 分级路由:
          - audio boundary 类 → story_plans
          - content/structural 类 → story_scripts
          - 未知 code → DEFAULT_QC_ROUTE (story_plans)
        """
        dropped_ids = auto_result.get("dropped_story_ids", [])
        if not dropped_ids:
            return None

        plans_by_target: dict[str, list[str]] = {}
        error_context: dict[str, str] = {}

        for sid in dropped_ids:
            target = DEFAULT_QC_ROUTE
            parts = [f"Story {sid} was dropped at QC review."]

            # 尝试从 QC report 读取 finding codes
            if job_root is not None:
                index_path = job_root / "story-qc" / "index.json"
                if index_path.is_file():
                    try:
                        index = load_json(index_path)
                        for report in index.get("reports", []):
                            if not isinstance(report, dict):
                                continue
                            if report.get("story_id") != sid:
                                continue
                            status = report.get("status", "")
                            parts[0] = f"Story {sid} was dropped at QC review (status={status})."

                            report_path_value = report.get("path")
                            if isinstance(report_path_value, str):
                                rp = Path(report_path_value).expanduser().resolve()
                                if rp.is_file():
                                    detail = load_json(rp)
                                    findings = detail.get("findings", [])
                                    for finding in findings:
                                        code = finding.get("code", "")
                                        if code in QC_FINDING_ROUTES:
                                            route = QC_FINDING_ROUTES[code]
                                            if route is not None:
                                                target = route
                                                break  # 取第一个匹配的

                                    finding_codes = [
                                        f.get("code", "")
                                        for f in findings
                                        if f.get("severity") in {"review", "block"}
                                    ]
                                    if finding_codes:
                                        parts.append(f"Finding codes: {finding_codes}.")
                            break
                    except (OSError, ValueError):
                        pass

            parts.append("Please regenerate to resolve these QC findings.")
            error_context[sid] = " ".join(parts)
            plans_by_target.setdefault(target, []).append(sid)

        if not plans_by_target:
            return None

        # 如果有多个 target, 取最深的那个 (最上游)
        # story_scripts (index 14) < story_plans (index 20)
        # 回 story_scripts 会覆盖所有, 回 story_plans 只覆盖 plan 级别的
        deepest_target = min(
            plans_by_target.keys(),
            key=lambda t: _stage_index(t),
        )
        all_story_ids: list[str] = []
        for ids in plans_by_target.values():
            all_story_ids.extend(ids)

        return RecoveryPlan(
            trigger_stage="story_qc_review",
            target_stage=deepest_target,
            story_ids=all_story_ids,
            error_context=error_context,
            max_attempts=QC_MAX_ATTEMPTS,
        )


# ── RecoveryLogger ────────────────────────────────────────────────────────────


class RecoveryLogger:
    """将 recovery 记录写入 recovery-log.json, 提供审计追溯。

    用法:
        logger = RecoveryLogger(job_root)
        logger.log_record(plan, outcome="recovered", details={...})
        records = logger.list_records()
    """

    def __init__(self, job_root: Path):
        self._job_root = job_root.expanduser().resolve()
        self._log_path = self._job_root / "recovery-log.json"

    def log_record(
        self,
        plan: RecoveryPlan,
        outcome: str,
        details: dict[str, Any] | None = None,
    ) -> RecoveryRecord:
        """将一次 recovery 记录写入 recovery-log.json。

        Args:
            plan: 恢复计划
            outcome: "recovered" | "still_rejected" | "exhausted" | "error"
            details: 额外上下文 (如 stages_reset, llm_calls 等)

        Returns:
            写入的 RecoveryRecord
        """
        record = RecoveryRecord(
            attempt=plan.attempt,
            trigger_stage=plan.trigger_stage,
            target_stage=plan.target_stage,
            story_ids=list(plan.story_ids),
            error_context=dict(plan.error_context),
            triggered_at=utc_now(),
            outcome=outcome,
            details=details or {},
        )
        self._append_record(record)
        return record

    def list_records(self) -> list[RecoveryRecord]:
        """读取 recovery-log.json 中的所有 recovery 记录。"""
        if not self._log_path.is_file():
            return []
        try:
            log = load_json(self._log_path)
            if not isinstance(log, dict):
                return []
            recoveries = log.get("recoveries", [])
            if not isinstance(recoveries, list):
                return []
            return [
                RecoveryRecord(
                    attempt=r.get("attempt", 0),
                    trigger_stage=r.get("trigger_stage", ""),
                    target_stage=r.get("target_stage", ""),
                    story_ids=r.get("story_ids", []),
                    error_context=r.get("error_context", {}),
                    triggered_at=r.get("triggered_at", ""),
                    outcome=r.get("outcome", "unknown"),
                    details=r.get("details", {}),
                )
                for r in recoveries
                if isinstance(r, dict)
            ]
        except (OSError, ValueError):
            return []

    def _append_record(self, record: RecoveryRecord) -> None:
        """追加一条记录到 recovery-log.json (原子写入)。"""
        if self._log_path.is_file():
            try:
                log = load_json(self._log_path)
                if not isinstance(log, dict):
                    log = self._new_log_skeleton()
            except (OSError, ValueError):
                log = self._new_log_skeleton()
        else:
            log = self._new_log_skeleton()

        recoveries = log.setdefault("recoveries", [])
        recoveries.append({
            "trigger_stage": record.trigger_stage,
            "target_stage": record.target_stage,
            "attempt": record.attempt,
            "max_attempts": 0,  # 从 record 中不可知, 保留字段
            "story_ids": record.story_ids,
            "error_context": record.error_context,
            "triggered_at": record.triggered_at,
            "outcome": record.outcome,
            "details": record.details,
        })
        log["updated_at"] = utc_now()
        atomic_write_json(self._log_path, log)

    def _new_log_skeleton(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "created_at": utc_now(),
            "recoveries": [],
        }


# ── 辅助 ──────────────────────────────────────────────────────────────────────


def _stage_index(name: str) -> int:
    """返回 Stage 在 _PIPELINE_ORDER 中的索引, 不存在时返回一个大值。"""
    try:
        return _PIPELINE_ORDER.index(name)
    except ValueError:
        return 9999


def recovery_stage_range(plan: RecoveryPlan) -> tuple[int, int]:
    """返回 recovery 需要重跑的 Stage 区间 (target_stage, trigger_stage)。

    Returns:
        (target_index, trigger_index) — 半开区间 [target, trigger)
    """
    target_idx = _stage_index(plan.target_stage)
    trigger_idx = _stage_index(plan.trigger_stage)
    return target_idx, trigger_idx


def build_recovery_error_context(
    plan: RecoveryPlan,
    story_id: str,
) -> str | None:
    """为单个 story 构建 prior_failure_error 字符串。

    从 plan.error_context 中提取对应 story 的错误描述,
    用于传递给 run_job 的 prior_failure_error 参数。
    """
    return plan.error_context.get(story_id)


__all__ = [
    "RecoveryPlan",
    "RecoveryRecord",
    "RecoveryResolver",
    "RecoveryLogger",
    "QC_FINDING_ROUTES",
    "DEFAULT_QC_ROUTE",
    "GLOBAL_MAX_ATTEMPTS",
    "QC_MAX_ATTEMPTS",
    "build_recovery_error_context",
    "recovery_stage_range",
]