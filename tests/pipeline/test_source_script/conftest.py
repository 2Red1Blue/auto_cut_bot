"""Test fixtures and Fakes for source_script pipeline tests.

Uses clean-pytest methodology: Fake-based testing, AAA pattern,
dependency injection between fixtures, function-scoped isolation.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest


# ── Fake Classes ──────────────────────────────────────────────────────────────────


class FakePipelineConfig:
    """Fake PipelineConfig for testing without real config files."""

    def __init__(self, job_root: Path) -> None:
        self.job_root = job_root.resolve()
        self.backend = "openai"
        self.extra: dict[str, Any] = {
            "script_model": "test-model",
            "expected_episode_count": None,
            "confidence_threshold": 0.7,
        }
        self.db_url = "sqlite:///:memory:"
        self.db_schema = "test_schema"


class FakeArtifactCache:
    """In-memory dict-based cache with truncation detection."""

    def __init__(self) -> None:
        self._store: dict[str, dict[str, Any]] = {}
        self.put_calls: list[tuple[str, dict[str, Any]]] = []
        self.get_calls: list[str] = []

    def get(self, sha: str) -> dict[str, Any] | None:
        self.get_calls.append(sha)
        return self._store.get(sha)

    def put(self, sha: str, data: dict[str, Any]) -> None:
        self.put_calls.append((sha, data))
        self._store[sha] = data

    def is_valid(
        self,
        job_root: str,
        script_sha: str,
        expected_count: int | None = None,
        force_reparse: bool = False,
    ) -> bool:
        if force_reparse:
            return False
        cached = self._store.get(script_sha)
        if cached is None:
            return False
        parse_meta = cached.get("_parse_meta", cached.get("parse_metadata", {}))
        if parse_meta.get("status") in ("parse_error", "failed"):
            return False
        if expected_count is not None:
            cached_episodes = cached.get("episodes", [])
            if len(cached_episodes) < expected_count * 0.5:
                return False
        return True

    def invalidate(self, job_root: str, script_sha: str) -> bool:
        if script_sha in self._store:
            del self._store[script_sha]
            return True
        return False


class FakeLLMBackend:
    """Configurable LLM backend that returns pre-defined responses."""

    def __init__(self) -> None:
        self.responses: list[dict[str, Any]] = []
        self.call_count: int = 0
        self.prompts_sent: list[str] = []
        self.fail_on_call: set[int] = set()
        self.always_fail: bool = False
        self.fail_exception: Exception = RuntimeError("LLM backend failed (fake)")

    def set_responses(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)

    def set_fail_on_calls(self, call_indices: set[int]) -> None:
        self.fail_on_call = set(call_indices)

    def __call__(self, prompt: str, model: str, cfg: Any) -> dict[str, Any]:
        self.call_count += 1
        self.prompts_sent.append(prompt)
        idx = self.call_count - 1

        if self.always_fail:
            raise self.fail_exception
        if idx in self.fail_on_call:
            raise self.fail_exception
        if idx < len(self.responses):
            return dict(self.responses[idx])
        return {"episodes": []}


class FakeDBClient:
    """Fake StageDBClient for testing database operations."""

    def __init__(self) -> None:
        self.is_available: bool = True
        self.subtitles: list[dict[str, Any]] = []
        self.written_scenes: list[dict[str, Any]] = []
        self.written_derived: list[dict[str, Any]] = []
        self.written_subjects: list[dict[str, Any]] = []
        self.written_shots: list[dict[str, Any]] = []
        self.fail_on_write: bool = False

    def query_subtitles(self, book_id: str) -> list[dict[str, Any]]:
        return list(self.subtitles)

    def write_scenes(self, episodes: list[dict[str, Any]], book_id: str) -> int:
        if self.fail_on_write:
            raise RuntimeError("DB write failed (fake)")
        self.written_scenes.extend(episodes)
        return len(episodes)

    def write_derived(
        self, episodes: list[dict[str, Any]], book_id: str
    ) -> None:
        if self.fail_on_write:
            raise RuntimeError("DB write failed (fake)")
        self.written_derived.append({"episodes": episodes, "book_id": book_id})


# ── Fixtures ──────────────────────────────────────────────────────────────────────


@pytest.fixture()
def tmp_job_root(tmp_path: Path) -> Path:
    """Temporary job root directory."""
    (tmp_path / ".sd-cache" / "source_script").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture()
def fake_pipeline_config(tmp_job_root: Path) -> FakePipelineConfig:
    """Fresh FakePipelineConfig for each test."""
    return FakePipelineConfig(tmp_job_root)


@pytest.fixture()
def fake_artifact_cache() -> FakeArtifactCache:
    """Fresh FakeArtifactCache for each test."""
    return FakeArtifactCache()


@pytest.fixture()
def fake_llm_backend() -> FakeLLMBackend:
    """Fresh FakeLLMBackend for each test."""
    return FakeLLMBackend()


@pytest.fixture()
def fake_db_client() -> FakeDBClient:
    """Fresh FakeDBClient for each test."""
    return FakeDBClient()


@pytest.fixture()
def fake_script_file(tmp_job_root: Path) -> Path:
    """Create a sample script file in the job root."""
    path = tmp_job_root / "script.txt"
    path.write_text(
        "第一集 墓地\n\n1-1 墓地 雨夜 外\n\n"
        "Lucifer：Humans call me Satan.\n\n"
        "第二集 宫殿\n\n2-1 宫殿 日内\n\n"
        "Emperor：Who dares enter?\n\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture()
def sample_chinese_script() -> str:
    """500-char Chinese drama script with episode markers."""
    return (
        "第一集 命运的开始\n\n"
        "1-1 学校操场 日 外\n\n"
        "小明：今天天气真好。\n小红：是啊，我们去跑步吧。\n\n"
        "1-2 教室 日内\n\n"
        "老师：同学们，今天我们来学习新的课程。\n\n"
        "第二集 成长的烦恼\n\n"
        "2-1 家中 夜 内\n\n"
        "妈妈：作业做完了吗？\n小明：还没呢，马上做。\n\n"
    )


@pytest.fixture()
def sample_english_script() -> str:
    """300-char English script with Scene markers."""
    return (
        "Scene 1: The Meeting\n\n"
        "John walks into the office. The room is dimly lit.\n\n"
        "John: Good morning, everyone.\n"
        "Sarah: You're late again.\n\n"
        "Scene 2: The Confrontation\n\n"
        "The hallway echoes with footsteps.\n\n"
        "John: I need to explain myself.\n"
    )


@pytest.fixture()
def sample_screenplay_script() -> str:
    """300-char screenplay format with INT/EXT markers."""
    return (
        "INT. COFFEE SHOP - DAY\n\n"
        "The barista cleans the counter. A bell rings.\n\n"
        "BARISTA\n"
        "What can I get for you?\n\n"
        "CUSTOMER\n"
        "Just a black coffee.\n\n"
        "EXT. STREET - NIGHT\n\n"
        "Rain falls on the pavement.\n"
    )


@pytest.fixture()
def sample_unknown_format_script() -> str:
    """200-char text with no recognizable format."""
    return (
        "Once upon a time, in a land far away, there lived a wise old man. "
        "He knew the secrets of the universe and shared them with anyone who asked. "
        "His words were simple but profound."
    )


@pytest.fixture()
def sample_script_chunks() -> list[str]:
    """Three sample script chunks for MapReduce testing."""
    return [
        "第一集 开始\n1-1 地点 日 外\n角色A：你好。\n",
        "第二集 继续\n2-1 场地 夜 内\n角色B：大家好。\n",
        "第三集 结束\n3-1 终点 晨 外\n角色C：再见。\n",
    ]


@pytest.fixture()
def sample_parsed_result() -> dict[str, Any]:
    """Valid parsed result with 2 episodes, 3 scenes each, confidence 0.9."""
    return {
        "episodes": [
            {
                "episode_number": 1,
                "title": "First Episode",
                "scenes": [
                    {
                        "scene_id": "S1E1", "scene_order": 1,
                        "heading": "1-1 墓地 雨夜 外", "location": "墓地",
                        "time_of_day": "雨夜", "is_flashback": False,
                        "characters_present": ["Lucifer"],
                        "dialogues": [{"character": "Lucifer", "text": "Hello.", "sequence": 1}],
                        "raw_description": "Lucifer walks.", "meta_tags": {}, "confidence": 0.95,
                    },
                    {
                        "scene_id": "S1E2", "scene_order": 2,
                        "heading": "1-2 宫殿 日内", "location": "宫殿",
                        "time_of_day": "日内", "is_flashback": False,
                        "characters_present": ["Emperor"],
                        "dialogues": [{"character": "Emperor", "text": "Enter.", "sequence": 1}],
                        "raw_description": "The throne room.", "meta_tags": {}, "confidence": 0.92,
                    },
                    {
                        "scene_id": "S1E3", "scene_order": 3,
                        "heading": "1-3 花园 夕 外", "location": "花园",
                        "time_of_day": "夕", "is_flashback": True,
                        "characters_present": ["Lucifer", "Emperor"],
                        "dialogues": [],
                        "raw_description": "A quiet garden.",
                        "meta_tags": {"mood": "melancholy"}, "confidence": 0.88,
                    },
                ],
            },
            {
                "episode_number": 2,
                "title": "Second Episode",
                "scenes": [
                    {
                        "scene_id": "S2E1", "scene_order": 1,
                        "heading": "2-1 战场 日 外", "location": "战场",
                        "time_of_day": "日", "is_flashback": False,
                        "characters_present": ["Soldier"],
                        "dialogues": [{"character": "Soldier", "text": "Charge!", "sequence": 1}],
                        "raw_description": "The battle begins.", "meta_tags": {}, "confidence": 0.91,
                    },
                    {
                        "scene_id": "S2E2", "scene_order": 2,
                        "heading": "2-2 帐篷 夜 内", "location": "帐篷",
                        "time_of_day": "夜", "is_flashback": False,
                        "characters_present": ["General"],
                        "dialogues": [{"character": "General", "text": "Retreat.", "sequence": 1}],
                        "raw_description": "Strategy meeting.", "meta_tags": {}, "confidence": 0.90,
                    },
                    {
                        "scene_id": "S2E3", "scene_order": 3,
                        "heading": "2-3 河边 晨 外", "location": "河边",
                        "time_of_day": "晨", "is_flashback": False,
                        "characters_present": ["Soldier", "General"],
                        "dialogues": [],
                        "raw_description": "Aftermath.", "meta_tags": {}, "confidence": 0.89,
                    },
                ],
            },
        ]
    }


@pytest.fixture()
def sample_low_confidence_result() -> dict[str, Any]:
    """Parsed result with one scene at confidence 0.5."""
    return {
        "episodes": [{
            "episode_number": 1,
            "scenes": [
                {
                    "scene_id": "S1E1", "scene_order": 1,
                    "heading": "1-1 墓地 雨夜 外", "location": "墓地",
                    "time_of_day": "雨夜", "is_flashback": False,
                    "characters_present": ["Lucifer"],
                    "dialogues": [{"character": "Lucifer", "text": "Hello.", "sequence": 1}],
                    "raw_description": "Lucifer walks.", "meta_tags": {}, "confidence": 0.50,
                },
                {
                    "scene_id": "S1E2", "scene_order": 2,
                    "heading": "1-2 宫殿 日内", "location": "宫殿",
                    "time_of_day": "日内", "is_flashback": False,
                    "characters_present": ["Emperor"],
                    "dialogues": [],
                    "raw_description": "Throne room.", "meta_tags": {}, "confidence": 0.95,
                },
            ],
        }]
    }


@pytest.fixture()
def sample_chunk_plan() -> list[dict[str, Any]]:
    """Sample chunk plan for MapReduce parsing."""
    return [
        {"chunk_id": 1, "episode_range": [1, 15], "char_start": 0, "char_end": 28300, "char_count": 28300},
        {"chunk_id": 2, "episode_range": [16, 30], "char_start": 27800, "char_end": 56200, "char_count": 28400},
        {"chunk_id": 3, "episode_range": [31, 45], "char_start": 55700, "char_end": 85234, "char_count": 29534},
    ]


@pytest.fixture()
def sample_episodes_for_save() -> list[dict[str, Any]]:
    """Sample episodes for source_script_save tests."""
    return [
        {"episode_number": 1, "scenes": [{"scene_id": "S1E1", "scene_order": 1, "heading": "1-1 墓地 雨夜 外", "location": "墓地", "time_of_day": "雨夜", "is_flashback": False, "characters_present": ["Lucifer"], "dialogues": [{"character": "Lucifer", "text": "Hello.", "sequence": 1}], "raw_description": "Lucifer walks.", "meta_tags": {}, "confidence": 0.95}]},
        {"episode_number": 2, "scenes": [{"scene_id": "S2E1", "scene_order": 1, "heading": "2-1 宫殿 日内", "location": "宫殿", "time_of_day": "日内", "is_flashback": False, "characters_present": ["Emperor"], "dialogues": [{"character": "Emperor", "text": "Enter.", "sequence": 1}], "raw_description": "Throne room.", "meta_tags": {}, "confidence": 0.92}]},
    ]


@pytest.fixture()
def fake_llm_response_success() -> FakeLLMBackend:
    """FakeLLMBackend pre-configured for success."""
    backend = FakeLLMBackend()
    backend.set_responses([{"episodes": [{"episode_number": 1, "scenes": [{"scene_id": "S1E1", "scene_order": 1, "heading": "1-1 墓地 雨夜 外", "location": "墓地", "time_of_day": "雨夜", "is_flashback": False, "characters_present": ["Lucifer"], "dialogues": [{"character": "Lucifer", "text": "Hello.", "sequence": 1}], "raw_description": "Lucifer walks.", "meta_tags": {}, "confidence": 0.95}]}]}])
    return backend


@pytest.fixture()
def fake_llm_response_retry() -> FakeLLMBackend:
    """FakeLLMBackend that fails first 2 calls then succeeds."""
    backend = FakeLLMBackend()
    backend.set_fail_on_calls({0, 1})
    backend.set_responses([{}, {}, {"episodes": [{"episode_number": 1, "scenes": [{"scene_id": "S1E1", "scene_order": 1, "heading": "1-1 墓地 雨夜 外", "location": "墓地", "time_of_day": "雨夜", "is_flashback": False, "characters_present": ["Lucifer"], "dialogues": [], "raw_description": "Lucifer walks.", "meta_tags": {}, "confidence": 0.95}]}]}])
    return backend


@pytest.fixture()
def fake_llm_response_error() -> FakeLLMBackend:
    """FakeLLMBackend that always fails."""
    backend = FakeLLMBackend()
    backend.always_fail = True
    return backend


@pytest.fixture()
def sample_episodes_with_duplicates() -> list[dict[str, Any]]:
    """3 episodes, episode 2 has a duplicate scene."""
    dup = {"scene_id": "S2E1", "scene_order": 1, "location": "宫殿", "dialogues": [{"character": "Emperor", "text": "Enter.", "sequence": 1}]}
    return [
        {"episode_number": 1, "scenes": [{"scene_id": "S1E1", "scene_order": 1, "location": "墓地", "dialogues": [{"character": "A", "text": "Hi.", "sequence": 1}]}]},
        {"episode_number": 2, "scenes": [dict(dup), dict(dup)]},
        {"episode_number": 3, "scenes": [{"scene_id": "S3E1", "scene_order": 1, "location": "河边", "dialogues": [{"character": "C", "text": "Bye.", "sequence": 1}]}]},
    ]


@pytest.fixture()
def sample_episodes_with_validation_errors() -> dict[str, Any]:
    """Parsed result with validation errors: first episode is 3, missing 1-2."""
    return {"episodes": [{"episode_number": 3, "scenes": [{"scene_id": "S3E1", "scene_order": 1, "heading": "3-1 墓地 雨夜 外", "location": "墓地", "time_of_day": "雨夜", "is_flashback": False, "characters_present": ["Lucifer"], "dialogues": [], "raw_description": "Lucifer walks.", "meta_tags": {}, "confidence": 0.95}]}]}
