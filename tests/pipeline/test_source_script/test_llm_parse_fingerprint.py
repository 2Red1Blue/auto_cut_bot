"""Tests for _make_fingerprint and _merge_chunk_results."""

import pytest
from auto_cut_bot.pipeline.plugins.ac_source_prep.stages.source_script.llm_parse import (
    _make_fingerprint,
    _merge_chunk_results,
)


class TestMakeFingerprint:
    def test_same_inputs_same_hash(self):
        """同一输入产生相同 hash。"""
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

    def test_different_episode_different_hash(self):
        """不同 episode 产生不同 hash。"""
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

    def test_different_scene_id_different_hash(self):
        """不同 scene_id 产生不同 hash。"""
        # Arrange
        scene1 = {
            "scene_id": "S1E1",
            "location": "墓地",
            "dialogues": [{"character": "A", "text": "Hi.", "sequence": 1}],
        }
        scene2 = {
            "scene_id": "S1E2",
            "location": "墓地",
            "dialogues": [{"character": "A", "text": "Hi.", "sequence": 1}],
        }

        # Act
        fp1 = _make_fingerprint(1, scene1)
        fp2 = _make_fingerprint(1, scene2)

        # Assert
        assert fp1 != fp2

    def test_empty_dialogues(self):
        """空 dialogues 列表仍然能生成有效的 hash。"""
        # Arrange
        scene = {
            "scene_id": "S1E1",
            "location": "墓地",
            "dialogues": [],
        }

        # Act
        fp = _make_fingerprint(1, scene)

        # Assert
        assert isinstance(fp, str)
        assert len(fp) == 64

    def test_missing_dialogue_fields(self):
        """scene 缺少 dialogues 字段时仍能正常返回 hash。"""
        # Arrange
        scene = {"scene_id": "S1E1", "location": "墓地"}

        # Act
        fp = _make_fingerprint(1, scene)

        # Assert
        assert isinstance(fp, str)
        assert len(fp) == 64


class TestMergeChunkResults:
    def test_no_overlap(self):
        """两个 chunk 没有重叠，所有场景都被保留。"""
        # Arrange
        results = [
            {
                "episodes": [
                    {
                        "episode_number": 1,
                        "scenes": [
                            {
                                "scene_id": "S1E1",
                                "scene_order": 1,
                                "heading": "1-1 墓地 雨夜 外",
                                "location": "墓地",
                                "time_of_day": "雨夜",
                                "is_flashback": False,
                                "characters_present": ["Lucifer"],
                                "dialogues": [
                                    {"character": "Lucifer", "text": "Hello.", "sequence": 1}
                                ],
                                "raw_description": "Lucifer walks.",
                                "meta_tags": {},
                                "confidence": 0.95,
                            },
                        ],
                    },
                ],
            },
            {
                "episodes": [
                    {
                        "episode_number": 2,
                        "scenes": [
                            {
                                "scene_id": "S2E1",
                                "scene_order": 1,
                                "heading": "2-1 宫殿 日内",
                                "location": "宫殿",
                                "time_of_day": "日内",
                                "is_flashback": False,
                                "characters_present": ["Emperor"],
                                "dialogues": [
                                    {"character": "Emperor", "text": "Enter.", "sequence": 1}
                                ],
                                "raw_description": "Throne room.",
                                "meta_tags": {},
                                "confidence": 0.92,
                            },
                        ],
                    },
                ],
            },
        ]

        # Act
        merged = _merge_chunk_results(results)

        # Assert
        assert len(merged["episodes"]) == 2
        ep1 = merged["episodes"][0]
        ep2 = merged["episodes"][1]
        assert ep1["episode_number"] == 1
        assert len(ep1["scenes"]) == 1
        assert ep1["scenes"][0]["scene_id"] == "S1E1"
        assert ep2["episode_number"] == 2
        assert len(ep2["scenes"]) == 1
        assert ep2["scenes"][0]["scene_id"] == "S2E1"

    def test_with_overlap_dedup(self):
        """两个 chunk 有重叠场景时，重复的场景被去重。"""
        # Arrange
        scene_s1e1 = {
            "scene_id": "S1E1",
            "scene_order": 1,
            "location": "墓地",
            "dialogues": [{"character": "A", "text": "Hello.", "sequence": 1}],
        }
        scene_s1e2 = {
            "scene_id": "S1E2",
            "scene_order": 2,
            "location": "宫殿",
            "dialogues": [{"character": "B", "text": "Hi.", "sequence": 1}],
        }
        results = [
            {"episodes": [{"episode_number": 1, "scenes": [dict(scene_s1e1)]}]},
            {"episodes": [{"episode_number": 1, "scenes": [dict(scene_s1e1), dict(scene_s1e2)]}]},
        ]

        # Act
        merged = _merge_chunk_results(results)

        # Assert
        assert len(merged["episodes"]) == 1
        assert merged["episodes"][0]["episode_number"] == 1
        assert len(merged["episodes"][0]["scenes"]) == 2
        # 场景按 scene_id 排序
        assert merged["episodes"][0]["scenes"][0]["scene_id"] == "S1E1"
        assert merged["episodes"][0]["scenes"][1]["scene_id"] == "S1E2"

    def test_no_episodes(self):
        """所有 chunk 结果都没有 episodes 时返回空列表。"""
        # Arrange
        results = [{"episodes": []}, {"episodes": []}]

        # Act
        merged = _merge_chunk_results(results)

        # Assert
        assert merged["episodes"] == []
