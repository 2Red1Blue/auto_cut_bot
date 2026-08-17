"""产物缓存 GC 管理器 + Stage 级执行缓存。

缓存 GC (CacheManager):
  - LRU 淘汰: 按最后访问时间 (atime) 排序, 淘汰最久未使用的条目
  - 引用可达性: 被 index.json 引用的产物永不淘汰 (GC 安全)
  - 可预览: dry_run 模式只报告将删除的文件, 不实际删除
  - 可回滚: 删除前将文件移到回收站目录 (.sd-cache/.gc-trash/),
    手动清理回收站才真正释放空间
  - 可配置: 最大缓存大小可配置, 默认 10 GB

用法:
    from autocut_core.cache import CacheManager
    mgr = CacheManager(job_root)
    mgr.collect(max_size_gb=10)          # 常规 GC
    mgr.collect(max_size_gb=10, dry_run=True)  # 预览模式

Stage 执行缓存 (StageCache):
  - 缓存键: {schema_version}/{stage_version}/{stage_name}/{inputs_hash}
  - 存储路径: .sd-cache/checkpoints/{cache_key}/checkpoint.json
  - 缓存命中: 输入哈希一致且上次执行状态为 completed
  - 版本变化时缓存键前缀变化, 旧缓存自然失效

用法:
    from autocut_core.cache import StageCache
    sc = StageCache(job_root / ".sd-cache")
    if sc.hit("source_windows", inputs_hash):
        print("缓存命中, 跳过执行")
    sc.save(checkpoint)
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autocut_core.contracts.types import Checkpoint, StageStatus
from autocut_core.io import atomic_write_json, load_json
from autocut_core.logging import get_logger
from autocut_core.version import get_cache_key

logger = get_logger(__name__)

# 默认最大缓存大小 (GB)
_DEFAULT_MAX_SIZE_GB = 10.0
# 回收站目录名
_TRASH_DIR = ".gc-trash"
# 字节常量
_BYTES_PER_GB = 1024 ** 3


class CacheManager:
    """产物缓存 GC 管理器。

    职责:
      - 扫描 .sd-cache 目录下的缓存文件
      - 按 LRU 策略淘汰最久未访问的文件
      - 保护被 index.json 引用的活跃产物
      - 支持 dry-run 预览和 trash 可回滚删除
    """

    def __init__(self, job_root: Path) -> None:
        self._root = job_root.expanduser().resolve()
        self._cache = self._root / ".sd-cache"
        self._trash = self._cache / _TRASH_DIR

    # ── 公开 API ──────────────────────────────────────────────────────

    def collect(
        self,
        *,
        max_size_gb: float = _DEFAULT_MAX_SIZE_GB,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """执行 GC 收集。

        返回统计信息:
            {
                "total_size_gb": float,       # 当前缓存总大小
                "protected_count": int,       # 被保护的活跃产物数
                "evicted_count": int,         # 本次淘汰的文件数
                "evicted_size_gb": float,     # 本次淘汰释放的空间
                "remaining_size_gb": float,   # 淘汰后剩余大小
                "dry_run": bool,              # 是否为预览模式
                "trash": str,                 # 回收站路径 (仅非 dry_run)
            }
        """
        # 1. 收集活跃引用 (被 index.json 引用的产物)
        protected: set[str] = self._collect_protected()

        # 2. 扫描缓存文件 (排除 index.json, lock 文件, 回收站)
        cache_files = self._scan_cache_files()

        # 3. 计算总大小
        total_size = sum(f["size"] for f in cache_files)
        total_size_gb = total_size / _BYTES_PER_GB

        # 4. 按 LRU 排序 (atime 最旧的在前)
        cache_files.sort(key=lambda f: f["atime"])

        # 5. 淘汰直到低于阈值
        max_size_bytes = int(max_size_gb * _BYTES_PER_GB)
        evicted: list[dict[str, Any]] = []
        remaining = total_size

        for entry in cache_files:
            if remaining <= max_size_bytes:
                break
            # 跳过被保护的活跃产物
            if entry["path"] in protected:
                continue
            evicted.append(entry)
            remaining -= entry["size"]

        evicted_size_gb = sum(e["size"] for e in evicted) / _BYTES_PER_GB

        # 6. 执行删除 (或 dry-run 只报告)
        trash_path = ""
        if evicted and not dry_run:
            trash_path = str(self._trash)
            self._trash.mkdir(parents=True, exist_ok=True)
            for entry in evicted:
                self._move_to_trash(Path(entry["path"]))

        if evicted:
            logger.info(
                "GC: %s %d files (%.2f GB) → remaining %.2f GB (threshold %.1f GB)",
                "would evict" if dry_run else "evicted",
                len(evicted),
                evicted_size_gb,
                remaining / _BYTES_PER_GB,
                max_size_gb,
            )
        else:
            logger.debug(
                "GC: no eviction needed (%.2f GB ≤ %.1f GB threshold)",
                total_size_gb, max_size_gb,
            )

        return {
            "total_size_gb": round(total_size_gb, 3),
            "protected_count": len(protected),
            "evicted_count": len(evicted),
            "evicted_size_gb": round(evicted_size_gb, 3),
            "remaining_size_gb": round(remaining / _BYTES_PER_GB, 3),
            "dry_run": dry_run,
            "trash": trash_path,
        }

    def trash_size_gb(self) -> float:
        """返回回收站当前大小 (GB)。"""
        if not self._trash.is_dir():
            return 0.0
        total = 0
        for entry in self._trash.rglob("*"):
            if entry.is_file():
                total += entry.stat().st_size
        return total / _BYTES_PER_GB

    def empty_trash(self) -> int:
        """清空回收站, 返回删除的文件数。"""
        if not self._trash.is_dir():
            return 0
        count = 0
        for entry in self._trash.iterdir():
            if entry.is_file():
                entry.unlink()
                count += 1
            elif entry.is_dir():
                shutil.rmtree(entry)
                count += 1
        return count

    # ── 内部方法 ──────────────────────────────────────────────────────

    def _collect_protected(self) -> set[str]:
        """收集被 index.json 引用的活跃产物路径集合。

        这些产物仍在流水线中可用, 不能被 GC 淘汰。
        """
        protected: set[str] = set()
        index_path = self._cache / "index.json"
        if not index_path.is_file():
            return protected
        try:
            data = load_json(index_path)
        except (OSError, ValueError):
            return protected
        if not isinstance(data, dict):
            return protected
        for entry in data.values():
            if not isinstance(entry, dict):
                continue
            path = entry.get("_path", entry.get("path", ""))
            if path:
                protected.add(str(Path(path).expanduser().resolve()))
        return protected

    def _scan_cache_files(self) -> list[dict[str, Any]]:
        """扫描缓存目录下的文件, 排除索引/锁/回收站文件。

        返回列表, 每项含 path, size, atime。
        """
        files: list[dict[str, Any]] = []
        if not self._cache.is_dir():
            return files

        for entry in self._cache.rglob("*"):
            if not entry.is_file():
                continue
            name = entry.name
            # 排除索引/锁/回收站/临时文件
            if name == "index.json" or name.endswith(".lock"):
                continue
            if _TRASH_DIR in entry.parts:
                continue
            stat = entry.stat()
            files.append({
                "path": str(entry),
                "size": stat.st_size,
                "atime": stat.st_atime,
            })
        return files

    def _move_to_trash(self, file_path: Path) -> None:
        """将文件移到回收站目录, 保留原始相对路径结构。

        移动到回收站后, 文件仍可恢复 (手动从回收站移回原位置)。
        """
        rel = file_path.relative_to(self._cache)
        dest = self._trash / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(file_path), str(dest))
        except OSError:
            # 移动失败时直接删除 (如跨文件系统)
            file_path.unlink(missing_ok=True)


# ═══════════════════════════════════════════════════════════════════════════
# Stage 执行缓存 — 输入内容寻址 + 版本绑定
# ═══════════════════════════════════════════════════════════════════════════


class StageCache:
    """Stage 级执行缓存 — 输入内容寻址 + 版本绑定。

    输入未变时跳过执行, 复用产物。
    版本变化时缓存键前缀变化, 旧缓存自然失效。

    缓存键结构: {schema_version}/{stage_version}/{stage_name}/{inputs_hash}
    存储路径: .sd-cache/checkpoints/{cache_key}/checkpoint.json
    """

    def __init__(self, cache_root: Path):
        self._root = cache_root / "checkpoints"
        self._root.mkdir(parents=True, exist_ok=True)

    def _checkpoint_dir(self, stage: str, inputs_hash: str) -> Path:
        """返回检查点存储目录路径。"""
        key = get_cache_key(stage, inputs_hash)
        return self._root / key

    def load(self, stage: str, inputs_hash: str) -> Checkpoint | None:
        """从磁盘加载检查点; 不存在或损坏时返回 None。"""
        if not inputs_hash:
            return None
        path = self._checkpoint_dir(stage, inputs_hash) / "checkpoint.json"
        if not path.is_file():
            return None
        try:
            data = load_json(path)
            return Checkpoint(**data)
        except Exception as exc:
            logger.warning(
                "检查点加载失败, 将重新执行: stage=%s err=%s", stage, exc,
            )
            return None

    def save(self, checkpoint: Checkpoint) -> None:
        """持久化检查点到磁盘。"""
        if not checkpoint.inputs_hash:
            return
        path = self._checkpoint_dir(
            checkpoint.stage_name, checkpoint.inputs_hash,
        )
        path.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            path / "checkpoint.json",
            checkpoint.model_dump(mode="json"),
        )

    def hit(
        self, stage: str, inputs_hash: str, force: bool = False,
    ) -> bool:
        """检查缓存是否命中: 输入哈希一致且上次执行 completed。

        force=True 时强制绕过缓存, 总是返回 False。
        """
        if force:
            return False
        cp = self.load(stage, inputs_hash)
        if cp is None:
            return False
        return cp.status == StageStatus.COMPLETED

    def invalidate(self, stage: str) -> None:
        """使指定 Stage 的所有缓存检查点失效。

        删除该 Stage 的检查点目录, 下次执行时 _cache_hit() 返回 False。
        用于 recovery 回路中重置上游 Stage 状态。

        checkpoint 实际存储路径为 ``{checkpoints}/{schema_version}/
        {stage_version}/{stage}/{inputs_hash}/checkpoint.json``,
        因此需要递归查找所有版本前缀下的 stage 目录并删除。
        """
        import shutil

        # checkpoints/{schema_version}/{stage_version}/{stage}/...
        # 递归查找所有版本前缀下的 stage 目录
        for stage_dir in self._root.rglob(stage):
            if stage_dir.is_dir() and stage_dir.name == stage:
                shutil.rmtree(stage_dir, ignore_errors=True)
                logger.info(
                    "stage cache invalidated: stage=%s path=%s",
                    stage, stage_dir,
                )