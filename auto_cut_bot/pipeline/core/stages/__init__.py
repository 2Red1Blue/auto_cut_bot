"""stages/ — Stage 基类与适配层。

包含:
  - _base.py: bus-based Stage 基类 (全部插件继承);
  - adapter.py: BusStageAdapter — 把 bus-based Stage 包装为
    编排器统一调度的生命周期接口。
"""

from __future__ import annotations
