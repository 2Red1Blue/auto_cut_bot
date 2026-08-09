"""PerBackendRateLimiter — 滑动窗口 + AIMD 动态调速。

每个 SemanticBackend 绑定一个 PerBackendRateLimiter 实例,
单进程内通过 time.monotonic() 追踪请求时间戳实现速率控制,
无需跨进程锁。

AIMD 模式:
  - Additive Increase: 每次 wait() 成功, 有效 RPM 向配置上限加 1
  - Multiplicative Decrease: 收到限流信号 (429) 时, 有效 RPM 减半
  - 这种模式与 call_provider 的指数退避重试互补,
    退避解决瞬时冲突, AIMD 解决长期速率收敛。

用法:
    limiter = PerBackendRateLimiter(backend)
    for request in requests:
        limiter.wait()
        send_request()
        if response.status == 429:
            limiter.throttle()
"""

from __future__ import annotations

import time
from collections import deque
from typing import Optional


class PerBackendRateLimiter:
    """滑动窗口速率限制器, 绑定单个 SemanticBackend。

    特性:
      - time.monotonic() 保证单调递增, 不受系统时钟调整影响
      - 滑动窗口 (默认 60 秒) 内维持请求数 <= rpm_limit
      - rpm_limit <= 0 时跳过所有限制 (noop)
      - AIMD 动态调速: 被限流时有效 RPM 减半, 成功后逐步恢复
    """

    __slots__ = (
        "_configured_rpm",
        "_effective_rpm",
        "_window_seconds",
        "_timestamps",
        "_additive_step",
        "_multiplicative_factor",
    )

    def __init__(
        self,
        backend: "SemanticBackend",  # type: ignore[name-defined]  # noqa: F821
        *,
        override_rpm: Optional[int] = None,
        window_seconds: float = 60.0,
        additive_step: int = 1,
        multiplicative_factor: float = 0.5,
    ) -> None:
        """初始化速率限制器。

        Args:
            backend: 后端描述符, 读取 rpm_limit 字段。
            override_rpm: 覆盖 backend.rpm_limit (0 = 无限制)。
            window_seconds: 滑动窗口长度, 默认 60 秒。
            additive_step: AIMD 加性增长步长, 每次成功 wait() 增加此值。
            multiplicative_factor: AIMD 乘性减小因子, 限流时乘以有效 RPM。
        """
        rpm = override_rpm if override_rpm is not None else backend.rpm_limit
        self._configured_rpm = rpm
        self._effective_rpm = rpm
        self._window_seconds = float(window_seconds)
        self._timestamps: deque[float] = deque()
        self._additive_step = additive_step
        self._multiplicative_factor = multiplicative_factor

    # ── 查询 ────────────────────────────────────────────────────────────────

    @property
    def configured_rpm(self) -> int:
        """配置的 RPM 上限 (来自 backend.rpm_limit)。"""
        return self._configured_rpm

    @property
    def effective_rpm(self) -> int:
        """当前有效 RPM (AIMD 调整后的值)。"""
        return self._effective_rpm

    @property
    def limited(self) -> bool:
        """是否启用限速 (rpm_limit > 0)。"""
        return self._configured_rpm > 0

    # ── 速率控制 ────────────────────────────────────────────────────────────

    def wait(self) -> None:
        """阻塞直到有空闲请求槽位。

        无限制 (rpm_limit <= 0) 时立即返回。
        窗口内已达上限时, 休眠直到最旧的时间戳滑出窗口;
        休眠后清理过期记录, 记录当前请求并触发 AIMD 加性增长。
        """
        if self._configured_rpm <= 0:
            return

        limit = max(1, self._effective_rpm)
        self._ensure_slot(limit)
        self._timestamps.append(time.monotonic())
        self._additive_increase()

    def throttle(self) -> None:
        """收到限流信号时调用, 触发 AIMD 乘性减小。

        有效 RPM 乘以 multiplicative_factor (默认 0.5),
        最低降至 1 RPM (不会降至 0, 保证至少一个请求能通过)。
        """
        if self._configured_rpm <= 0:
            return
        old = self._effective_rpm
        self._effective_rpm = max(1, int(old * self._multiplicative_factor))
        # 清理窗口内超出新限制的时间戳, 避免后续 wait() 立即休眠
        self._trim_to(self._effective_rpm)

    def reset(self) -> None:
        """重置状态: 清空窗口, 恢复有效 RPM 到配置值。"""
        self._timestamps.clear()
        self._effective_rpm = self._configured_rpm

    # ── 内部 ────────────────────────────────────────────────────────────────

    def _trim_to(self, limit: int) -> None:
        """保留窗口内最近 `limit` 条记录, 丢弃更旧的。"""
        while len(self._timestamps) > limit:
            self._timestamps.popleft()

    def _ensure_slot(self, limit: int) -> None:
        """确保窗口内请求数 < limit; 必要时休眠等待。"""
        now = time.monotonic()
        cutoff = now - self._window_seconds

        # 丢弃窗口外的过期时间戳
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()

        if len(self._timestamps) >= limit:
            # 需要休眠到最旧记录滑出窗口
            sleep_time = self._timestamps[0] - cutoff
            if sleep_time > 0:
                time.sleep(sleep_time)
                # 休眠后重新清理过期记录
                now = time.monotonic()
                cutoff = now - self._window_seconds
                while self._timestamps and self._timestamps[0] < cutoff:
                    self._timestamps.popleft()

    def _additive_increase(self) -> None:
        """AIMD 加性增长: 向配置上限逐步恢复有效 RPM。"""
        if self._effective_rpm < self._configured_rpm:
            self._effective_rpm = min(
                self._configured_rpm,
                self._effective_rpm + self._additive_step,
            )