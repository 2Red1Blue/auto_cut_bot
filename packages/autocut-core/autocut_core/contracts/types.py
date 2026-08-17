"""流水线核心类型定义 — 全部 Stage/编排器共享的数据模型。

采用 Pydantic v2 作为唯一 Schema 源 — 自动生成 JSON Schema，
validator 产出结构化 ContractViolation，不再手工维护 Markdown 合同。

接口约定:
  - Stage 生命周期: prepare(ctx)→TaskPlan / execute(ctx,plan)→StageResult /
    validate(ctx,result)→list[ContractViolation]
  - ArtifactBus.put() 携带 inputs/schema_version/producer 元信息
  - Artifact 不直接暴露 path 给业务层 (内部通过 bus.get() 访问内容)
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from autocut_core.io import (
    atomic_write_json, atomic_write_text, canonical_json, file_lock,
    json_sha256, load_json, sha256_file,
)
from autocut_core.logging import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# 合同违规
# ═══════════════════════════════════════════════════════════════════════════


class ContractViolation(BaseModel):
    """一条结构化的合同违规记录。

    所有 contracts/ 下的校验函数返回此类型的列表。
    """

    model_config = ConfigDict(frozen=True)

    rule_id: str                    # 对应 SKILL.md 条款, 如 "rule_26"
    code: str                       # 机器可读违规码, 如 "vad_speech_on_cut"
    message: str
    severity: str = "error"         # error | warning
    location: str = ""
    suggestion: str = ""


# ═══════════════════════════════════════════════════════════════════════════
# Stage 状态机 (22 Stage + 4 HUMAN_NODE 的完整状态)
# ═══════════════════════════════════════════════════════════════════════════


class StageStatus(str, Enum):
    """Stage 级状态机 — 记录在 project.json / Checkpoint 中。

    状态流转: pending → running → completed/failed;
    failed 经断点续传恢复后为 recovered; blocked 表示被人工节点/上游阻塞;
    skipped 表示因前置条件不满足而跳过。
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    RECOVERED = "recovered"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


class AttemptStatus(str, Enum):
    """单次执行尝试的结果状态 — success/全部成功, failure/全部失败,
    partial/部分子任务失败 (failed_job_ids 记录失败子集, 支持重试)。"""

    RUNNING = "running"
    SUCCESS = "success"
    FAILURE = "failure"
    PARTIAL = "partial"


class Attempt(BaseModel):
    """单次尝试 — 幂等键: stage_name + sha256(inputs)。"""

    attempt_id: str
    status: AttemptStatus
    started_at: str
    finished_at: str | None = None
    error: str | None = None
    failed_job_ids: list[str] = Field(default_factory=list)


class Checkpoint(BaseModel):
    """Stage 检查点 — 支持断点续传和失败子任务重试。"""

    stage_name: str
    status: StageStatus
    attempts: list[Attempt] = Field(default_factory=list)
    inputs_sha: dict[str, str] = Field(default_factory=dict)
    outputs_sha: dict[str, str] = Field(default_factory=dict)
    inputs_hash: str = ""                 # 缓存键: 输入内容 + 版本 + 配置的 SHA
    note: str = ""
    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


# ═══════════════════════════════════════════════════════════════════════════
# StageContext — 显式携带配置和恢复状态
# ═══════════════════════════════════════════════════════════════════════════


