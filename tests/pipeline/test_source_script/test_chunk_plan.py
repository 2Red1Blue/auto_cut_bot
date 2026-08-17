"""Tests for _compute_chunk_plan edge cases, _fallback_char_chunks, and
chunk plan validation from source_script_load.

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


class TestChunkPlanEdgeCases:
    """Edge case tests for _compute_chunk_plan."""

    def test_chunk_plan_includes_chunk_ids(self) -> None:
        # Arrange
        text = "第1集\n" + "a" * 5000 + "第2集\n" + "b" * 5000

        # Act
        chunks = _compute_chunk_plan(text, target_chars_per_chunk=30000)

        # Assert
        for i, chunk in enumerate(chunks, 1):
            assert chunk["chunk_id"] == f"chunk_{i}"

    def test_chunk_plan_includes_episode_ranges(self) -> None:
        # Arrange
        text = "第1集\n" + "a" * 5000 + "第2集\n" + "b" * 5000 + "第3集\n" + "c" * 5000

        # Act
        chunks = _compute_chunk_plan(text, target_chars_per_chunk=30000)

        # Assert
        for chunk in chunks:
            assert "episode_range" in chunk

    def test_large_single_segment_gets_split(self) -> None:
        # Arrange — text with one boundary but very large content
        text = "第1集\n" + "a" * 100000

        # Act
        chunks = _compute_chunk_plan(text, target_chars_per_chunk=30000)

        # Assert
        assert len(chunks) >= 3

    def test_empty_text_produces_empty_plan(self) -> None:
        # Arrange
        text = ""

        # Act
        chunks = _compute_chunk_plan(text)

        # Assert
        # Empty text has no chunk boundaries, so the fallback produces 0 chunks
        assert len(chunks) == 0


class TestFallbackCharChunksEdgeCases:
    """Edge case tests for _fallback_char_chunks."""

    def test_empty_text_produces_empty_plan(self) -> None:
        # Arrange
        text = ""

        # Act
        chunks = _fallback_char_chunks(text, target_chars=30000)

        # Assert
        # Empty text produces 0 chunks
        assert len(chunks) == 0

    def test_exact_multiple(self) -> None:
        # Arrange
        text = "a" * 60000

        # Act
        chunks = _fallback_char_chunks(text, target_chars=30000)

        # Assert
        assert len(chunks) == 2
        assert chunks[0]["char_count"] == 30000
        assert chunks[1]["char_count"] == 30000

    def test_episode_range_is_placeholder(self) -> None:
        # Arrange
        text = "a" * 50000

        # Act
        chunks = _fallback_char_chunks(text, target_chars=20000)

        # Assert
        for chunk in chunks:
            assert "episode_range" in chunk


class TestChunkPlanConsistency:
    """Integration-style tests for chunk plan consistency."""

    def test_boundaries_then_chunks_produces_valid_plan(self) -> None:
        # Arrange
        text = "第1集\n" + "a" * 5000 + "第2集\n" + "b" * 5000 + "第3集\n" + "c" * 5000

        # Act
        boundaries = _find_episode_boundaries(text)
        chunks = _compute_chunk_plan(text, target_chars_per_chunk=30000)

        # Assert
        assert len(boundaries) == 3
        assert len(chunks) >= 1
        for chunk in chunks:
            assert 0 <= chunk["char_start"] < chunk["char_end"] <= len(text)

    def test_estimated_count_matches_boundaries(self) -> None:
        # Arrange
        text = "第1集\ncontent\n第2集\nmore\n第3集\nend\n"

        # Act
        boundaries = _find_episode_boundaries(text)
        count = _count_episode_markers(text)

        # Assert
        assert len(boundaries) == 3
        assert count == 3