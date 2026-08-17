"""Tests for artifact-bus-cache: SHA256 content-addressing, incomplete marker, TTL cleanup."""

import json
import time
import pytest
from pathlib import Path
from auto_cut_bot.agent.runtime.artifact_cache import ArtifactCache, compute_cache_key


class TestComputeCacheKey:
    def test_same_input_same_key(self):
        k1 = compute_cache_key("hello", {"strategy": "direct"})
        k2 = compute_cache_key("hello", {"strategy": "direct"})
        assert k1 == k2

    def test_different_content_different_key(self):
        k1 = compute_cache_key("hello")
        k2 = compute_cache_key("world")
        assert k1 != k2

    def test_different_strategy_different_key(self):
        k1 = compute_cache_key("hello", {"strategy": "direct"})
        k2 = compute_cache_key("hello", {"strategy": "mapreduce"})
        assert k1 != k2

    def test_prompt_version_changes_key(self):
        k1 = compute_cache_key("hello", prompt_version="v1")
        k2 = compute_cache_key("hello", prompt_version="v2")
        assert k1 != k2

    def test_chunk_id_scopes_key(self):
        k1 = compute_cache_key("hello", chunk_id=1)
        k2 = compute_cache_key("hello", chunk_id=2)
        assert k1 != k2

    def test_no_prompt_version_same_as_none(self):
        k1 = compute_cache_key("hello")
        k2 = compute_cache_key("hello", prompt_version=None)
        assert k1 == k2


class TestArtifactCache:
    def test_put_and_get(self, tmp_path):
        cache = ArtifactCache(tmp_path, "test")
        key = compute_cache_key("data")
        cache.put(key, {"episodes": [1, 2, 3]})
        result = cache.get(key)
        assert result == {"episodes": [1, 2, 3]}

    def test_force_reparse_bypasses_cache(self, tmp_path):
        cache = ArtifactCache(tmp_path, "test")
        key = compute_cache_key("data")
        cache.put(key, {"episodes": [1, 2, 3]})
        result = cache.get(key, force_reparse=True)
        assert result is None

    def test_truncation_detection(self, tmp_path):
        cache = ArtifactCache(tmp_path, "test")
        key = compute_cache_key("data")
        cache.put(key, {"episodes": [1, 2]})
        result = cache.get(key, expected_count=45)
        assert result is None  # 2 < 45 * 0.5 = 22

    def test_valid_when_above_threshold(self, tmp_path):
        cache = ArtifactCache(tmp_path, "test")
        key = compute_cache_key("data")
        cache.put(key, {"episodes": list(range(30))})
        result = cache.get(key, expected_count=45)
        assert result is not None  # 30 >= 22

    def test_get_with_meta_incomplete(self, tmp_path):
        cache = ArtifactCache(tmp_path, "test")
        key = compute_cache_key("data")
        cache.put(key, {"episodes": [1, 2, 3]})
        data, meta = cache.get_with_meta(key)
        assert data is not None
        assert meta["incomplete"] is True
        assert meta["from_cache"] is True

    def test_get_with_meta_complete(self, tmp_path):
        cache = ArtifactCache(tmp_path, "test")
        key = compute_cache_key("data")
        cache.put(key, {"episodes": [1, 2, 3]})
        data, meta = cache.get_with_meta(key, expected_count=3)
        assert data is not None
        assert meta["incomplete"] is False
        assert meta["from_cache"] is True

    def test_invalidate(self, tmp_path):
        cache = ArtifactCache(tmp_path, "test")
        key = compute_cache_key("data")
        cache.put(key, {"x": 1})
        assert cache.invalidate(key) is True
        assert cache.get(key) is None
        assert cache.invalidate(key) is False

    def test_cleanup_expired(self, tmp_path):
        cache = ArtifactCache(tmp_path, "test")
        key = compute_cache_key("old")
        cache.put(key, {"x": 1})
        removed = cache.cleanup_expired(max_age_seconds=0)
        assert removed == 1
        assert cache.get(key) is None

    def test_cleanup_keeps_recent(self, tmp_path):
        cache = ArtifactCache(tmp_path, "test")
        key = compute_cache_key("recent")
        cache.put(key, {"x": 1})
        removed = cache.cleanup_expired(max_age_seconds=86400 * 365)
        assert removed == 0
        assert cache.get(key) is not None

    def test_parse_error_status_invalidates(self, tmp_path):
        cache = ArtifactCache(tmp_path, "test")
        key = compute_cache_key("bad")
        cache.put(key, {"episodes": [1], "_parse_meta": {"status": "parse_error"}})
        assert cache.get(key) is None

    def test_list_keys(self, tmp_path):
        cache = ArtifactCache(tmp_path, "test")
        cache.put("k1", {"x": 1})
        cache.put("k2", {"x": 2})
        keys = cache.list_keys()
        assert len(keys) == 2

    def test_clear(self, tmp_path):
        cache = ArtifactCache(tmp_path, "test")
        cache.put("k1", {"x": 1})
        assert cache.clear() == 1
        assert cache.list_keys() == []
