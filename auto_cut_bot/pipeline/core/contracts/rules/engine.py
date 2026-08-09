"""合同规则引擎 — 声明式规则 + 通用 Runner。

背景: skills/*/SKILL.md 中定义了大量合同规则 (固定合同 1–36),
这些规则原来在 validate() 函数里顺序执行
(validate_story_artifacts.py)。
本模块把合同规则组织为**声明式规则表**:

  Rule = {rule_id, check_fn, severity, description, group, requires}

  - ``group``: 规则所属产物类型 (story_script / series_bible / story_plan /
    render_recipe / qc_admission ...), 用于按产物分组与过滤;
  - ``requires``: 规则运行所需的产物名集合, 缺任一产物时规则被跳过
    (记入报告 skipped, 不产生误判);
  - ``check_fn(payloads) -> Iterable[Finding]``: 纯函数, 只在数据结构上
    判定, 不做 I/O; 返回的 Finding 由 Runner 包装为现有
    ``ContractViolation`` 类型 (rule_id/severity 自动注入),
    与编排器的违规处理链路无缝对接。

设计边界:
  - 本模块只放**框架级**引擎与跨域通用规则;
    领域插件可在 ``plugins/<domain>/`` 中构造 Rule 并注册到引擎;
  - Runner 捕获单条规则内部异常并转为 ``rule_internal_error`` 违规,
    一条规则崩溃不会拖垮整个校验批次;
  - validate() 保留不动, 作为行为基准 (见 rules/legacy_story_artifacts.py
    的表化示范与 tests/unit/test_validate_rule_equivalence.py 的等价性证明)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from autocut_core.contracts.types import ContractViolation
from autocut_core.logging import fields, get_logger

logger = get_logger(__name__)

__all__ = [
    "Finding",
    "Rule",
    "RuleReport",
    "RuleEngine",
]


# ═══════════════════════════════════════════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class Finding:
    """规则 check_fn 的单条发现 — Runner 负责升级为 ContractViolation。

    check_fn 只描述"发现了什么问题", 不携带 rule_id,
    rule_id 由 Rule 声明、Runner 注入; severity 默认跟随 Rule,
    单条发现可用 Finding.severity 覆盖 (如一条规则同时产出
    error 阻断与 warning 提示时)。
    """

    code: str                       # 机器可读违规码, 如 "beat_count_out_of_range"
    message: str                    # 人类可读描述
    location: str = ""              # 产物内定位, 如 "beats[3]"
    suggestion: str = ""            # 修复建议
    severity: str = ""              # 空串 = 跟随 Rule.severity


#: check_fn 签名: 接收 {产物名: 产物数据} 映射, 返回 Finding 序列。
CheckFn = Callable[[Mapping[str, Any]], Iterable[Finding]]


@dataclass(frozen=True)
class Rule:
    """一条声明式合同规则。

    属性:
      rule_id:     全局唯一规则 ID (如 "rule_22_plan_duration_cap"),
                   与 SKILL.md 条款的对应关系见 description/source;
      check_fn:    纯数据判定函数;
      severity:    "error" | "warning";
      description: 规则判定的合同内容 (人类可读);
      group:       产物类型分组 (story_script / series_bible ...);
      requires:    运行所需产物名集合 (默认仅自身 group),
                   缺失时规则被跳过而非误报;
      source:      合同出处, 如 "SKILL.md 固定合同 rule 22"。
    """

    rule_id: str
    check_fn: CheckFn
    severity: str = "error"
    description: str = ""
    group: str = "general"
    requires: tuple[str, ...] = ()
    source: str = ""

    def __post_init__(self) -> None:
        if not self.rule_id:
            raise ValueError("Rule.rule_id must be non-empty")
        if self.severity not in ("error", "warning"):
            raise ValueError(f"Rule.severity invalid: {self.severity!r}")
        if not self.requires:
            object.__setattr__(self, "requires", (self.group,))


# ═══════════════════════════════════════════════════════════════════════════
# 报告
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class RuleReport:
    """一次规则运行的结构化报告。

    violations: 全部违规 (含规则内部错误), 与现有 ContractViolation 对接;
    evaluated:  实际执行过的 rule_id (按执行顺序);
    skipped:    因缺少所需产物而跳过的 rule_id。
    """

    violations: list[ContractViolation] = field(default_factory=list)
    evaluated: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """无 error 级违规即为通过 (warning 不阻断)。"""
        return not any(v.severity == "error" for v in self.violations)

    def errors(self) -> list[ContractViolation]:
        return [v for v in self.violations if v.severity == "error"]

    def warnings(self) -> list[ContractViolation]:
        return [v for v in self.violations if v.severity == "warning"]

    def by_rule(self) -> dict[str, list[ContractViolation]]:
        grouped: dict[str, list[ContractViolation]] = {}
        for violation in self.violations:
            grouped.setdefault(violation.rule_id, []).append(violation)
        return grouped

    def to_dict(self) -> dict[str, Any]:
        """导出为可落盘 JSON (供 *-validation.json 类产物使用)。"""
        return {
            "ok": self.ok,
            "violations": [
                {
                    "rule_id": v.rule_id,
                    "code": v.code,
                    "message": v.message,
                    "severity": v.severity,
                    "location": v.location,
                    "suggestion": v.suggestion,
                }
                for v in self.violations
            ],
            "evaluated": list(self.evaluated),
            "skipped": list(self.skipped),
        }


# ═══════════════════════════════════════════════════════════════════════════
# 引擎 (注册表 + Runner)
# ═══════════════════════════════════════════════════════════════════════════


class RuleEngine:
    """规则注册表 + 通用 Runner。

    用法::

        engine = RuleEngine()
        engine.register(Rule(rule_id="rule_22_plan_duration_cap", ...))
        report = engine.run({"story_plan": plan}, groups=["story_plan"])
    """

    def __init__(self, rules: Sequence[Rule] | None = None) -> None:
        self._rules: dict[str, Rule] = {}
        self._order: list[str] = []
        for rule in rules or []:
            self.register(rule)

    # ── 注册 ────────────────────────────────────────────────────────

    def register(self, rule: Rule) -> "RuleEngine":
        """注册一条规则; rule_id 重复时抛 ValueError (返回 self 支持链式)。"""
        if rule.rule_id in self._rules:
            raise ValueError(f"duplicate rule_id: {rule.rule_id}")
        self._rules[rule.rule_id] = rule
        self._order.append(rule.rule_id)
        return self

    def rule(self, rule_id: str) -> Rule | None:
        return self._rules.get(rule_id)

    @property
    def rule_ids(self) -> list[str]:
        """按注册顺序返回全部 rule_id。"""
        return list(self._order)

    def groups(self) -> list[str]:
        """按首次出现顺序返回全部产物分组。"""
        seen: list[str] = []
        for rule_id in self._order:
            group = self._rules[rule_id].group
            if group not in seen:
                seen.append(group)
        return seen

    def rules(
        self,
        *,
        group: str | None = None,
        groups: Sequence[str] | None = None,
        rule_ids: Sequence[str] | None = None,
        severity: str | None = None,
    ) -> list[Rule]:
        """按注册顺序过滤规则 — 分组 / rule_id / 严重级别可组合。"""
        wanted_groups = set(groups) if groups else ({group} if group else None)
        wanted_ids = set(rule_ids) if rule_ids else None
        result: list[Rule] = []
        for rule_id in self._order:
            rule = self._rules[rule_id]
            if wanted_groups is not None and rule.group not in wanted_groups:
                continue
            if wanted_ids is not None and rule.rule_id not in wanted_ids:
                continue
            if severity is not None and rule.severity != severity:
                continue
            result.append(rule)
        return result

    # ── 运行 ────────────────────────────────────────────────────────

    def run(
        self,
        artifacts: Mapping[str, Any],
        *,
        groups: Sequence[str] | None = None,
        rule_ids: Sequence[str] | None = None,
    ) -> RuleReport:
        """遍历选中规则、收集违规、生成结构化报告。

        artifacts: {产物名: 产物数据} — 规则按 requires 声明取用;
        groups/rule_ids: 可选过滤 (缺省运行全部已注册规则)。
        """
        report = RuleReport()
        for rule in self.rules(groups=groups, rule_ids=rule_ids):
            missing = [name for name in rule.requires if name not in artifacts]
            if missing:
                report.skipped.append(rule.rule_id)
                continue
            report.evaluated.append(rule.rule_id)
            try:
                findings = list(rule.check_fn(artifacts))
            except Exception as exc:  # noqa: BLE001 — 单规则崩溃不拖垮批次
                # 记录异常栈便于定位规则实现缺陷, 同时转为结构化违规
                logger.warning(
                    "规则内部异常: %s crashed: %r",
                    rule.rule_id, exc,
                    extra=fields(rule_id=rule.rule_id),
                    exc_info=True,
                )
                report.violations.append(
                    ContractViolation(
                        rule_id=rule.rule_id,
                        code="rule_internal_error",
                        message=f"rule {rule.rule_id} crashed: {exc!r}",
                        severity="error",
                        suggestion="检查规则实现; 这通常表示产物结构与规则预期不符",
                    )
                )
                continue
            for finding in findings:
                report.violations.append(
                    ContractViolation(
                        rule_id=rule.rule_id,
                        code=finding.code,
                        message=finding.message,
                        severity=finding.severity or rule.severity,
                        location=finding.location,
                        suggestion=finding.suggestion,
                    )
                )
        return report
