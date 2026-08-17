"""Tests for _validate_episodes from source_script_save.

Uses clean-pytest methodology: AAA pattern with # Arrange / # Act / # Assert comments.
"""

from __future__ import annotations

import pytest

from auto_cut_bot.agent.tools.pipeline.source_script_save import _validate_episodes


class TestValidateEpisodes:
    """Tests for _validate_episodes."""

    def test_valid_episodes_no_errors(self, sample_parsed_result) -> None:
        # Arrange
        meta = {"expected_count": 2}

        # Act
        errors = _validate_episodes(sample_parsed_result["episodes"], meta)

        # Assert
        assert errors == []

    def test_empty_episodes_returns_error(self) -> None:
        # Arrange
        meta = {"expected_count": 5}

        # Act
        errors = _validate_episodes([], meta)

        # Assert
        assert len(errors) > 0

    def test_expected_count_mismatch(self, sample_parsed_result) -> None:
        # Arrange — sample_parsed_result has 2 episodes
        meta = {"expected_count": 5}

        # Act
        errors = _validate_episodes(sample_parsed_result["episodes"], meta)

        # Assert
        assert any("Expected" in e for e in errors)

    def test_first_episode_not_one(self) -> None:
        # Arrange
        episodes = [{"episode_number": 3, "scenes": [{"scene_id": "S3E1", "scene_order": 1}]}]
        meta = {}

        # Act
        errors = _validate_episodes(episodes, meta)

        # Assert
        assert any("1" in e for e in errors)

    def test_missing_episodes_gap(self) -> None:
        # Arrange
        episodes = [
            {"episode_number": 1, "scenes": [{"scene_id": "S1E1", "scene_order": 1}]},
            {"episode_number": 3, "scenes": [{"scene_id": "S3E1", "scene_order": 1}]},
            {"episode_number": 4, "scenes": [{"scene_id": "S4E1", "scene_order": 1}]},
        ]
        meta = {}

        # Act
        errors = _validate_episodes(episodes, meta)

        # Assert
        assert any("Missing" in e or "gap" in e.lower() for e in errors)

    def test_single_episode(self) -> None:
        # Arrange
        episodes = [{"episode_number": 1, "scenes": [{"scene_id": "S1E1", "scene_order": 1}]}]
        meta = {"expected_count": 1}

        # Act
        errors = _validate_episodes(episodes, meta)

        # Assert
        assert errors == []

    def test_episode_count_matches_expected(self, sample_parsed_result) -> None:
        # Arrange
        meta = {"expected_count": 2}

        # Act
        errors = _validate_episodes(sample_parsed_result["episodes"], meta)

        # Assert
        assert not any("Expected" in e and "got" in e for e in errors)