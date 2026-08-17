"""统一异常体系 — autocut_core 全部自定义异常的唯一定义源。

异常层级:
    PipelineError (继承 RuntimeError, 保持与既有捕获代码兼容)
    ├── ConfigError             配置解析/缺失导致的失败
    ├── StageNotFoundError      请求执行的 Stage 未注册
    ├── StageExecutionError     Stage 生命周期内的执行失败
    ├── ContractViolationError  error 级合同违规阻断流水线
    ├── ArtifactNotFoundError   上游产物缺失导致下游无法消费
    └── AutoDecisionError       auto 模式决策需要中止流水线

退出码约定 (autocut CLI):
    EXIT_OK                 = 0   成功 (含人工节点的正常暂停退出)
    EXIT_FAILURE            = 1   一般流水线失败
    EXIT_CONTRACT_VIOLATION = 2   error 级合同违规阻断

每个异常携带 ``error_code`` (机器可读, 写入 failure.json 的
error_code 字段) 与 ``exit_code`` (进程退出码),
编排器在 CLI 入口统一捕获并按退出码约定退出。
"""

from __future__ import annotations

__all__ = [
    "EXIT_OK",
    "EXIT_FAILURE",
    "EXIT_CONTRACT_VIOLATION",
    "PipelineError",
    "ConfigError",
    "StageNotFoundError",
    "StageExecutionError",
    "ContractViolationError",
    "ArtifactNotFoundError",
    "AutoDecisionError",
]

# ── 退出码约定 ────────────────────────────────────────────────────────────
EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_CONTRACT_VIOLATION = 2


class PipelineError(RuntimeError):
    """流水线异常基类 — 全部核心层自定义异常的父类。

    继承 RuntimeError 以保持与既有 ``except RuntimeError`` /
    测试断言的行为兼容。子类通过类属性声明 error_code 与 exit_code,
    实例化时可用同名关键字参数覆盖。
    """

    error_code: str = "pipeline_error"
    exit_code: int = EXIT_FAILURE

    def __init__(
        self,
        message: str,
        *,
        error_code: str | None = None,
        exit_code: int | None = None,
    ) -> None:
        super().__init__(message)
        if error_code is not None:
            self.error_code = error_code
        if exit_code is not None:
            self.exit_code = exit_code


class ConfigError(PipelineError):
    """配置层失败 — 必填配置缺失、job_root 未设置、配置解析失败等。"""

    error_code = "config_error"


class StageNotFoundError(PipelineError):
    """请求执行的 Stage 未在 StageRegistry 中注册。"""

    error_code = "stage_not_found"


class StageExecutionError(PipelineError):
    """Stage 生命周期 (prepare/execute/validate 或子进程委托) 内的执行失败。"""

    error_code = "stage_execution_error"


class ContractViolationError(PipelineError):
    """error 级合同违规阻断流水线 — 退出码独立于一般失败 (EXIT_CONTRACT_VIOLATION)。"""

    error_code = "contract_violation"
    exit_code = EXIT_CONTRACT_VIOLATION


class ArtifactNotFoundError(PipelineError):
    """下游 Stage 请求的上游产物在 ArtifactBus 中不存在。"""

    error_code = "artifact_not_found"


class AutoDecisionError(PipelineError):
    """auto 模式决策需要中止流水线时抛出 (如裁决所需的输入文件缺失)。"""

    error_code = "auto_decision_failed"
