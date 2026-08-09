"""并发控制模块 — 从 semantic_engine.py 提取的 RateLimiter + AdaptiveConcurrencyController。

包含:
  - RateLimiter: 客户端请求限速器
  - AdaptiveConcurrencyController: AIMD 自适应并发控制器
  - resolve_worker_count: 解析并发数
  - parse_worker_setting: 解析 --workers 参数
  - 并发上限常量
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

# ── 并发上限常量 ────────────────────────────────────────────────────────

ABSOLUTE_MAX_SEMANTIC_WORKERS = 64
DEFAULT_QWEN_TEXT_WORKERS = 64
DEFAULT_QWEN_REMOTE_MULTIMODAL_WORKERS = 32
DEFAULT_QWEN_INLINE_MULTIMODAL_WORKERS = 16
DEFAULT_DOUBAO_TEXT_WORKERS = 64
DEFAULT_DOUBAO_REMOTE_MULTIMODAL_WORKERS = 32
DEFAULT_DOUBAO_INLINE_MULTIMODAL_WORKERS = 16
DEFAULT_OTHER_BACKEND_WORKERS = 8
UNPACED_BACKENDS = frozenset({"qwen", "doubao"})

# ── RateLimiter ─────────────────────────────────────────────────────────


class RateLimiter:
    """客户端限速器 — 按每分钟请求数均匀放行请求 (0 表示不限速)。"""

    def __init__(self, requests_per_minute: float) -> None:
        self.requests_per_minute = float(requests_per_minute)
        self.interval = (
            60.0 / self.requests_per_minute
            if self.requests_per_minute > 0
            else 0.0
        )
        self.next_time = 0.0
        self.lock = threading.Lock()

    def wait(self) -> None:
        """阻塞到下一个允许发送的时刻 (线程安全)。"""
        if self.interval <= 0:
            return
        with self.lock:
            now = time.monotonic()
            delay = max(0.0, self.next_time - now)
            self.next_time = max(now, self.next_time) + self.interval
        if delay:
            time.sleep(delay)


# ── 并发解析 ────────────────────────────────────────────────────────────


def parse_worker_setting(value: str) -> str | int:
    """解析 --workers 参数: 'auto' 或 1..64 的整数。"""
    import argparse
    normalized = str(value).strip().lower()
    if normalized == "auto":
        return "auto"
    try:
        workers = int(normalized)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "--workers must be auto or an integer"
        ) from exc
    if not 1 <= workers <= ABSOLUTE_MAX_SEMANTIC_WORKERS:
        raise argparse.ArgumentTypeError(
            f"--workers must be auto or in 1..{ABSOLUTE_MAX_SEMANTIC_WORKERS}"
        )
    return workers


def _environment_worker_cap(
    names: tuple[str, ...],
    default: int,
    *,
    environ: dict[str, str],
) -> int:
    """从环境变量读取并发上限; 未设置时返回默认值。"""
    for name in names:
        value = environ.get(name)
        if value is None or not value.strip():
            continue
        try:
            cap = int(value)
        except ValueError as exc:
            raise ValueError(f"{name} must be an integer") from exc
        if not 1 <= cap <= ABSOLUTE_MAX_SEMANTIC_WORKERS:
            raise ValueError(
                f"{name} must be in 1..{ABSOLUTE_MAX_SEMANTIC_WORKERS}"
            )
        return cap
    return default


def resolve_worker_count(
    requested: str | int,
    *,
    backend_name: str,
    jobs: list[dict[str, Any]],
    environ: dict[str, str] | None = None,
) -> int:
    """把 auto 并发解析为 backend/任务类型允许的最大安全并发数。"""

    from autocut_core.backends._base import MULTIMODAL_TASKS

    def _job_uses_multimodal(job: dict[str, Any]) -> bool:
        task = job.get("task")
        return task in MULTIMODAL_TASKS or (
            task == "story_plan_selection"
            and bool(job.get("media_file") or job.get("media_url"))
        )

    job_count = max(1, len(jobs))
    if isinstance(requested, int):
        if not 1 <= requested <= ABSOLUTE_MAX_SEMANTIC_WORKERS:
            raise ValueError(
                f"workers must be in 1..{ABSOLUTE_MAX_SEMANTIC_WORKERS}"
            )
        return min(job_count, requested)
    if requested != "auto":
        raise ValueError("workers must be auto or an integer")
    values = os.environ if environ is None else environ
    has_multimodal = any(_job_uses_multimodal(job) for job in jobs)
    has_inline_multimodal = any(
        _job_uses_multimodal(job)
        and isinstance(job.get("media_file"), str)
        for job in jobs
    )
    if backend_name == "qwen":
        cap = _environment_worker_cap(
            ("QWEN_MAX_MULTIMODAL_CONCURRENCY", "QWEN_MAX_CONCURRENCY")
            if has_multimodal
            else ("QWEN_MAX_TEXT_CONCURRENCY", "QWEN_MAX_CONCURRENCY"),
            (
                DEFAULT_QWEN_INLINE_MULTIMODAL_WORKERS
                if has_inline_multimodal
                else DEFAULT_QWEN_REMOTE_MULTIMODAL_WORKERS
            )
            if has_multimodal
            else DEFAULT_QWEN_TEXT_WORKERS,
            environ=values,
        )
    elif backend_name == "doubao":
        cap = _environment_worker_cap(
            ("DOUBAO_MAX_MULTIMODAL_CONCURRENCY", "DOUBAO_MAX_CONCURRENCY")
            if has_multimodal
            else ("DOUBAO_MAX_TEXT_CONCURRENCY", "DOUBAO_MAX_CONCURRENCY"),
            (
                DEFAULT_DOUBAO_INLINE_MULTIMODAL_WORKERS
                if has_inline_multimodal
                else DEFAULT_DOUBAO_REMOTE_MULTIMODAL_WORKERS
            )
            if has_multimodal
            else DEFAULT_DOUBAO_TEXT_WORKERS,
            environ=values,
        )
    else:
        cap = _environment_worker_cap(
            ("SEMANTIC_MAX_CONCURRENCY",),
            DEFAULT_OTHER_BACKEND_WORKERS,
            environ=values,
        )
    return min(job_count, cap, ABSOLUTE_MAX_SEMANTIC_WORKERS)


# ── AdaptiveConcurrencyController ────────────────────────────────────────


class AdaptiveConcurrencyController:
    """自适应并发控制器 — AIMD 策略调整并发上限。"""

    def __init__(self, maximum: int) -> None:
        if maximum < 1:
            raise ValueError("maximum concurrency must be positive")
        self.maximum = int(maximum)
        self.limit = int(maximum)
        self.active = 0
        self.peak_active = 0
        self.throttle_events = 0
        self.decrease_events = 0
        self.increase_events = 0
        self.successes_since_adjustment = 0
        self.cooldown_until = 0.0
        self.last_decrease_at = float("-inf")
        self.condition = threading.Condition()

    def acquire(self) -> None:
        """获取一个并发槽; 达到上限或处于冷却期时阻塞等待。"""
        with self.condition:
            while True:
                now = time.monotonic()
                cooldown = max(0.0, self.cooldown_until - now)
                if cooldown <= 0 and self.active < self.limit:
                    self.active += 1
                    self.peak_active = max(self.peak_active, self.active)
                    return
                self.condition.wait(
                    timeout=max(0.01, min(cooldown or 0.25, 0.25))
                )

    def release(
        self,
        *,
        success: bool,
        throttled: bool = False,
        cooldown_seconds: float = 0.0,
    ) -> None:
        """释放并发槽并按结果调整上限。"""
        with self.condition:
            if self.active <= 0:
                raise RuntimeError("concurrency slot released without acquire")
            self.active -= 1
            now = time.monotonic()
            if throttled:
                self.throttle_events += 1
                self.cooldown_until = max(
                    self.cooldown_until,
                    now + max(0.0, float(cooldown_seconds)),
                )
                if now - self.last_decrease_at >= 1.0:
                    reduced = max(1, (self.limit + 1) // 2)
                    if reduced < self.limit:
                        self.limit = reduced
                        self.decrease_events += 1
                    self.last_decrease_at = now
                self.successes_since_adjustment = 0
            elif success:
                self.successes_since_adjustment += 1
                recovery_threshold = max(2, self.limit)
                if (
                    self.limit < self.maximum
                    and self.successes_since_adjustment >= recovery_threshold
                ):
                    self.limit += 1
                    self.increase_events += 1
                    self.successes_since_adjustment = 0
            self.condition.notify_all()

    def snapshot(self) -> dict[str, Any]:
        """导出当前控制状态快照。"""
        with self.condition:
            return {
                "maximum": self.maximum,
                "current_limit": self.limit,
                "peak_active_requests": self.peak_active,
                "throttle_events": self.throttle_events,
                "decrease_events": self.decrease_events,
                "increase_events": self.increase_events,
            }