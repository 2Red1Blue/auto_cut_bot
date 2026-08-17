"""运行时埋点探针 — 记录 pipeline 运行期间所有被执行的代码路径。

启用方式:
    AC_PIPELINE_TRACE=1 autocut run ...
    autocut run --trace ...

每个 ``--trace`` 运行产生一份独立报告, 存放在 ``{job_root}/.sd-cache/traces/``,
以 ``{run_id}.json`` 命名 (run_id = timestamp + short uuid)。

报告结构::

    {
      "run_id": "2026-08-06-143052-abc123",
      "never_called_modules": ["autocut_core.viz.server", ...],
      "never_called_funcs": [],
      "call_counts": {"autocut_core.legacy.semantic_batch.run_batch": 3, ...},
      "module_map": {"autocut_core.io": 12, ...}
    }

线程安全: ``_call_counts`` 用 ``threading.Lock`` 保护, 支持
``run_semantic_batch`` 内部 ThreadPoolExecutor 并发写入。
"""

from __future__ import annotations

import contextvars
import functools
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from autocut_core.io import atomic_write_json

# contextvars 隔离不同 pipeline run (平行流水线安全),
# 但 _call_counts 是共享的 Lock 保护字典 (线程池安全)
_tracer: contextvars.ContextVar = contextvars.ContextVar("sd_tracer", default=None)

_TRACES_DIRNAME = ".sd-cache/traces"


def get_tracer() -> RuntimeTracer | None:
    """获取当前 context 的 tracer (未启用时返回 None)。"""
    return _tracer.get()


def start_trace(
    run_id: str | None = None,
    *,
    report_dir: Path | str | None = None,
) -> RuntimeTracer:
    """在当前 context 中启动一个 tracer。

    Args:
        run_id: 报告文件名前缀; None 时自动生成 timestamp-uuid
        report_dir: 报告落盘目录; None 时默认 ``{job_root}/.sd-cache/traces/``
    """
    if run_id is None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        run_id = f"{ts}-{uuid.uuid4().hex[:8]}"
    t = RuntimeTracer(run_id, report_dir=report_dir)
    _tracer.set(t)
    return t


def stop_trace() -> RuntimeTracer | None:
    """停止当前 context 的 tracer 并返回最终报告 (已落盘)。"""
    t = _tracer.get()
    if t is None:
        return None
    _tracer.set(None)
    t._persist()
    return t


def trace(func: Callable) -> Callable:
    """装饰器 — 被装饰的函数每次调用自动记录到当前 tracer。

    用法::

        @trace
        def run_batch(manifest_path, **kwargs):
            ...
    """
    func_path = f"{func.__module__}.{func.__qualname__}"

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        t = get_tracer()
        if t is not None:
            t.trace(func_path)
        return func(*args, **kwargs)

    return wrapper


