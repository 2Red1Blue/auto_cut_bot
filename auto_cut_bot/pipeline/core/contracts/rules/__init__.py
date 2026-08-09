"""contracts/rules/ — 声明式合同规则引擎与规则表。

模块组成:
  - engine.py:                  Rule / Finding / RuleReport / RuleEngine
                                (注册、分组过滤、遍历收集、结构化报告);
  - builtin.py:                 SKILL.md 固定合同的 15 条声明式落地规则;
  - legacy_story_artifacts.py:  validate() 5 组检查的表化示范。

快速开始::

    from autocut_core.contracts.rules import default_engine

    engine = default_engine()
    report = engine.run({"story_script": script}, groups=["story_script"])
    if not report.ok:
        for violation in report.errors():
            ...

与现有链路的对接: RuleReport.violations 即 ``list[ContractViolation]``,
可直接并入 Stage.validate() 的返回值 / StageResult.violations。
"""

from __future__ import annotations

from autocut_core.contracts.rules.builtin import BUILTIN_RULES
from autocut_core.contracts.rules.engine import (
    Finding,
    Rule,
    RuleEngine,
    RuleReport,
)
from autocut_core.contracts.rules.legacy_story_artifacts import (
    LEGACY_VALIDATION_RULES,
    TARGET_KEY,
)

__all__ = [
    "Finding",
    "Rule",
    "RuleEngine",
    "RuleReport",
    "BUILTIN_RULES",
    "LEGACY_VALIDATION_RULES",
    "TARGET_KEY",
    "default_engine",
]


def default_engine(*, include_legacy_demo: bool = False) -> RuleEngine:
    """构造内置规则引擎。

    include_legacy_demo=True 时同时注册 validate() 表化示范规则
    (消费 ``validate_target`` 产物)。
    """
    rules = list(BUILTIN_RULES)
    if include_legacy_demo:
        rules.extend(LEGACY_VALIDATION_RULES)
    return RuleEngine(rules)
