"""确定性原子 I/O 工具集 — Story-first 流水线全局共享的落盘基础设施。

模块职责:
  - **原子写入**: 所有写操作走 mkstemp + os.replace, 保证任何时刻
    落盘文件要么是完整旧内容、要么是完整新内容, 不会出现半截文件
    (流水线可能在中途被中断/恢复, 半截 JSON 会导致 resume 崩溃);
  - **并发安全**: 所有写操作通过文件锁 (fcntl/msvcrt) 保护, 防止
    并行进程间的 lost update; 读-改-写循环自动重试;
  - **显式编码读取**: 所有读操作显式指定 utf-8, 避免平台默认编码差异;
  - **SHA-256 哈希工具**: 支撑 Stage 间的哈希链合同 (Artifact 的
    input_shas 绑定链) 与 ArtifactBus 的内容寻址存储;
  - **项目状态维护**: project.json 检查点与 failure.json 失败记录的
    统一写入入口 (orchestrator 与各 Stage 共用)。

在流水线中的位置: 最底层工具模块, 被 contracts/types.py (ArtifactBus)、
orchestrator/pipeline.py 以及各插件 Stage 依赖, 自身不依赖任何上层模块。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import time as _time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator, Iterable


# ── 时间 ──────────────────────────────────────────────────────────────────


def utc_now() -> str:
    """返回当前 UTC 时间的 ISO-8601 字符串 (带时区), 用于所有落盘时间戳。"""
    return datetime.now(timezone.utc).isoformat()


# ── 文件锁 ──────────────────────────────────────────────────────────────────


def _lock_file_descriptor() -> Any:
    """返回当前平台的文件锁模块 (fcntl on Unix, msvcrt on Windows)。"""
    try:
        import fcntl
        return fcntl
    except ImportError:
        import msvcrt  # type: ignore[import-not-found,no-redef]
        return msvcrt


def _acquire_file_lock(lock_path: Path, *, timeout: float = 30.0) -> Any:
    """获取文件排他锁, 超时未获取则抛出 TimeoutError。

    返回锁文件描述符, 调用方负责在 finally 中传给 _release_file_lock()。
    锁文件创建在目标文件同目录下, 保证 rename 在同一文件系统。
    """
    resolved = lock_path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(resolved), os.O_CREAT | os.O_RDWR, 0o644)
    deadline = _time.monotonic() + timeout
    lock_mod = _lock_file_descriptor()
    while True:
        try:
            if hasattr(lock_mod, "flock"):
                lock_mod.flock(fd, lock_mod.LOCK_EX | lock_mod.LOCK_NB)
            else:
                # msvcrt: lock entire file
                lock_mod.locking(fd, lock_mod.LK_NBLCK, 1)  # type: ignore[union-attr]
            return fd
        except (BlockingIOError, OSError):
            if _time.monotonic() >= deadline:
                os.close(fd)
                raise TimeoutError(
                    f"无法获取文件锁 {resolved} (超时 {timeout}s)"
                )
            _time.sleep(0.05 + _time.monotonic() % 0.05)


def _release_file_lock(fd: Any) -> None:
    """释放文件排他锁并关闭文件描述符。"""
    try:
        lock_mod = _lock_file_descriptor()
        if hasattr(lock_mod, "flock"):
            lock_mod.flock(fd, lock_mod.LOCK_UN)
        else:
            # msvcrt: unlock entire file
            lock_mod.locking(fd, lock_mod.LK_UNLCK, 1)  # type: ignore[union-attr]
    except Exception:
        pass
    finally:
        os.close(fd)


@contextmanager
def file_lock(target_path: Path, *, timeout: float = 30.0) -> Generator[None, None, None]:
    """文件锁上下文管理器 — 对目标文件加排他锁, 离开时自动释放。

    锁文件为 ``{target_path}.lock``, 与目标文件同目录。
    跨平台兼容: Unix 用 fcntl.flock, Windows 用 msvcrt.locking。
    """
    lock_path = Path(str(target_path) + ".lock")
    fd = _acquire_file_lock(lock_path, timeout=timeout)
    try:
        yield
    finally:
        _release_file_lock(fd)


def _retry_with_backoff(
    fn: Any, *args: Any, max_retries: int = 5, base_delay: float = 0.1, **kwargs: Any
) -> Any:
    """带指数退避的重试包装器 — 用于并发冲突场景下自动重试。

    退避策略: base_delay * 2^attempt + 随机抖动 (0~0.1s)。
    所有重试耗尽后抛出最后一次的异常。
    """
    import random as _random
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return fn(*args, **kwargs)
        except (BlockingIOError, OSError) as exc:
            last_exc = exc
            if attempt == max_retries:
                break
            delay = base_delay * (2 ** attempt) + _random.uniform(0, 0.1)
            _time.sleep(delay)
    raise last_exc  # type: ignore[misc]


# ── 读取 ──────────────────────────────────────────────────────────────────


def load_json(path: Path) -> Any:
    """读取 JSON 文件 (显式 utf-8)。文件不存在或格式非法时抛出原始异常。"""
    with path.expanduser().resolve().open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    """读取 JSONL 文件, 逐行解析为字典列表。

    若行内容为 ``{"analysis": {...}}`` 包装结构, 自动解包取出
    内层 analysis 对象。行不是 JSON 对象时抛出带行号的 ValueError。
    """
    records: list[dict[str, Any]] = []
    with path.expanduser().resolve().open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if isinstance(value, dict) and isinstance(value.get("analysis"), dict):
                value = value["analysis"]
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number}: expected JSON object")
            records.append(value)
    return records


# ── 原子写入 ────────────────────────────────────────────────────────────────


def _atomic_text(path: Path, text: str, *, mode: int | None = None) -> None:
    """原子写入底层实现: 先写同目录临时文件, fsync 后 os.replace 改名。

    为什么: 临时文件与目标同目录, 保证 rename 在同一文件系统上原子完成;
    流水线可能在任意时刻中断, 半截文件会破坏 resume 的正确性。
    finally 中清理残留临时文件, 防止失败路径遗留垃圾。
    """
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{resolved.name}-", suffix=".tmp", dir=resolved.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, resolved)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_write_json(
    path: Path, value: Any, *, private: bool = False, compact: bool = False
) -> None:
    """原子写入 JSON 文件。

    - ``private=True`` 时文件权限 0600 (用于可能含敏感信息的 failure.json);
    - ``compact=True`` 使用紧凑分隔符 (用于参与哈希计算或索引的文件),
      否则使用 indent=2 便于人工审阅; 均保留非 ASCII 字符原样输出。
    """
    kwargs = (
        {"ensure_ascii": False, "separators": (",", ":")}
        if compact
        else {"ensure_ascii": False, "indent": 2}
    )
    _atomic_text(
        path,
        json.dumps(value, **kwargs) + "\n",
        mode=0o600 if private else None,
    )


def atomic_write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    """原子写入 JSONL 文件 — 每行一个紧凑 JSON 对象, 空记录写空文件。"""
    lines = [
        json.dumps(item, ensure_ascii=False, separators=(",", ":")) for item in records
    ]
    _atomic_text(path, "\n".join(lines) + ("\n" if lines else ""))


def atomic_write_text(path: Path, text: str) -> None:
    """原子写入纯文本文件 (内容由调用方保证与哈希计算一致)。"""
    _atomic_text(path, text)


# ── SHA-256 哈希 ────────────────────────────────────────────────────────────


class _JCSEncoder(json.JSONEncoder):
    """RFC 8785 近似 JSON 编码器。

    与完整 JCS 的差异:
      - 不处理 IEEE 754 负零 / 次正规数的特殊字节表示;
      - 字符串采用 Python 默认转义 (与 JCS 的 \\uXXXX 要求基本一致);
      - 不检测重复键 (Python dict 天然去重)。
    """

    def __init__(self) -> None:
        super().__init__(ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def encode(self, o: Any) -> str:
        # 顶层 NaN/Infinity 拦截 (嵌套值由 default() 拦截)
        if isinstance(o, float) and (math.isnan(o) or math.isinf(o)):
            raise ValueError(f"JSON canonical: reject {o!r}")
        return super().encode(o)

    def default(self, o: Any) -> Any:
        if isinstance(o, float):
            if math.isnan(o) or math.isinf(o):
                raise ValueError(f"JSON canonical: reject {o!r}")
        return super().default(o)


def _normalize_for_json(value: Any) -> Any:
    """预遍历: 可无损还原的 float 转 int (1.0→1), 其余保持。

    保证 ``json.dumps`` 对 ``1.0`` 输出 ``1`` 而非 ``1.0``,
    与 RFC 8785 整数编码规则对齐。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        if value != value:  # NaN
            raise ValueError("JSON canonical: reject NaN")
        if value == float("inf") or value == float("-inf"):
            raise ValueError("JSON canonical: reject Infinity")
        i = int(value)
        return i if float(i) == value else value
    if isinstance(value, (int, str, type(None))):
        return value
    if isinstance(value, list):
        return [_normalize_for_json(item) for item in value]
    if isinstance(value, dict):
        return {k: _normalize_for_json(v) for k, v in value.items()}
    return value


