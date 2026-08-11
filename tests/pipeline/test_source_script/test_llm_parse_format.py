"""Tests for format detection, hint building, prompt construction, and guardrails.

Uses clean-pytest methodology: AAA pattern with # Arrange / # Act / # Assert comments.
"""

from __future__ import annotations

import pytest

from auto_cut_bot.pipeline.plugins.ac_source_prep.stages.source_script.llm_parse import (
    _build_format_hint,
    _build_guardrails,
    _build_prompt,
    _detect_format,
)


# ── _detect_format ──────────────────────────────────────────────────────────────────


class TestDetectFormat:
    """Tests for _detect_format — script format detection."""

    def test_detect_format_chinese_numbered(self, sample_chinese_script: str) -> None:
        # Arrange
        # sample_chinese_script contains "1-1 学校操场 日 外" pattern

        # Act
        result = _detect_format(sample_chinese_script)

        # Assert
        assert result == "chinese_numbered"

    def test_detect_format_english_scene(self, sample_english_script: str) -> None:
        # Arrange
        # sample_english_script contains "Scene 1: The Meeting" pattern

        # Act
        result = _detect_format(sample_english_script)

        # Assert
        assert result == "english_scene"

    def test_detect_format_screenplay(self, sample_screenplay_script: str) -> None:
        # Arrange
        # sample_screenplay_script contains "INT. COFFEE SHOP - DAY" pattern

        # Act
        result = _detect_format(sample_screenplay_script)

        # Assert
        assert result == "screenplay"

    def test_detect_format_unknown(self, sample_unknown_format_script: str) -> None:
        # Arrange
        # sample_unknown_format_script has no recognizable script format markers

        # Act
        result = _detect_format(sample_unknown_format_script)

        # Assert
        assert result == "unknown"


# ── _build_format_hint ──────────────────────────────────────────────────────────────


class TestBuildFormatHint:
    """Tests for _build_format_hint — format hint text generation."""

    def test_build_format_hint_chinese_numbered(self) -> None:
        # Arrange
        format_type = "chinese_numbered"

        # Act
        hint = _build_format_hint(format_type)

        # Assert
        assert "Chinese numbered scene format" in hint
        assert "S{episode}E{scene_order}" in hint

    def test_build_format_hint_english_scene(self) -> None:
        # Arrange
        format_type = "english_scene"

        # Act
        hint = _build_format_hint(format_type)

        # Assert
        assert "English scene format" in hint
        assert "Scene 2:" in hint

    def test_build_format_hint_screenplay(self) -> None:
        # Arrange
        format_type = "screenplay"

        # Act
        hint = _build_format_hint(format_type)

        # Assert
        assert "screenplay format" in hint
        assert "INT/EXT" in hint

    def test_build_format_hint_unknown_returns_empty(self) -> None:
        # Arrange
        format_type = "unknown"

        # Act
        hint = _build_format_hint(format_type)

        # Assert
        assert hint == ""

    def test_format_hint_unknown_key_fallback(self) -> None:
        # Arrange
        format_type = "nonexistent_format"

        # Act
        hint = _build_format_hint(format_type)

        # Assert
        assert hint == ""


# ── _build_prompt ───────────────────────────────────────────────────────────────────


class TestBuildPrompt:
    """Tests for _build_prompt — full LLM prompt construction."""

    def test_build_prompt_includes_system_prompt(self, sample_chinese_script: str) -> None:
        # Arrange
        format_type = "chinese_numbered"

        # Act
        prompt = _build_prompt(sample_chinese_script, format_type)

        # Assert
        assert "You are a script parser" in prompt

    def test_build_prompt_includes_format_hint(self, sample_chinese_script: str) -> None:
        # Arrange
        format_type = "chinese_numbered"

        # Act
        prompt = _build_prompt(sample_chinese_script, format_type)

        # Assert
        assert "Format hint:" in prompt
        assert "Chinese numbered scene format" in prompt

    def test_build_prompt_includes_guardrails(self, sample_chinese_script: str) -> None:
        # Arrange
        format_type = "chinese_numbered"
        guardrails = "IMPORTANT: Test guardrail message"

        # Act
        prompt = _build_prompt(sample_chinese_script, format_type, guardrails)

        # Assert
        assert "IMPORTANT: Test guardrail message" in prompt

    def test_build_prompt_includes_script_text(self, sample_chinese_script: str) -> None:
        # Arrange
        format_type = "chinese_numbered"

        # Act
        prompt = _build_prompt(sample_chinese_script, format_type)

        # Assert
        assert "Script text:" in prompt
        assert sample_chinese_script in prompt


# ── _build_guardrails ───────────────────────────────────────────────────────────────


class TestBuildGuardrails:
    """Tests for _build_guardrails — retry guardrail construction."""

    def test_build_guardrails_constructs_error_list(self) -> None:
        # Arrange
        expected_count = 5
        errors = ["Error 1", "Error 2"]

        # Act
        result = _build_guardrails(expected_count, errors)

        # Assert
        assert "exactly 5 episodes" in result
        assert "Previous error: Error 1" in result
        assert "Previous error: Error 2" in result

    def test_build_guardrails_empty_errors(self) -> None:
        # Arrange
        expected_count = 3
        errors: list[str] = []

        # Act
        result = _build_guardrails(expected_count, errors)

        # Assert
        assert "exactly 3 episodes" in result
        assert "Previous error" not in result

    def test_build_guardrails_includes_expected_count(self) -> None:
        # Arrange
        expected_count = 10
        errors = ["Missing episodes"]

        # Act
        result = _build_guardrails(expected_count, errors)

        # Assert
        assert "exactly 10 episodes" in result
        assert "Previous error: Missing episodes" in result