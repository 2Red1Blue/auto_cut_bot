"""Tests for confidence scoring, low-confidence scene detection, and scene context extraction."""

import pytest
from auto_cut_bot.pipeline.plugins.ac_source_prep.stages.source_script.llm_parse import (
    _ensure_confidence_scores,
    _find_low_confidence_scenes,
    _extract_scene_context,
)


class TestEnsureConfidenceScores:
    def test_backfills_missing(self):
        """Scenes without confidence get default 1.0."""
        result = {
            "episodes": [{
                "episode_number": 1,
                "scenes": [
                    {"scene_id": "S1E1", "scene_order": 1},
                    {"scene_id": "S1E2", "scene_order": 2},
                ],
            }]
        }
        updated = _ensure_confidence_scores(result)
        for ep in updated["episodes"]:
            for scene in ep["scenes"]:
                assert scene["confidence"] == 1.0

    def test_noop_when_all_present(self, sample_parsed_result):
        """When all scenes already have confidence, values are unchanged."""
        updated = _ensure_confidence_scores(sample_parsed_result)
        for orig_ep, upd_ep in zip(sample_parsed_result["episodes"], updated["episodes"]):
            for orig_scene, upd_scene in zip(orig_ep["scenes"], upd_ep["scenes"]):
                assert upd_scene["confidence"] == orig_scene["confidence"]

    def test_empty_episodes(self):
        """Empty episodes list passes through unchanged."""
        result: dict = {"episodes": []}
        updated = _ensure_confidence_scores(result)
        assert updated["episodes"] == []

    def test_mixed_present_missing(self):
        """Only scenes missing confidence are backfilled; existing values preserved."""
        result = {
            "episodes": [{
                "episode_number": 1,
                "scenes": [
                    {"scene_id": "S1E1", "scene_order": 1, "confidence": 0.85},
                    {"scene_id": "S1E2", "scene_order": 2},
                    {"scene_id": "S1E3", "scene_order": 3, "confidence": 0.92},
                ],
            }]
        }
        updated = _ensure_confidence_scores(result)
        scenes = updated["episodes"][0]["scenes"]
        assert scenes[0]["confidence"] == 0.85
        assert scenes[1]["confidence"] == 1.0
        assert scenes[2]["confidence"] == 0.92


class TestFindLowConfidenceScenes:
    def test_threshold_boundary_equal(self):
        """Confidence exactly at threshold is NOT considered low (< is strict)."""
        result = {
            "episodes": [{
                "episode_number": 1,
                "scenes": [
                    {"scene_id": "S1E1", "scene_order": 1, "confidence": 0.70},
                ],
            }]
        }
        low = _find_low_confidence_scenes(result, threshold=0.70)
        assert low == []

    def test_threshold_above(self):
        """Confidence above threshold is NOT low."""
        result = {
            "episodes": [{
                "episode_number": 1,
                "scenes": [
                    {"scene_id": "S1E1", "scene_order": 1, "confidence": 0.85},
                ],
            }]
        }
        low = _find_low_confidence_scenes(result, threshold=0.70)
        assert low == []

    def test_threshold_below(self):
        """Confidence below threshold IS detected as low."""
        result = {
            "episodes": [{
                "episode_number": 1,
                "scenes": [
                    {"scene_id": "S1E1", "scene_order": 1, "confidence": 0.55},
                ],
            }]
        }
        low = _find_low_confidence_scenes(result, threshold=0.70)
        assert len(low) == 1
        assert low[0]["scene"]["scene_id"] == "S1E1"
        assert low[0]["confidence"] == 0.55

    def test_empty_episodes(self):
        """Empty episodes list returns empty result."""
        result: dict = {"episodes": []}
        low = _find_low_confidence_scenes(result, threshold=0.70)
        assert low == []

    def test_multiple_below(self):
        """Multiple scenes below threshold are all returned."""
        result = {
            "episodes": [
                {
                    "episode_number": 1,
                    "scenes": [
                        {"scene_id": "S1E1", "scene_order": 1, "confidence": 0.50},
                        {"scene_id": "S1E2", "scene_order": 2, "confidence": 0.90},
                        {"scene_id": "S1E3", "scene_order": 3, "confidence": 0.45},
                    ],
                },
                {
                    "episode_number": 2,
                    "scenes": [
                        {"scene_id": "S2E1", "scene_order": 1, "confidence": 0.62},
                        {"scene_id": "S2E2", "scene_order": 2, "confidence": 0.88},
                    ],
                },
            ]
        }
        low = _find_low_confidence_scenes(result, threshold=0.70)
        assert len(low) == 3
        scene_ids = {item["scene"]["scene_id"] for item in low}
        assert scene_ids == {"S1E1", "S1E3", "S2E1"}

    def test_none_below_threshold(self, sample_parsed_result):
        """All scenes above threshold returns empty list."""
        low = _find_low_confidence_scenes(sample_parsed_result, threshold=0.70)
        assert low == []


class TestExtractSceneContext:
    def test_match_by_heading(self):
        """Scene heading matches a line in the script text."""
        lines = [
            "some previous line",
            "another line",
            "1-1 墓地 雨夜 外",
            "Lucifer：Humans call me Satan.",
            "more dialogue here",
            "even more text",
            "final line",
        ]
        scene = {"heading": "1-1 墓地 雨夜 外", "location": "墓地"}
        context = _extract_scene_context(lines, scene, context_lines=2)
        assert "1-1 墓地 雨夜 外" in context
        assert "some previous line" in context
        assert "final line" not in context

    def test_match_by_location(self):
        """When heading is empty, falls back to location matching."""
        lines = [
            "intro text",
            "墓地 雨夜 外",
            "Lucifer walks through the graveyard.",
            "Lucifer：Greetings.",
            "trailing text",
        ]
        scene = {"heading": "", "location": "墓地 雨夜 外"}
        context = _extract_scene_context(lines, scene, context_lines=1)
        assert "墓地 雨夜 外" in context
        assert "Lucifer walks" in context

    def test_no_match(self):
        """When neither heading nor location match, returns empty string."""
        lines = [
            "completely unrelated line",
            "no match here either",
            "still nothing",
        ]
        scene = {"heading": "9-9 不存在 幻 外", "location": "不存在"}
        context = _extract_scene_context(lines, scene, context_lines=2)
        assert context == ""
