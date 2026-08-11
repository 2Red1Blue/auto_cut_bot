"""Tests for _validate_episode_boundaries."""

from __future__ import annotations

import pytest
from auto_cut_bot.pipeline.plugins.ac_source_prep.stages.source_script.llm_parse import (
    _validate_episode_boundaries,
)


class TestValidateEpisodeBoundaries:
    """Tests for _validate_episode_boundaries."""

    def test_validate_empty_episodes_returns_error(self):
        """Empty episodes list returns 'No episodes found' error."""
        result = {"episodes": []}

        errors = _validate_episode_boundaries(result)

        assert len(errors) == 1
        assert "No episodes found" in errors[0]

    def test_validate_expected_count_mismatch(self, sample_parsed_result):
        """expected_count differs from actual episode count."""
        expected_count = 5  # sample_parsed_result has 2 episodes

        errors = _validate_episode_boundaries(sample_parsed_result, expected_count)

        assert any("Expected 5 episodes" in e for e in errors)

    def test_validate_first_episode_not_one(self):
        """First episode number is not 1."""
        result = {
            "episodes": [
                {
                    "episode_number": 3,
                    "scenes": [{"scene_id": "S3E1", "scene_order": 1}],
                },
            ]
        }

        errors = _validate_episode_boundaries(result)

        assert any("First episode should be 1" in e for e in errors)

    def test_validate_missing_episodes_gap(self):
        """Episode numbers have a gap (e.g. 1, 3, 4 missing 2)."""
        result = {
            "episodes": [
                {"episode_number": 1, "scenes": [{"scene_id": "S1E1", "scene_order": 1}]},
                {"episode_number": 3, "scenes": [{"scene_id": "S3E1", "scene_order": 1}]},
                {"episode_number": 4, "scenes": [{"scene_id": "S4E1", "scene_order": 1}]},
            ]
        }

        errors = _validate_episode_boundaries(result)

        assert any("Missing episodes" in e for e in errors)
        assert any("2" in e for e in errors)  # episode 2 is missing

    def test_validate_outlier_scene_counts_too_few(self):
        """Episode with far fewer scenes than the mean triggers outlier error."""
        result = {
            "episodes": [
                {"episode_number": 1, "scenes": [
                    {"scene_id": "S1E1", "scene_order": 1},
                    {"scene_id": "S1E2", "scene_order": 2},
                    {"scene_id": "S1E3", "scene_order": 3},
                ]},
                {"episode_number": 2, "scenes": [
                    {"scene_id": "S2E1", "scene_order": 1},
                    {"scene_id": "S2E2", "scene_order": 2},
                    {"scene_id": "S2E3", "scene_order": 3},
                ]},
                # mean=3.0, 0.3*mean=0.9, this episode has 0 scenes (< 0.9) -> outlier
                {"episode_number": 3, "scenes": []},
            ]
        }

        errors = _validate_episode_boundaries(result)

        assert any("outlier scene counts" in e for e in errors)

    def test_validate_outlier_scene_counts_too_many(self):
        """Episode with far more scenes than the mean triggers outlier error."""
        result = {
            "episodes": [
                {"episode_number": 1, "scenes": [
                    {"scene_id": "S1E1", "scene_order": 1},
                ]},
                {"episode_number": 2, "scenes": [
                    {"scene_id": "S2E1", "scene_order": 1},
                ]},
                {"episode_number": 3, "scenes": [
                    {"scene_id": "S3E1", "scene_order": 1},
                ]},
                # mean = (1+1+1+10)/4 = 3.25, 3.0*mean = 9.75, 10 > 9.75 -> outlier
                {"episode_number": 4, "scenes": [
                    {"scene_id": "S4E1", "scene_order": 1},
                    {"scene_id": "S4E2", "scene_order": 2},
                    {"scene_id": "S4E3", "scene_order": 3},
                    {"scene_id": "S4E4", "scene_order": 4},
                    {"scene_id": "S4E5", "scene_order": 5},
                    {"scene_id": "S4E6", "scene_order": 6},
                    {"scene_id": "S4E7", "scene_order": 7},
                    {"scene_id": "S4E8", "scene_order": 8},
                    {"scene_id": "S4E9", "scene_order": 9},
                    {"scene_id": "S4E10", "scene_order": 10},
                ]},
            ]
        }

        errors = _validate_episode_boundaries(result)

        assert any("outlier scene counts" in e for e in errors)

    def test_validate_normal_distribution_no_errors(self, sample_parsed_result):
        """Valid result with expected count produces no errors."""
        errors = _validate_episode_boundaries(sample_parsed_result, expected_count=2)

        assert errors == []

    def test_validate_single_episode(self):
        """Single episode skips scene count outlier detection."""
        result = {
            "episodes": [
                {
                    "episode_number": 1,
                    "scenes": [{"scene_id": "S1E1", "scene_order": 1}],
                },
            ]
        }

        errors = _validate_episode_boundaries(result)

        assert errors == []

    def test_validate_episode_count_matches_expected(self, sample_parsed_result):
        """When expected_count matches actual episode count, no count mismatch error."""
        errors = _validate_episode_boundaries(sample_parsed_result, expected_count=2)

        assert not any("Expected" in e and "episodes, got" in e for e in errors)
