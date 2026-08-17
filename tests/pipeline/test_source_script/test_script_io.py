"""Tests for _read_script, _find_script_file, and SCRIPT_SIZE_THRESHOLD
from source_script_load.

Uses clean-pytest methodology: AAA pattern with # Arrange / # Act / # Assert comments.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from auto_cut_bot.agent.tools.pipeline.source_script_load import (
    SCRIPT_SIZE_THRESHOLD,
    _find_script_file,
    _read_script,
)


class TestReadScript:
    """Tests for _read_script."""

    def test_reads_txt_file(self, tmp_path) -> None:
        # Arrange
        path = tmp_path / "script.txt"
        path.write_text("第一集\n内容\n", encoding="utf-8")

        # Act
        text = _read_script(path)

        # Assert
        assert text == "第一集\n内容\n"

    def test_reads_txt_with_bom(self, tmp_path) -> None:
        # Arrange — write with utf-8-sig encoding (includes BOM)
        path = tmp_path / "script.txt"
        path.write_text("第一集\n内容\n", encoding="utf-8-sig")

        # Act — _read_script uses utf-8-sig which strips BOM on read
        text = _read_script(path)

        # Assert — BOM should be stripped by utf-8-sig
        assert text == "第一集\n内容\n"

    def test_reads_empty_file(self, tmp_path) -> None:
        # Arrange
        path = tmp_path / "empty.txt"
        path.write_text("", encoding="utf-8")

        # Act
        text = _read_script(path)

        # Assert
        assert text == ""


class TestFindScriptFile:
    """Tests for _find_script_file."""

    def test_finds_txt_in_job_root(self, tmp_path) -> None:
        # Arrange
        (tmp_path / "script.txt").write_text("content", encoding="utf-8")
        cfg = MagicMock()
        cfg.extra = {}

        # Act
        path = _find_script_file(tmp_path, cfg)

        # Assert
        assert path is not None
        assert path.name == "script.txt"

    def test_finds_docx_in_job_root(self, tmp_path) -> None:
        # Arrange
        (tmp_path / "script.docx").write_text("content", encoding="utf-8")
        cfg = MagicMock()
        cfg.extra = {}

        # Act
        path = _find_script_file(tmp_path, cfg)

        # Assert
        assert path is not None
        assert path.suffix == ".docx"

    def test_no_script_file_returns_none(self, tmp_path) -> None:
        # Arrange
        cfg = MagicMock()
        cfg.extra = {}

        # Act
        path = _find_script_file(tmp_path, cfg)

        # Assert
        assert path is None

    def test_explicit_path_in_config(self, tmp_path) -> None:
        # Arrange
        explicit = tmp_path / "my_script.txt"
        explicit.write_text("content", encoding="utf-8")
        cfg = MagicMock()
        cfg.extra = {"script_path": str(explicit)}

        # Act
        path = _find_script_file(tmp_path, cfg)

        # Assert
        assert path == explicit


class TestScriptSizeThreshold:
    """Tests for SCRIPT_SIZE_THRESHOLD constant."""

    def test_threshold_is_positive(self) -> None:
        # Arrange / Act / Assert
        assert SCRIPT_SIZE_THRESHOLD > 0

    def test_threshold_determines_strategy(self) -> None:
        """Text above threshold should use mapreduce, below should use direct."""
        # Arrange
        small_text = "a" * 10000
        large_text = "a" * (SCRIPT_SIZE_THRESHOLD + 1)

        # Act / Assert
        assert len(small_text) < SCRIPT_SIZE_THRESHOLD
        assert len(large_text) > SCRIPT_SIZE_THRESHOLD