"""Stage 自动发现 — 启动时扫描并注册全部流水线 Stage。

发现源与优先级:
  1. 文件扫描 (主要机制) — 遍历 plugins/*/stages/*/stage.py
  2. entry_points (辅助) — 正式安装后从 pyproject.toml 扫描

同名冲突: 文件扫描优先 — entry_points 不会覆盖文件扫描发现的同名 Stage。

发现失败处理: 记录警告日志 (含异常栈), 不静默跳过; 后续由
_validate_pipeline_order() 汇总报告 _PIPELINE_ORDER 中缺失的 Stage。
"""

from __future__ import annotations

import importlib
import sys
from importlib.metadata import entry_points
from pathlib import Path
from typing import TYPE_CHECKING

from autocut_core.logging import get_logger

if TYPE_CHECKING:
    from autocut_core.stages._base import Stage

logger = get_logger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 22+ 个 Stage 的固定执行顺序 (名称必须与 contract.stage_name 一致;
# 插件目录名与规范名不一致时, 文件发现会以 contract.stage_name 注册)
_PIPELINE_ORDER: list[str | tuple[str, ...]] = [
    # 流水线顺序是显式设计决策, 非 OCP 违反:
    # 22 个 Stage 的先后关系由视频剪辑业务逻辑决定 (源数据 → 分析 → 创作 → 审批 → 渲染),
    # 无法从依赖关系自动推导 (多个 Stage 读取同一上游表, 拓扑排序无法唯一确定顺序)。
    # 新增 Stage 时需在此显式声明其位置, 这是对架构意图的明确表达, 不是硬编码缺陷。
    # tuple 表示并行组: 组内 Stage 无相互依赖, 可同时执行。

    # Phase 1: Source Prep (VLM-first architecture)
    "source_windows",              # 1. 切视频 + 480p CRF32
    "global_context",              # 2. 从 API/剧本提取全局背景（新）
    "vlm_analysis",                # 3. VLM 逐窗分析（原 window_analysis）
    "confidence_check",            # 4. 质量门控，按需触发 ASR（新）

    # Phase 1.5: Conditional ASR (由 confidence_check 动态决定是否跳过)
    "asr_transcript",              # 5. 条件触发: FunASR 转录

    # Phase 2: Series Knowledge
    "event_cards",                 # 6
    "episode_digests",             # 7
    "chapter_digests",             # 8
    "series_registry",             # 9
    "series_assignment",           # 10

    # Phase 3: Story Generation
    "series_bible",                # 11
    "story_catalog",               # 12
    "story_portfolio",             # 13
    "story_treatments",            # 14
    "story_scripts",               # 15
    "story_preflight",             # 16
    "story_approval",              # 17 (HITL)

    # Phase 4: Production
    "story_evidence",              # 18
    "span_candidates",             # 19
    "story_plans_preflight",       # 20 (HITL)
    "story_plans",                 # 21
    "story_plans_materialize",     # 22
    "story_plans_qc_admission",    # 23 (HITL)
    "story_qc",                    # 24
    "story_qc_review",             # 25 (HITL)
    "story_render",                # 26
]

# Phase 1 (数据采集): source_windows → story_scripts
# Phase 2 (创作与生产): story_preflight → story_render
_PHASE_BOUNDARY = "story_preflight"

HUMAN_NODES: set[str] = {
    "story_approval",
    "story_plans_preflight",
    "story_plans_qc_admission",
    "story_qc_review",
}


class StageRegistry:
    """发现并索引所有已注册的流水线 Stage。"""

    def __init__(self) -> None:
        self._stages: dict[str, type["Stage"]] = {}

    def discover(self) -> dict[str, type["Stage"]]:
        """扫描所有可用的 Stage, 返回 {name: stage_cls} 映射。

        主要机制: 文件约定发现 — plugins/*/stages/*/stage.py
        辅助机制: entry_points — pip 安装的插件

        文件发现的 Stage 优先 — entry_points 不会覆盖同名 Stage。
        发现完成后校验: _PIPELINE_ORDER 中未实现的 Stage 记录警告日志。
        """
        # 方式一: 文件扫描 (主要机制)
        self._discover_from_files()

        # 方式二: entry_points (辅助, 用于 pip-installable 插件)
        try:
            eps = entry_points(group="ac_cutflow.stages")
        except TypeError:
            eps = entry_points().get("ac_cutflow.stages", [])
        for ep in eps:
            if ep.name in self._stages:  # 文件扫描优先, 不覆盖同名 Stage
                continue
            try:
                self._stages[ep.name] = ep.load()
            except (ImportError, AttributeError):
                # 不静默吞错 — 记录警告, 便于排查安装/依赖问题
                logger.warning(
                    "entry_point Stage 加载失败: %s (%s)",
                    ep.name, ep.value, exc_info=True,
                )

        self._validate_pipeline_order()
        return self._stages

    def _validate_pipeline_order(self) -> None:
        """校验命名一致性: 未实现的 Stage 记录警告。"""
        registered = set(self._stages.keys())
        for item in _PIPELINE_ORDER:
            # 并行组 (tuple): 检查组内每个 stage
            if isinstance(item, tuple):
                for name in item:
                    if name not in registered and name not in HUMAN_NODES:
                        logger.warning(
                            "_PIPELINE_ORDER 并行组中的 Stage '%s' 未找到已注册实现 "
                            "(plugins/*/stages/%s/stage.py 缺失或导入失败)",
                            name, name,
                        )
                continue
            if item not in registered:
                if item in HUMAN_NODES:
                    logger.debug("Stage %s 为人工节点, 暂无插件实现", item)
                else:
                    logger.warning(
                        "_PIPELINE_ORDER 中的 Stage '%s' 未找到已注册实现 "
                        "(plugins/*/stages/%s/stage.py 缺失或导入失败)",
                        item, item,
                    )

    def _discover_from_files(self) -> None:
        """遍历 plugins/*/stages/*/stage.py 查找 Stage 子类。"""
        plugins_root = _PROJECT_ROOT / "plugins"
        if not plugins_root.is_dir():
            return

        for plugin_dir in sorted(plugins_root.iterdir()):
            if not plugin_dir.is_dir() or plugin_dir.name.startswith("."):
                continue
            stages_dir = plugin_dir / "stages"
            if not stages_dir.is_dir():
                continue

            for stage_dir in sorted(stages_dir.iterdir()):
                if not stage_dir.is_dir() or stage_dir.name.startswith("_"):
                    continue
                stage_file = stage_dir / "stage.py"
                if not stage_file.is_file():
                    continue
                try:
                    stage_cls = _import_stage(stage_file, stage_dir.name)
                    if stage_cls is not None:
                        key = _registration_key(stage_dir.name, stage_cls)
                        self._stages[key] = stage_cls
                        self._check_stage_name_consistency(key, stage_cls)
                except (ImportError, AttributeError, TypeError, ValueError):
                    # 单个插件发现失败不中断整体扫描 — 记录警告含异常栈
                    logger.warning(
                        "Stage 插件导入失败: %s", stage_file, exc_info=True
                    )

    def _check_stage_name_consistency(
        self, dir_name: str, stage_cls: type
    ) -> None:
        """检查插件目录名与 contract.stage_name 是否一致。

        注册 key 以插件目录名为准 (与 _PIPELINE_ORDER 对齐);
        contract.stage_name 不一致时记录警告 — 下游 bus.latest()/resolve()
        依赖两者一致。
        """
        try:
            instance = stage_cls(None)  # type: ignore[call-arg]
            contract_name = instance.contract.stage_name
        except (TypeError, AttributeError, ValueError):
            # 构造器/合同不可用时跳过一致性检查 (不阻断发现流程)
            return
        if contract_name != dir_name:
            logger.warning(
                "Stage 命名不一致: 插件目录名 '%s' ≠ contract.stage_name '%s' — "
                "bus 产物键以 contract.stage_name 为准, 请统一命名",
                dir_name, contract_name,
            )

    def get(self, name: str) -> type["Stage"] | None:
        """按注册名获取 Stage 类, 未注册时返回 None。"""
        return self._stages.get(name)

    def list_stages(self) -> list[str]:
        """列出全部已注册 Stage 的名称 (排序后)。"""
        return sorted(self._stages.keys())

    def pipeline_order(self) -> list[str | tuple[str, ...]]:
        """返回可执行的流水线顺序: _PIPELINE_ORDER 中已注册的子集。

        未实现的 Stage (如人工节点) 被自动过滤。
        并行组 (tuple) 中的 Stage 如果全部未注册则整个组被过滤;
        如果部分注册则保留注册的子集。
        """
        registered = set(self._stages.keys())
        result: list[str | tuple[str, ...]] = []
        for item in _PIPELINE_ORDER:
            if isinstance(item, tuple):
                active = tuple(s for s in item if s in registered)
                if len(active) > 1:
                    result.append(active)
                elif len(active) == 1:
                    result.append(active[0])
            elif item in registered:
                result.append(item)
        return result

    def human_nodes(self) -> set[str]:
        """返回需要人工介入的节点集合 (auto 模式由决策函数接管)。"""
        return HUMAN_NODES


def _registration_key(dir_name: str, stage_cls: type) -> str:
    """注册 key 与 _PIPELINE_ORDER 对齐。

    插件目录名 ≠ contract.stage_name 时: 若 contract.stage_name 是
    _PIPELINE_ORDER 中的规范名且目录名不在其中, 以 contract.stage_name
    注册 (如 registry → series_registry), 保证 pipeline_order() 可解析。
    """
    try:
        contract_name = stage_cls(None).contract.stage_name  # type: ignore[call-arg]
    except (TypeError, AttributeError, ValueError):
        # 构造器/合同不可用时回退目录名注册
        return dir_name
    if (
        contract_name
        and contract_name != dir_name
        and contract_name in _PIPELINE_ORDER
        and dir_name not in _PIPELINE_ORDER
    ):
        return contract_name
    return dir_name


def _import_stage(file_path: Path, stage_name: str) -> type | None:
    """导入单个 stage.py 并返回其中的 Stage 子类。

    把文件路径转成模块导入路径 (如 plugins/ac_qc/stages/story_qc/stage.py
    → plugins.ac_qc.stages.story_qc.stage), 导入成功后扫描模块内
    第一个 Stage 子类返回; 模块内无 Stage 或导入失败时返回 None。
    """
    from autocut_core.stages._base import Stage as BaseStage

    rel = file_path.relative_to(_PROJECT_ROOT)
    mod_path = str(rel.with_suffix("")).replace("/", ".").replace("\\", ".")

    _ensure_packages(rel)
    try:
        mod = importlib.import_module(mod_path)
    except ImportError:
        logger.warning(
            "Stage 模块导入失败 (ImportError): %s — 请检查该插件目录是否缺少 __init__.py",
            mod_path, exc_info=True,
        )
        return None

    for attr_name in dir(mod):
        obj = getattr(mod, attr_name)
        if isinstance(obj, type) and issubclass(obj, BaseStage) and obj is not BaseStage:
            return obj
    return None


def _ensure_packages(rel_path: Path) -> None:
    """确保父级包可导入 — 不在运行时写入 __init__.py。

    插件包结构中的 __init__.py 由仓库静态维护；
    缺失时依赖 Python 命名空间包机制 (PEP 420) 导入。
    """
    parts = rel_path.parts[:-1]
    for i in range(1, len(parts) + 1):
        pkg_name = ".".join(parts[:i])
        if pkg_name not in sys.modules:
            try:
                importlib.import_module(pkg_name)
            except ImportError:
                pass
