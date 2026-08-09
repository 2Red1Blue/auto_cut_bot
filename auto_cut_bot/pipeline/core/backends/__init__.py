"""backends/ — 语义模型后端层。

包含:
  - _base.py: SemanticBackend 描述符与 BACKENDS 注册表
    (qwen / doubao), 合同锁定任务类型→模型映射 (rule 2).
"""

from __future__ import annotations
