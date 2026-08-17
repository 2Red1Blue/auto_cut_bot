"""Tests for _find_episode_boundaries, _count_episode_markers, _compute_chunk_plan,
and _fallback_char_chunks from source_script_load.

Uses clean-pytest methodology: AAA pattern with # Arrange / # Act / # Assert comments.
"""

from __future__ import annotations

import pytest

from auto_cut_bot.agent.tools.pipeline.source_script_load import (
    _compute_chunk_plan,
    _count_episode_markers,
    _fallback_char_chunks,
    _find_episode_boundaries,
)


# ── _find_episode_boundaries ────────────────────────────────────────────────────


class TestFindEpisodeBoundaries:
    """Tests for _find_episode_boundaries."""

    def test_finds_chinese_episode_markers(self) -> None:
        # Arrange
        text = "第1集 开始\n内容\n第2集 发展\n内容\n第3集 结束\n"

        # Act
        positions = _find_episode_boundaries(text)

        # Assert
        assert len(positions) == 3
        assert positions == sorted(positions)

    def test_finds_english_episode_markers(self) -> None:
        # Arrange
        text = "Episode 1\ncontent\nEpisode 2\nmore\nEpisode 3\nend\n"

        # Act
        positions = _find_episode_boundaries(text)

        # Assert
        assert len(positions) == 3

    def test_no_boundaries_returns_empty(self) -> None:
        # Arrange
        text = "There are no episode markers here at all."

        # Act
        positions = _find_episode_boundaries(text)

        # Assert
        assert positions == []

    def test_mixed_patterns(self) -> None:
        # Arrange
        text = "第1集\ncontent\nEpisode 2\nmore\nEP 3\nend\n"

        # Act
        positions = _find_episode_boundaries(text)

        # Assert
        assert len(positions) == 3


# ── _count_episode_markers ──────────────────────────────────────────────────────


class TestCountEpisodeMarkers:
    """Tests for _count_episode_markers."""

    def test_returns_max_episode_number(self) -> None:
        # Arrange
        text = "第1集\n第5集\n第3集\n"

        # Act
        count = _count_episode_markers(text)

        # Assert
        assert count == 5

    def test_no_markers_returns_zero(self) -> None:
        # Arrange
        text = "No markers."

        # Act
        count = _count_episode_markers(text)

        # Assert
        assert count == 0

    def test_english_markers(self) -> None:
        # Arrange
        text = "Episode 1\nEpisode 10\n"

        # Act
        count = _count_episode_markers(text)

        # Assert
        assert count == 10

    def test_ep_markers(self) -> None:
        # Arrange
        text = "EP 1\nEP 8\n"

        # Act
        count = _count_episode_markers(text)

        # Assert
        assert count == 8


# ── _compute_chunk_plan ─────────────────────────────────────────────────────────


class TestComputeChunkPlan:
    """Tests for _compute_chunk_plan."""

    def test_no_boundaries_falls_back_to_char_chunks(self) -> None:
        # Arrange
        text = "a" * 100000

        # Act
        chunks = _compute_chunk_plan(text, target_chars_per_chunk=30000)

        # Assert
        assert len(chunks) >= 3
        for chunk in chunks:
            assert "chunk_id" in chunk
            assert "char_start" in chunk
            assert "char_end" in chunk
            assert "char_count" in chunk

    def test_small_text_produces_single_chunk(self) -> None:
        # Arrange
        text = "第一集\n内容\n"

        # Act
        chunks = _compute_chunk_plan(text, target_chars_per_chunk=30000)

        # Assert
        assert len(chunks) == 1
        assert chunks[0]["char_start"] == 0
        assert chunks[0]["char_end"] == len(text)

    def test_chunks_are_contiguous(self) -> None:
        # Arrange
        text = "第1集\n" + "a" * 10000 + "第2集\n" + "b" * 10000

        # Act
        chunks = _compute_chunk_plan(text, target_chars_per_chunk=30000)

        # Assert
        assert len(chunks) >= 1
        for i in range(1, len(chunks)):
            assert chunks[i]["char_start"] >= chunks[i - 1]["char_end"]

    def test_chunks_cover_full_text(self) -> None:
        # Arrange
        text = "第1集\n" + "a" * 10000 + "第2集\n" + "b" * 10000

        # Act
        chunks = _compute_chunk_plan(text, target_chars_per_chunk=30000)

        # Assert
        assert chunks[0]["char_start"] == 0
        assert chunks[-1]["char_end"] == len(text)


# ── _fallback_char_chunks ───────────────────────────────────────────────────────


class TestFallbackCharChunks:
    """Tests for _fallback_char_chunks."""

    def test_splits_evenly(self) -> None:
        # Arrange
        text = "a" * 90000

        # Act
        chunks = _fallback_char_chunks(text, target_chars=30000)

        # Assert
        assert len(chunks) == 3
        assert chunks[0]["chunk_id"] == "chunk_1"
        assert chunks[1]["chunk_id"] == "chunk_2"
        assert chunks[2]["chunk_id"] == "chunk_3"

    def test_single_chunk_for_small_text(self) -> None:
        # Arrange
        text = "short"

        # Act
        chunks = _fallback_char_chunks(text, target_chars=30000)

        # Assert
        assert len(chunks) == 1
        assert chunks[0]["char_count"] == len(text)

    def test_chunks_are_contiguous(self) -> None:
        # Arrange
        text = "a" * 50000

        # Act
        chunks = _fallback_char_chunks(text, target_chars=20000)

        # Assert
        for i in range(1, len(chunks)):
            assert chunks[i]["char_start"] == chunks[i - 1]["char_end"]
        assert chunks[0]["char_start"] == 0
        assert chunks[-1]["char_end"] == len(text)