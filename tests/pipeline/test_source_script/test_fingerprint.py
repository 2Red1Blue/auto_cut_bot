"""Tests for _make_fingerprint, _deduplicate_scenes from source_script_save.

Uses clean-pytest methodology: AAA pattern with # Arrange / # Act / # Assert comments.
"""

from __future__ import annotations

import pytest

from auto_cut_bot.agent.tools.pipeline.source_script_save import (
    _deduplicate_scenes,
    _make_fingerprint,
)


class TestMakeFingerprint:
    """Tests for _make_fingerprint."""

    def test_same_inputs_same_hash(self) -> None:
        # Arrange
        scene = {
            "scene_id": "S1E1",
            "location": "墓地",
            "dialogues": [{"character": "Lucifer", "text": "Hello.", "sequence": 1}],
        }

        # Act
        fp1 = _make_fingerprint(1, scene)
        fp2 = _make_fingerprint(1, scene)

        # Assert
        assert fp1 == fp2
        assert isinstance(fp1, str)
        assert len(fp1) == 64  # SHA256 hex digest

    def test_different_episode_different_hash(self) -> None:
        # Arrange
        scene = {
            "scene_id": "S1E1",
            "location": "墓地",
            "dialogues": [{"character": "Lucifer", "text": "Hello.", "sequence": 1}],
        }

        # Act
        fp1 = _make_fingerprint(1, scene)
        fp2 = _make_fingerprint(2, scene)

        # Assert
        assert fp1 != fp2

    def test_different_scene_id_different_hash(self) -> None:
        # Arrange
        scene1 = {
            "scene_id": "S1E1", "location": "墓地",
            "dialogues": [{"character": "A", "text": "Hi.", "sequence": 1}],
        }
        scene2 = {
            "scene_id": "S1E2", "location": "墓地",
            "dialogues": [{"character": "A", "text": "Hi.", "sequence": 1}],
        }

        # Act
        fp1 = _make_fingerprint(1, scene1)
        fp2 = _make_fingerprint(1, scene2)

        # Assert
        assert fp1 != fp2

    def test_empty_dialogues(self) -> None:
        # Arrange
        scene = {"scene_id": "S1E1", "location": "墓地", "dialogues": []}

        # Act
        fp = _make_fingerprint(1, scene)

        # Assert
        assert isinstance(fp, str)
        assert len(fp) == 64

    def test_missing_dialogue_fields(self) -> None:
        # Arrange
        scene = {"scene_id": "S1E1", "location": "墓地"}

        # Act
        fp = _make_fingerprint(1, scene)

        # Assert
        assert isinstance(fp, str)
        assert len(fp) == 64


class TestDeduplicateScenes:
    """Tests for _deduplicate_scenes."""

    def test_no_duplicates(self) -> None:
        # Arrange
        episodes = [
            {"episode_number": 1, "scenes": [
                {"scene_id": "S1E1", "scene_order": 1, "location": "墓地",
                 "dialogues": [{"character": "A", "text": "Hi.", "sequence": 1}]},
            ]},
            {"episode_number": 2, "scenes": [
                {"scene_id": "S2E1", "scene_order": 1, "location": "宫殿",
                 "dialogues": [{"character": "B", "text": "Hello.", "sequence": 1}]},
            ]},
        ]

        # Act
        removed = _deduplicate_scenes(episodes)

        # Assert
        assert removed == 0
        assert len(episodes[0]["scenes"]) == 1
        assert len(episodes[1]["scenes"]) == 1

    def test_duplicate_scenes_removed(self) -> None:
        # Arrange
        scene = {
            "scene_id": "S1E1", "scene_order": 1, "location": "墓地",
            "dialogues": [{"character": "A", "text": "Hi.", "sequence": 1}],
        }
        episodes = [
            {"episode_number": 1, "scenes": [dict(scene), dict(scene)]},
        ]

        # Act
        removed = _deduplicate_scenes(episodes)

        # Assert
        assert removed == 1
        assert len(episodes[0]["scenes"]) == 1

    def test_empty_episodes(self) -> None:
        # Arrange
        episodes: list = []

        # Act
        removed = _deduplicate_scenes(episodes)

        # Assert
        assert removed == 0