"""Content-addressed artifact cache with truncation detection and invalidation.

Consolidates cache logic previously scattered across source_script_load.py
and prompt_context.py into a single reusable module.

Cache key = sha256(content + serialized strategy_params), providing
content-addressable storage that naturally invalidates when inputs change.

Usage::

    cache = ArtifactCache(job_root)
    if cache.is_valid(key, expected_count=45):
        data = cache.get(key)
    else:
        data = compute_expensive_result()
        cache.put(key, data)

Truncation detection:
    If cached episodes < expected_count * 0.5, the entry is treated as
    invalid — this prevents the "45-episode script cached as 2 episodes"
    class of bugs.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from autocut_core.io import atomic_write_json, load_json
from autocut_core.logging import get_logger

logger = get_logger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

_CACHE_ROOT = ".sd-cache"
_TRUNCATION_THRESHOLD = 0.5  # cached episodes < expected * threshold → invalid


def compute_cache_key(
    content: str,
    strategy_params: dict[str, Any] | None = None,
    *,
    prompt_version: str | None = None,
    chunk_id: int | None = None,
) -> str:
    """Compute a content-addressed cache key.

    key = sha256(content + strategy_params + prompt_version + chunk_id)[:16]

    When prompt_version changes, old caches are automatically invalidated.
    When chunk_id is set, the key is scoped to a single chunk (MapReduce).
    """
    payload = content
    if strategy_params:
        payload += json.dumps(strategy_params, sort_keys=True, ensure_ascii=False)
    if prompt_version:
        payload += f"::pv={prompt_version}"
    if chunk_id is not None:
        payload += f"::chunk={chunk_id}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class ArtifactCache:
    """SHA256 content-addressed cache with truncation detection.

    Stores JSON-serializable data in ``.sd-cache/{namespace}/{key}.json``.
    Supports force_reparse bypass and automatic invalidation when cached
    results appear truncated.

    Parameters
    ----------
    job_root : Path
        Root directory of the pipeline job. The cache directory is created
        at ``job_root / .sd-cache / {namespace} /``.
    namespace : str
        Subdirectory name under .sd-cache to isolate different artifact types
        (e.g. ``"source_script"``, ``"episode_digests"``).
    """

    def __init__(self, job_root: Path, namespace: str = "artifacts") -> None:
        self._root = job_root.expanduser().resolve()
        self._namespace = namespace
        self._cache_dir = self._root / _CACHE_ROOT / namespace

    # ── Public API ──────────────────────────────────────────────────────────

    def get(
        self,
        key: str,
        *,
        expected_count: int | None = None,
        force_reparse: bool = False,
    ) -> dict[str, Any] | None:
        """Load cached data. Returns None on miss, corruption, or truncation.

        When expected_count is None, cached data is returned but marked
        ``incomplete`` — callers should verify completeness themselves.
        Use ``get_with_meta`` if you need the incomplete flag.
        """
        result, _meta = self.get_with_meta(
            key, expected_count=expected_count, force_reparse=force_reparse,
        )
        return result

    def get_with_meta(
        self,
        key: str,
        *,
        expected_count: int | None = None,
        force_reparse: bool = False,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        """Load cached data with metadata.

        Returns (data, meta) where meta includes:
        - ``incomplete``: True when expected_count was None and cache exists
        - ``truncated``: True when cache was truncated and auto-invalidated
        - ``from_cache``: True when data was served from cache

        Callers should check meta['incomplete'] before trusting the result
        when expected_count was not known.
        """
        meta: dict[str, Any] = {"incomplete": False, "truncated": False, "from_cache": False}

        if force_reparse:
            return None, meta

        if not self.is_valid(key, expected_count=expected_count):
            return None, meta

        cache_path = self._cache_path(key)
        try:
            data = load_json(cache_path)
        except (OSError, json.JSONDecodeError):
            logger.warning("Cache read failed, invalidating: %s", cache_path)
            self.invalidate(key)
            return None, meta

        # Mark incomplete when expected_count was not provided
        if expected_count is None:
            meta["incomplete"] = True
            logger.debug(
                "Cache hit but expected_count unknown — marking incomplete: key=%s", key,
            )

        meta["from_cache"] = True
        return data, meta

    def put(self, key: str, data: dict[str, Any]) -> None:
        """Persist data to the cache.

        Uses atomic write to prevent corruption from concurrent writes
        or partial flushes.
        """
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = self._cache_path(key)
        atomic_write_json(cache_path, data)
        logger.debug("Cache written: %s", cache_path)

    def invalidate(self, key: str) -> bool:
        """Remove a cached entry. Returns True if the entry was deleted."""
        cache_path = self._cache_path(key)
        if cache_path.is_file():
            cache_path.unlink()
            logger.debug("Cache invalidated: %s", cache_path)
            return True
        return False

    def is_valid(
        self,
        key: str,
        *,
        expected_count: int | None = None,
        force_reparse: bool = False,
    ) -> bool:
        """Check whether a valid cache entry exists.

        Returns False if:
        - force_reparse is True
        - Cache file is missing or corrupt
        - ``parse_metadata.status`` is ``"parse_error"`` or ``"failed"``
        - Cached ``episodes`` count < ``expected_count * TRUNCATION_THRESHOLD``
          (clearly truncated)
        """
        if force_reparse:
            return False

        cache_path = self._cache_path(key)
        if not cache_path.is_file():
            return False

        try:
            cached = load_json(cache_path)
        except (OSError, json.JSONDecodeError):
            return False

        if not isinstance(cached, dict):
            return False

        # Check parse status — explicit failure/degraded markers always invalidate
        meta = cached.get("_parse_meta") or cached.get("parse_metadata") or {}
        if meta.get("status") in ("parse_error", "failed", "unavailable", "error"):
            return False

        # Truncation detection
        if expected_count is not None:
            episodes = cached.get("episodes", [])
            if isinstance(episodes, list):
                min_viable = int(expected_count * _TRUNCATION_THRESHOLD)
                if len(episodes) < min_viable:
                    logger.warning(
                        "Cache truncated: %d episodes < %d expected (threshold=%d), "
                        "invalidating key=%s",
                        len(episodes), expected_count, min_viable, key,
                    )
                    self.invalidate(key)
                    return False

        return True

    def list_keys(self) -> list[str]:
        """List all cache keys in this namespace."""
        if not self._cache_dir.is_dir():
            return []
        return [
            p.stem for p in self._cache_dir.glob("*.json")
            if p.is_file()
        ]

    def clear(self) -> int:
        """Remove all cached entries in this namespace. Returns count removed."""
        if not self._cache_dir.is_dir():
            return 0
        count = 0
        for cache_file in self._cache_dir.glob("*.json"):
            cache_file.unlink()
            count += 1
        if count:
            logger.info("Cache cleared: namespace=%s count=%d", self._namespace, count)
        return count

    def cleanup_expired(self, max_age_seconds: int = 86400 * 7) -> int:
        """Remove cache entries older than max_age_seconds.

        Default TTL is 7 days. Returns count of removed entries.
        Used by periodic cleanup to control storage growth (R-AB.3.3).
        """
        import time

        if not self._cache_dir.is_dir():
            return 0
        count = 0
        cutoff = time.time() - max_age_seconds
        for cache_file in self._cache_dir.glob("*.json"):
            try:
                if cache_file.stat().st_mtime < cutoff:
                    cache_file.unlink()
                    count += 1
            except OSError:
                pass
        if count:
            logger.info(
                "Cache cleanup: namespace=%s removed=%d max_age=%ds",
                self._namespace, count, max_age_seconds,
            )
        return count

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _cache_path(self, key: str) -> Path:
        """Resolve the full filesystem path for a cache key."""
        # Sanitize key to prevent path traversal
        safe_key = Path(key).name if "/" in key or "\\" in key else key
        return self._cache_dir / f"{safe_key}.json"