"""contracts/ — 业务规则校验层。

与 schema/ 的边界:
  - contracts/ 负责**业务规则校验**: validate_* 函数,
    产出结构化 ``ContractViolation`` (severity=error|warning);
  - schema/ 负责**数据结构定义**: JSON Schema / Pydantic 模型与 ID 模式,
    只描述"数据长什么样", 不做业务裁决。

分层约定:
  - 核心层 ``autocut_core/contracts/`` 只放**框架级**校验基类与
    通用合同类型 (ContractViolation、Checkpoint/StageStatus 状态机、
    AudioBoundaryPolicy 等跨域策略);
  - **业务合同校验函数** (如 window 准入、story script 可行性、
    plan 合法性等) 放在对应领域插件的 ``plugins/<domain>/contracts/``
    中, 不进入核心层;
  - 所有校验函数返回 ``list[ContractViolation]``,
    由编排器按 severity 决定是否中止 Stage。
"""

from __future__ import annotations