class RuntimeTracer:
    """运行时探针 — 记录所有被 trace 的函数调用。

    模块映射预填充所有已知模块为 0, 未被 trace 命中的即为 never_called。
    """

    def __init__(
        self,
        run_id: str,
        *,
        report_dir: Path | str | None = None,
    ) -> None:
        self.run_id = run_id
        self._report_dir: Path | None = (
            Path(report_dir) if report_dir is not None else None
        )
        self._lock = threading.Lock()
        self._call_counts: dict[str, int] = {}
        self._module_map = self._build_module_map()

    # ── 模块映射 ──────────────────────────────────────────────────────

    @staticmethod
    def _build_module_map() -> dict[str, int]:
        """预填充所有已知模块为 0, 未被 trace 命中的即为 never_called。"""
        modules: dict[str, int] = {
            # 核心层
            "autocut_core.io": 0,
            "autocut_core.config": 0,
            "autocut_core.errors": 0,
            "autocut_core.logging": 0,
            "autocut_core.version": 0,
            # contracts — 包括死代码候选
            "autocut_core.contracts.types": 0,
            "autocut_core.contracts.audio_boundary": 0,
            "autocut_core.contracts.rules.engine": 0,
            "autocut_core.contracts.rules.builtin": 0,
            "autocut_core.contracts.rules.legacy_story_artifacts": 0,
            # schema
            "autocut_core.schema.ids": 0,
            # contracts
            "autocut_core.legacy.semantic_batch": 0,
            "autocut_core.legacy.stage_prep": 0,
            "autocut_core.legacy.local_scripts": 0,
            # semantic (新拆分的)
            "autocut_core.semantic.utils": 0,
            "autocut_core.semantic.window_recovery": 0,
            "autocut_core.semantic.response_validation": 0,
            "autocut_core.semantic.request": 0,
            "autocut_core.semantic.story_logic": 0,
            "autocut_core.semantic.registry": 0,
            "autocut_core.semantic.batch_runner": 0,
            # viz (可选)
            "autocut_core.viz.server": 0,
            "autocut_core.viz.control": 0,
            "autocut_core.viz.events": 0,
            # orchestrator
            "autocut_core.orchestrator.pipeline": 0,
            "autocut_core.orchestrator.auto": 0,
            # backends
            "autocut_core.backends._base": 0,
        }

        # Stage 插件从注册表动态补
        try:
            from autocut_core.registry import StageRegistry
            reg = StageRegistry()
            reg.discover()
            for name in reg.list_stages():
                modules[f"plugins.*.stages.{name}"] = 0
        except Exception:
            pass

        return modules

    # ── 记录 ──────────────────────────────────────────────────────────

    def trace(self, func_path: str) -> None:
        """记录一次函数调用 (线程安全)。"""
        with self._lock:
            self._call_counts[func_path] = (
                self._call_counts.get(func_path, 0) + 1
            )
            # 标记模块级
            module = ".".join(func_path.split(".")[:-1])
            if module in self._module_map:
                self._module_map[module] += 1

    def trace_module(self, module: str) -> None:
        """直接标记模块为已调用 (用于没有独立函数的模块)。"""
        with self._lock:
            if module in self._module_map:
                self._module_map[module] += 1

    # ── 报告 ──────────────────────────────────────────────────────────

    def report(self) -> dict[str, Any]:
        """生成调用报告 (不落盘)。"""
        with self._lock:
            return {
                "run_id": self.run_id,
                "never_called_modules": sorted(
                    m for m, c in self._module_map.items() if c == 0
                ),
                "never_called_funcs": [],  # 预留: @trace 装饰器协作
                "call_counts": dict(self._call_counts),
                "module_map": dict(self._module_map),
            }

    def _persist(self) -> Path:
        """原子写入报告到磁盘。"""
        data = self.report()
        report_path = self._resolve_report_path()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(report_path, data)
        return report_path

    def _resolve_report_path(self) -> Path:
        if self._report_dir is not None:
            return Path(self._report_dir) / f"{self.run_id}.json"
        # 默认路径: 相对于当前工作目录
        return Path.cwd() / _TRACES_DIRNAME / f"{self.run_id}.json"


# ── 聚合工具 ──────────────────────────────────────────────────────────


def merge_reports(traces_dir: Path | str) -> dict[str, Any]:
    """聚合一个目录下的多份 trace 报告, 输出合并统计。

    Returns:
        {
          "total_runs": 3,
          "always_dead": [...],   # 每次运行调用次数都是 0 的模块
          "alive": [...],         # 至少被调用一次的模块
          "by_run": {run_id: report, ...},
        }
    """
    import json as _json

    traces = Path(traces_dir)
    reports: dict[str, dict] = {}
    for f in sorted(traces.glob("*.json")):
        try:
            reports[f.stem] = _json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue

    if not reports:
        return {"total_runs": 0, "always_dead": [], "alive": [], "by_run": {}}

    all_modules: set[str] = set()
    for r in reports.values():
        all_modules.update(r.get("module_map", {}).keys())

    always_dead: list[str] = []
    alive: list[str] = []
    for mod in sorted(all_modules):
        counts = [
            reports[rid].get("module_map", {}).get(mod, 0)
            for rid in reports
        ]
        if all(c == 0 for c in counts):
            always_dead.append(mod)
        else:
            alive.append(mod)

    return {
        "total_runs": len(reports),
        "always_dead": always_dead,
        "alive": alive,
        "by_run": reports,
    }