"""Tests for _detect_format, _build_agent_instructions, _build_direct_instructions,
_build_mapreduce_instructions from source_script_load.

Uses clean-pytest methodology: AAA pattern with # Arrange / # Act / # Assert comments.
"""

from __future__ import annotations

import pytest

from auto_cut_bot.agent.tools.pipeline.source_script_load import (
    _build_agent_instructions,
    _build_direct_instructions,
    _build_mapreduce_instructions,
    _detect_format,
)


# ── _detect_format ──────────────────────────────────────────────────────────────


class TestDetectFormat:
    """Tests for _detect_format — script format detection."""

    def test_detect_format_chinese_numbered(self, sample_chinese_script: str) -> None:
        # Arrange — sample_chinese_script contains "1-1 学校操场 日 外" pattern

        # Act
        result = _detect_format(sample_chinese_script)

        # Assert
        assert result == "chinese_numbered"

    def test_detect_format_english_scene(self, sample_english_script: str) -> None:
        # Arrange — sample_english_script contains "Scene 1: The Meeting" pattern

        # Act
        result = _detect_format(sample_english_script)

        # Assert
        assert result == "english_scene"

    def test_detect_format_screenplay(self, sample_screenplay_script: str) -> None:
        # Arrange — sample_screenplay_script contains "INT. COFFEE SHOP - DAY" pattern

        # Act
        result = _detect_format(sample_screenplay_script)

        # Assert
        assert result == "screenplay"

    def test_detect_format_unknown(self, sample_unknown_format_script: str) -> None:
        # Arrange — no recognizable script format markers

        # Act
        result = _detect_format(sample_unknown_format_script)

        # Assert
        assert result == "unknown"

    def test_detect_format_sample_size_limit(self) -> None:
        """Only the first sample_size characters are examined."""
        # Arrange
        text = "a" * 6000 + "\n1-1 测试 日 外\n"

        # Act
        result = _detect_format(text, sample_size=5000)

        # Assert
        assert result == "unknown"


# ── _build_agent_instructions ───────────────────────────────────────────────────


class TestBuildAgentInstructions:
    """Tests for _build_agent_instructions — strategy dispatch."""

    def test_direct_strategy(self, sample_chinese_script: str, tmp_path) -> None:
        # Arrange
        script_path = tmp_path / "script.txt"
        script_path.write_text(sample_chinese_script, encoding="utf-8")

        # Act
        result = _build_agent_instructions(
            sample_chinese_script, "chinese_numbered", script_path, strategy="direct",
        )

        # Assert
        assert "DIRECT" in result
        assert "Format hint" in result
        assert "source_script_save" in result

    def test_mapreduce_strategy(self, sample_chinese_script: str, tmp_path) -> None:
        # Arrange
        script_path = tmp_path / "script.txt"
        script_path.write_text(sample_chinese_script, encoding="utf-8")

        # Act
        result = _build_agent_instructions(
            sample_chinese_script, "chinese_numbered", script_path, strategy="mapreduce",
        )

        # Assert
        assert "MAPREDUCE" in result
        assert "source_script_chunk_parse" in result
        assert "source_script_save" in result


# ── _build_direct_instructions ──────────────────────────────────────────────────


class TestBuildDirectInstructions:
    """Tests for _build_direct_instructions."""

    _FORMAT_HINTS = {
        "chinese_numbered": "Scene format: '1-2 墓地 雨夜 外'.",
        "english_scene": "Scene format: 'Scene 2: The Graveyard'.",
        "screenplay": "Screenplay format: 'SCENE 1-2 - INT. WORKSHOP - DAY'.",
        "unknown": "Scene format not auto-detected.",
    }

    def test_includes_format_hint(self, sample_chinese_script: str, tmp_path) -> None:
        # Arrange
        script_path = tmp_path / "script.txt"
        script_path.write_text(sample_chinese_script, encoding="utf-8")

        # Act
        result = _build_direct_instructions(
            sample_chinese_script, "chinese_numbered", self._FORMAT_HINTS, script_path,
        )

        # Assert
        assert "Format hint: chinese_numbered" in result
        assert "Scene format: '1-2 墓地 雨夜 外'" in result

    def test_includes_script_text(self, sample_chinese_script: str, tmp_path) -> None:
        # Arrange
        script_path = tmp_path / "script.txt"
        script_path.write_text(sample_chinese_script, encoding="utf-8")

        # Act
        result = _build_direct_instructions(
            sample_chinese_script, "chinese_numbered", self._FORMAT_HINTS, script_path,
        )

        # Assert
        assert "PARSING INSTRUCTIONS" in result
        assert "DIRECT" in result
        assert "source_script_save" in result

    def test_unknown_format_still_produces_instructions(self, tmp_path) -> None:
        # Arrange
        script_path = tmp_path / "script.txt"
        script_path.write_text("some text", encoding="utf-8")

        # Act
        result = _build_direct_instructions(
            "some text", "unknown", self._FORMAT_HINTS, script_path,
        )

        # Assert
        assert "PARSING INSTRUCTIONS" in result
        assert "Format hint: unknown" in result


# ── _build_mapreduce_instructions ───────────────────────────────────────────────


class TestBuildMapreduceInstructions:
    """Tests for _build_mapreduce_instructions."""

    _FORMAT_HINTS = {
        "chinese_numbered": "Scene format: '1-2 墓地 雨夜 外'.",
        "english_scene": "Scene format: 'Scene 2: The Graveyard'.",
        "screenplay": "Screenplay format: 'SCENE 1-2 - INT. WORKSHOP - DAY'.",
        "unknown": "Scene format not auto-detected.",
    }

    def test_includes_mapreduce_references(self, tmp_path) -> None:
        # Arrange
        script_path = tmp_path / "script.txt"
        script_path.write_text("test", encoding="utf-8")

        # Act
        result = _build_mapreduce_instructions(
            "chinese_numbered", self._FORMAT_HINTS, script_path,
        )

        # Assert
        assert "MAPREDUCE" in result
        assert "source_script_chunk_parse" in result
        assert "source_script_save" in result

    def test_includes_format_hint(self, tmp_path) -> None:
        # Arrange
        script_path = tmp_path / "script.txt"
        script_path.write_text("test", encoding="utf-8")

        # Act
        result = _build_mapreduce_instructions(
            "english_scene", self._FORMAT_HINTS, script_path,
        )

        # Assert
        assert "Format hint: english_scene" in result