class StageContext(BaseModel):
    """Stage 执行上下文。arbitrary_types_allowed=True = 允许存放任意 Python 对象，不强制校验、不自动序列化
    不是第一次跑这个 stage，是失败后重新跑，要做续跑逻辑。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    job_root: Path
    config: Any                   # PipelineConfig (arbitrary_types_allowed)
    checkpoint: Checkpoint
    inputs: dict[str, "Artifact"] = Field(default_factory=dict)

    @property
    def stage_name(self) -> str:
        return self.checkpoint.stage_name

    def is_resume(self) -> bool:
        return self.checkpoint.status in (StageStatus.FAILED, StageStatus.RECOVERED)


# ═══════════════════════════════════════════════════════════════════════════
# TaskPlan — prepare() 的返回值
# ═══════════════════════════════════════════════════════════════════════════


class TaskPlan(BaseModel):
    """Stage.prepare() 产出的执行计划。

    tasks: 待执行的子任务列表 (每项含 type/payload, 与语义批处理
    manifest 中的 job 同构); checkpoint: 可选的断点状态;
    dry_run: True 时 execute 只报告将执行的内容不落盘。
    """

    tasks: list[dict[str, Any]] = Field(default_factory=list)
    checkpoint: Checkpoint | None = None
    dry_run: bool = False


# ═══════════════════════════════════════════════════════════════════════════
# Artifact — 内容寻址的不可变产物
# ═══════════════════════════════════════════════════════════════════════════


class Artifact(BaseModel):
    """不可变的 Stage 产物。

    path 通过只读 property 暴露 (内部存储为 _path PrivateAttr) —
    推荐外部通过 ArtifactBus.get() 访问内容。
    input_shas 自动推导哈希绑定链:
      Script ← Portfolio + Treatment
      Plan ← Evidence + Span + Selection
      frozen=True — 产物一旦发布不可篡改
      _path 用 PrivateAttr 存储，通过只读 property 暴露——业务层不应直接操作路径
    """

    model_config = ConfigDict(frozen=True)

    stage: str
    name: str
    sha256: str
    schema_version: str = "1.0"
    producer_version: str = ""
    input_shas: dict[str, str] = Field(default_factory=dict)
    _path: str = PrivateAttr(default="")

    def __init__(self, **data: Any) -> None:
        """构造时从 kwargs 中提取 _path/path 并存入 PrivateAttr。

        支持 Artifact(..., _path="/tmp/x") 和 Artifact(..., path="/tmp/x") 两种形式,
        后者为旧 dataclass 接口的兼容别名。
        """
        p = data.pop("_path", data.pop("path", ""))
        super().__init__(**data)
        object.__setattr__(self, "_path", p)

    @property
    def path(self) -> Path:
        """产物磁盘路径 (只读)。"""
        return Path(self._path)

    def bindings(self) -> dict[str, str]:
        """返回此产物绑定的全部哈希关系。"""
        return {**self.input_shas, "self": self.sha256}

    def __repr__(self) -> str:
        inputs = ",".join(self.input_shas.keys()) or "none"
        return f"Artifact({self.stage}/{self.name} sha={self.sha256[:12]} ←[{inputs}])"

    def model_dump_index(self) -> dict[str, Any]:
        """导出为 ArtifactBus index.json 的可序列化字典。"""
        return {
            "stage": self.stage,
            "name": self.name,
            "sha256": self.sha256,
            "schema_version": self.schema_version,
            "input_shas": self.input_shas,
            "_path": self._path,
        }


# ═══════════════════════════════════════════════════════════════════════════
# ArtifactBus — 带 inputs/schema_version/producer 元信息
# ═══════════════════════════════════════════════════════════════════════════


class ArtifactBus:
    """每个任务产物的存储与校验总线。

    Artifact 按 SHA-256 内容寻址存储, 不可变 — 重复写入不会覆盖。
    启动时从 index.json 恢复已有 Artifact, 支持跨进程 resume。
    """

    def __init__(self, job_root: Path) -> None:
        self._root = job_root.expanduser().resolve()
        self._cache = self._root / ".sd-cache"
        self._cache.mkdir(parents=True, exist_ok=True)
        self._index: dict[str, Artifact] = {}
        self._restore()

    # ── 启动恢复 ────────────────────────────────────────────────────

    def _restore(self) -> None:
        """从 index.json 恢复已有 Artifact, 支持跨进程 resume。

        索引文件损坏/结构非法时记录警告并回退空索引 —
        不让历史索引问题阻断新一轮执行。
        """
        index_path = self._cache / "index.json"
        if not index_path.is_file():
            return
        try:
            data = load_json(index_path)
        except (OSError, ValueError) as exc:
            logger.warning("产物索引读取失败, 回退空索引: %s (%s)", index_path, exc)
            return
        if not isinstance(data, dict):
            return
        for key, entry in data.items():
            if not isinstance(entry, dict):
                continue
            try:
                artifact = Artifact(
                    stage=entry.get("stage", ""),
                    name=entry.get("name", ""),
                    sha256=entry.get("sha256", ""),
                    schema_version=entry.get("schema_version", "1.0"),
                    producer_version=entry.get("producer_version", ""),
                    input_shas=entry.get("input_shas", {}),
                    _path=entry.get("_path", entry.get("path", "")),
                )
                self._index[key] = artifact
            except (TypeError, ValueError, KeyError):
                # 单条索引条目非法时跳过, 不影响其余条目恢复
                continue

    # ── 发布 ────────────────────────────────────────────────────────

    def put(
        self,
        name: str,
        data: Any,
        *,
        stage: str,
        schema_version: str = "1.0",
        producer_version: str = "",
        inputs: list[Artifact] | None = None,
        private: bool = False,
    ) -> Artifact:
        """发布产物 — 内容寻址, 不可变。

        SHA 作为文件名的一部分, 同内容不产生重复文件。
        """
        input_shas: dict[str, str] = {}
        if inputs:
            for inp in inputs:
                input_shas[f"{inp.stage}/{inp.name}"] = inp.sha256

        # 先计算 SHA (规范化 JSON — sort_keys + 紧凑分隔符)
        sha = json_sha256(data)

        # 内容寻址: .sd-cache/{stage}/{sha[:16]}-{name}.json
        cache_dir = self._cache / stage
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = cache_dir / f"{sha[:16]}-{name}.json"

        # 仅当文件不存在时才写入 (幂等)。
        # 写入内容必须与 SHA 计算的规范化字节完全一致,
        # 否则 get() 的 sha256_file 校验会失败。
        if not cache_path.is_file():
            atomic_write_text(cache_path, canonical_json(data).decode("utf-8"))

        artifact = Artifact(
            stage=stage,
            name=name,
            sha256=sha,
            schema_version=schema_version,
            producer_version=producer_version,
            input_shas=input_shas,
            _path=str(cache_path),
        )
        self._index[f"{stage}/{name}"] = artifact
        self._write_index()
        return artifact

    # ── 消费 ────────────────────────────────────────────────────────

    def get(self, artifact: Artifact) -> Any:
        """读取产物并校验 SHA。"""
        actual = sha256_file(Path(artifact._path))
        if actual != artifact.sha256:
            raise ValueError(
                f"SHA 不匹配 {artifact.stage}/{artifact.name}: "
                f"期望 {artifact.sha256[:12]}, 实际 {actual[:12]}"
            )
        return load_json(Path(artifact._path))

    def latest(self, stage: str) -> Artifact | None:
        """返回指定 Stage 最近一次发布的产物 (按插入顺序)。

        下游 Stage 依赖此方法解析上游产物, 因此注册 key 必须与
        contract.stage_name 一致 (见 registry 命名一致性校验)。
        """
        matches = [v for k, v in self._index.items() if k.startswith(f"{stage}/")]
        return matches[-1] if matches else None

    def resolve(self, stage: str, name: str) -> Artifact | None:
        """按 ``{stage}/{name}`` 精确查找产物, 未发布时返回 None。"""
        return self._index.get(f"{stage}/{name}")

    def _write_index(self) -> None:
        """把内存索引原子落盘为 index.json — 支持跨进程 resume。

        并发安全: 通过文件锁保护写入, 防止并行进程间的 lost update。
        写入前从磁盘重新加载索引并与内存合并, 保证不丢失其他进程的并发写入。
        注意: 每次 put 后都会重写索引 (条目数有限, 开销可忽略),
        保证进程崩溃后已发布的产物不会丢失。
        """
        index_path = self._cache / "index.json"
        with file_lock(index_path):
            # 重新加载磁盘索引并与内存合并 — 防止覆盖其他进程的并发写入
            merged = self._merge_index_from_disk(index_path)
            payload = {
                k: v.model_dump_index()
                for k, v in merged.items()
            }
            atomic_write_json(index_path, payload)

    def _merge_index_from_disk(self, index_path: Path) -> dict[str, Artifact]:
        """从磁盘加载索引并与内存索引合并, 返回合并后的索引。

        合并策略: 内存索引优先 (本进程最新写入的条目覆盖磁盘旧条目),
        磁盘中不存在于内存的条目被保留 (其他进程的并发写入)。
        """
        merged = dict(self._index)
        if not index_path.is_file():
            return merged
        try:
            disk_data = load_json(index_path)
        except (OSError, ValueError):
            return merged
        if not isinstance(disk_data, dict):
            return merged
        for key, entry in disk_data.items():
            if key in merged:
                continue  # 内存索引优先
            if not isinstance(entry, dict):
                continue
            try:
                artifact = Artifact(
                    stage=entry.get("stage", ""),
                    name=entry.get("name", ""),
                    sha256=entry.get("sha256", ""),
                    schema_version=entry.get("schema_version", "1.0"),
                    producer_version=entry.get("producer_version", ""),
                    input_shas=entry.get("input_shas", {}),
                    _path=entry.get("_path", entry.get("path", "")),
                )
                merged[key] = artifact
            except (TypeError, ValueError, KeyError):
                continue
        # 更新内存索引以反映合并结果
        self._index = merged
        return merged


# ═══════════════════════════════════════════════════════════════════════════
# StageResult — execute() 的结构化返回值
# ═══════════════════════════════════════════════════════════════════════════


class StageResult(BaseModel):
    """Stage.execute() 的结构化返回值。

    status: 本次尝试的结果状态; artifacts: 新发布的产物列表;
    violations: 执行中产生的合同违规; failed_job_ids: 失败的
    子任务 ID 列表 (partial 状态下供重试使用)。
    """

    status: AttemptStatus
    artifacts: list[Artifact] = Field(default_factory=list)
    violations: list[ContractViolation] = Field(default_factory=list)
    failed_job_ids: list[str] = Field(default_factory=list)
    note: str = ""