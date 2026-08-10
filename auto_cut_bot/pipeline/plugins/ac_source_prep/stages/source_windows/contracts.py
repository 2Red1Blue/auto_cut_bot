"""窗口策略常量与滑动窗口算法。

source_windows Stage 的合同参数: 窗口时长/重叠时长的默认值与
合法边界。窗口时长过短会导致上下文不足, 过长会超出模型输入限制;
重叠区间用于避免跨窗事件在边界处被截断。
"""

from __future__ import annotations

DEFAULT_WINDOW_SECONDS: float = 240.0
DEFAULT_OVERLAP_SECONDS: float = 12.0
MINIMUM_WINDOW_SECONDS: float = 150.0
MAXIMUM_WINDOW_SECONDS: float = 360.0
MINIMUM_OVERLAP_SECONDS: float = 8.0

VIDEO_SUFFIXES: set[str] = {".mp4", ".mov", ".mkv", ".m4v", ".webm", ".avi", ".ts"}


def sliding_windows(
    duration: float, length: float, overlap: float
) -> list[tuple[float, float]]:
    """生成覆盖 [0, duration) 的 (start, end) 滑动窗口列表。

    相邻窗口间保留 overlap 秒重叠, 供下游合并跨窗事件;
    最后一个窗口完整保留 (不截短凑长); 时长不超过窗口长时
    只产出一个整窗。重叠 ≥ 窗口长时报错 (会死循环)。
    """
    if duration <= length + 0.05:
        return [(0.0, round(duration, 6))]
    result: list[tuple[float, float]] = []
    start = 0.0
    while start < duration - 0.001:
        end = min(duration, start + length)
        result.append((round(start, 6), round(end, 6)))
        if end >= duration - 0.001:
            break
        next_start = end - overlap
        if next_start <= start:
            raise ValueError("window length must be greater than overlap")
        start = next_start
    return result