def canonical_json(value: Any) -> bytes:
    """确定性规范化 JSON 字节串 (RFC 8785 JCS 近似)。

    规范化手段: 排序键 + 紧凑分隔符 + 浮点整数化 + NaN/Infinity 拒绝。
    与完整 RFC 8785 的差异见 ``_JCSEncoder`` 文档。

    为什么: 同一逻辑内容可能因键顺序/空白/数字表示差异产生不同字节,
    规范化后才能保证"同内容必同哈希", 这是 ArtifactBus 内容寻址
    与跨 Stage 哈希链合同的正确性前提。
    """
    return _JCSEncoder().encode(_normalize_for_json(value)).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    """计算字节串的 SHA-256 十六进制摘要。"""
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    """流式计算任意大小文件的 SHA-256 (1MB 分块, 不把大文件读入内存)。"""
    digest = hashlib.sha256()
    with path.expanduser().resolve().open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_sha256(value: Any) -> str:
    """计算任意 JSON 值的规范化 SHA-256 (ArtifactBus.put 的内容寻址依据)。"""
    return sha256_bytes(canonical_json(value))


def stable_id(prefix: str, value: Any, *, length: int = 12) -> str:
    """生成确定性稳定 ID: ``{prefix}-{json_sha256 前 length 位}``。

    同一输入内容永远产生同一 ID — 事件卡/实体等幂等重跑时 ID 不变,
    避免下游引用断裂。
    """
    return f"{prefix}-{json_sha256(value)[:length]}"


# ── 校验辅助 ────────────────────────────────────────────────────


def normalize_text(value: Any) -> str:
    """把任意值归一化为单空白文本 (非字符串返回空串), 用于宽松比较。"""
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()


def is_number(value: Any) -> bool:
    """判断是否为数字 (bool 不算数字 — Python 中 bool 是 int 子类)。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def require_object(value: Any, where: str) -> dict[str, Any]:
    """断言值是 JSON 对象, 否则抛出带位置信息的 ValueError。"""
    if not isinstance(value, dict):
        raise ValueError(f"{where}: expected object")
    return value


def require_array(value: Any, where: str) -> list[Any]:
    """断言值是 JSON 数组, 否则抛出带位置信息的 ValueError。"""
    if not isinstance(value, list):
        raise ValueError(f"{where}: expected array")
    return value


def unwrap_analysis(value: Any) -> Any:
    """解包 ``{"analysis": {...}}`` 包装结构, 非包装值原样返回。

    用于处理 LLM 输出中常见的 ``{"analysis": {...}}`` 外层包装,
    与 ``load_jsonl`` 的自动解包保持一致的语义约定。
    """
    if isinstance(value, dict) and isinstance(value.get("analysis"), dict):
        return value["analysis"]
    return value


# ── 项目状态 ────────────────────────────────────────────────────────────


def update_project_stage(
    project_path: Path,
    stage: str,
    status: str,
    *,
    inputs: dict[str, str] | None = None,
    outputs: dict[str, str] | None = None,
    note: str = "",
) -> None:
    """更新 project.json 中指定 Stage 的检查点状态。

    project.json 是整条流水线的状态检查点 (schema_version 登记在
    version.SCHEMA_VERSIONS["project"])。文件不存在时自动创建骨架;
    只覆盖目标 stage 条目, 保留其他 Stage 的历史状态, 支持断点续传。

    并发安全: 通过文件锁保护读-改-写循环, 防止并行进程间的 lost update。
    锁超时后自动重试 (指数退避), 最多 5 次。

    参数:
        project_path: project.json 路径 (通常在 job_root 下)。
        stage: Stage 名 (与 _PIPELINE_ORDER 一致)。
        status: 新状态字符串 (completed / failed / ...)。
        inputs/outputs: 输入/输出产物的 {名称: sha256} 哈希映射。
        note: 人工可读备注 (如失败原因摘要)。
    """
    resolved = project_path.expanduser().resolve()
    with file_lock(resolved):
        if resolved.is_file():
            project = require_object(load_json(resolved), str(resolved))
        else:
            project = {
                "schema_version": "1.0",
                "created_at": utc_now(),
                "updated_at": utc_now(),
                "stages": {},
            }
        stages = project.setdefault("stages", {})
        if not isinstance(stages, dict):
            raise ValueError("project.stages must be an object")
        stages[stage] = {
            "status": status,
            "updated_at": utc_now(),
            "inputs": inputs or {},
            "outputs": outputs or {},
            "note": note,
        }
        project["updated_at"] = utc_now()
        atomic_write_json(resolved, project)


def record_pipeline_failure(
    job_root: Path,
    *,
    stage: str,
    error: str,
    error_code: str = "stage_failed",
    details: dict[str, Any] | None = None,
) -> Path:
    """记录流水线失败 — 写 failure.json 并把对应 Stage 标记为 failed。

    failure.json 为 0600 权限 (可能含错误详情中的敏感路径/片段),
    error 文本截断到 8000 字符防止异常堆栈无限膨胀。
    返回 failure.json 路径, 供上层日志引用。
    """
    root = job_root.expanduser().resolve()
    payload = {
        "schema_version": "1.0",
        "status": "failed",
        "failed_at": utc_now(),
        "stage": stage,
        "error_code": error_code,
        "error": str(error)[:8000],
        "details": details or {},
    }
    failure_path = root / "failure.json"
    atomic_write_json(failure_path, payload, private=True)
    update_project_stage(
        root / "project.json",
        stage,
        "failed",
        outputs={"failure": str(failure_path)},
        note=f"{error_code}: {str(error)[:1000]}",
    )
    return failure_path


def _stage_failure_is_recovered(
    failed_stage: str,
    completed_stage: str,
    *,
    stage_order: Iterable[str] | None,
) -> bool:
    """Return whether a completed stage proves an earlier failure stale.

    Unknown/internal stage names intentionally have no inferred ordering.  A
    successful formal downstream stage can recover only stages present in the
    caller-supplied orchestrator order; every stage still supports exact-name
    recovery.
    """
    if failed_stage == completed_stage:
        return True
    if stage_order is None:
        return False
    positions = {
        stage_name: index
        for index, stage_name in enumerate(stage_order)
        if isinstance(stage_name, str)
    }
    return bool(
        failed_stage in positions
        and completed_stage in positions
        and positions[failed_stage] < positions[completed_stage]
    )


def mark_pipeline_failure_recovered(
    job_root: Path,
    *,
    stage: str,
    stage_order: Iterable[str] | None = None,
) -> None:
    """Reconcile stale failure state after a successful stage.

    ``failure.json`` remains as historical evidence and is marked recovered.
    Matching stale ``project.json`` stages are reconciled in place without
    discarding their inputs, outputs or original failure note.

    并发安全: project.json 的读-改-写通过文件锁保护。
    """
    root = job_root.expanduser().resolve()
    normalized_stage_order = (
        tuple(stage_order) if stage_order is not None else None
    )
    recovered_at = utc_now()
    failure_path = root / "failure.json"
    if failure_path.is_file():
        payload = load_json(failure_path)
        failed_stage = (
            payload.get("stage") if isinstance(payload, dict) else None
        )
        if (
            isinstance(payload, dict)
            and payload.get("status") == "failed"
            and isinstance(failed_stage, str)
            and _stage_failure_is_recovered(
                failed_stage,
                stage,
                stage_order=normalized_stage_order,
            )
        ):
            payload["status"] = "recovered"
            payload["recovered_at"] = recovered_at
            payload["recovered_by_stage"] = stage
            atomic_write_json(failure_path, payload, private=True)

    project_path = root / "project.json"
    if not project_path.is_file():
        return
    with file_lock(project_path):
        project = require_object(load_json(project_path), str(project_path))
        stages = project.get("stages")
        if not isinstance(stages, dict):
            raise ValueError("project.stages must be an object")
        changed = False
        recovery_note = f"Recovered after successful stage {stage}."
        for failed_stage, state in stages.items():
            if (
                not isinstance(failed_stage, str)
                or not isinstance(state, dict)
                or state.get("status") != "failed"
                or not _stage_failure_is_recovered(
                    failed_stage,
                    stage,
                    stage_order=normalized_stage_order,
                )
            ):
                continue
            state["status"] = "recovered"
            state["updated_at"] = recovered_at
            state["recovered_at"] = recovered_at
            state["recovered_by_stage"] = stage
            existing_note = state.get("note")
            if isinstance(existing_note, str) and existing_note.strip():
                if recovery_note not in existing_note:
                    state["note"] = existing_note.rstrip() + "\n" + recovery_note
            else:
                state["note"] = recovery_note
            changed = True
        if changed:
            project["updated_at"] = recovered_at
            atomic_write_json(project_path, project)
