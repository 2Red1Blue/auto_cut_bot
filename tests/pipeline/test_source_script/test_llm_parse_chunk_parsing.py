"""Tests for _parse_single_chunk — single chunk LLM parsing with retry, confidence, and reparse.

Uses clean-pytest methodology: AAA pattern, Fake-based testing, monkeypatch for
dependency injection, function-scoped isolation.
"""

from __future__ import annotations

from typing import Any

import pytest

from auto_cut_bot.pipeline.plugins.ac_source_prep.stages.source_script.llm_parse import (
    _ensure_confidence_scores,
    _parse_single_chunk,
)


# ── Helpers ──────────────────────────────────────────────────────────────────────────


def _make_fake_call_llm(
    responses: list[dict[str, Any]],
    *,
    fail_on_calls: set[int] | None = None,
    always_fail: bool = False,
) -> Any:
    """Create a fake _call_llm replacement with configurable responses.

    Args:
        responses: Ordered list of dicts to return on each call.
        fail_on_calls: Set of 0-based call indices on which to raise.
        always_fail: If True, raise on every call.

    Returns:
        A callable matching _call_llm(prompt, model, cfg) -> dict.
    """
    call_count = [0]

    def fake_call_llm(prompt: str, model: str, cfg: Any) -> dict[str, Any]:
        idx = call_count[0]
        call_count[0] += 1
        if always_fail:
            raise RuntimeError("LLM backend failed (fake)")
        if fail_on_calls and idx in fail_on_calls:
            raise RuntimeError("LLM backend failed (fake)")
        if idx < len(responses):
            return dict(responses[idx])
        return {"episodes": []}

    fake_call_llm.call_count = call_count  # type: ignore[attr-defined]
    return fake_call_llm


def _make_valid_result() -> dict[str, Any]:
    """Return a valid parsed result with one episode and one scene."""
    return {
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
                    }
                ],
            }
        ]
    }


def _make_invalid_result() -> dict[str, Any]:
    """Return an invalid parsed result (empty episodes)."""
    return {"episodes": []}


def _make_low_confidence_result() -> dict[str, Any]:
    """Return a valid result with one scene at confidence 0.50."""
    return {
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
                        "confidence": 0.50,
                    }
                ],
            }
        ]
    }


# ── Tests ────────────────────────────────────────────────────────────────────────────


class TestParseSingleChunk:
    """Tests for _parse_single_chunk — retry, confidence, reparse, and metadata."""

    # ── Success / Retry paths ────────────────────────────────────────────────────────

    def test_parse_single_chunk_success_first_attempt(
        self, monkeypatch: pytest.MonkeyPatch, fake_pipeline_config: Any,
    ) -> None:
        """_parse_single_chunk returns success on the first LLM call."""
        # Arrange
        fake_call = _make_fake_call_llm([_make_valid_result()])
        monkeypatch.setattr(
            "auto_cut_bot.pipeline.plugins.ac_source_prep.stages.source_script.llm_parse._call_llm",
            fake_call,
        )
        monkeypatch.setattr("time.sleep", lambda _: None)
        chunk_text = "第一集 墓地\n\n1-1 墓地 雨夜 外\n\nLucifer：Hello.\n"

        # Act
        result = _parse_single_chunk(chunk_text, fake_pipeline_config)

        # Assert
        assert fake_call.call_count[0] == 1
        assert len(result["episodes"]) == 1
        assert result["_parse_meta"]["attempts"] == 1
        assert result["_parse_meta"]["status"] == "success"

    def test_parse_single_chunk_success_after_one_retry(
        self, monkeypatch: pytest.MonkeyPatch, fake_pipeline_config: Any,
    ) -> None:
        """_parse_single_chunk succeeds after one validation failure."""
        # Arrange
        fake_call = _make_fake_call_llm([_make_invalid_result(), _make_valid_result()])
        monkeypatch.setattr(
            "auto_cut_bot.pipeline.plugins.ac_source_prep.stages.source_script.llm_parse._call_llm",
            fake_call,
        )
        monkeypatch.setattr("time.sleep", lambda _: None)
        chunk_text = "第一集 墓地\n\n1-1 墓地 雨夜 外\n\n"

        # Act
        result = _parse_single_chunk(chunk_text, fake_pipeline_config)

        # Assert
        assert fake_call.call_count[0] == 2
        assert len(result["episodes"]) == 1
        assert result["_parse_meta"]["attempts"] == 2
        assert result["_parse_meta"]["status"] == "success"

    def test_parse_single_chunk_success_after_two_retries(
        self, monkeypatch: pytest.MonkeyPatch, fake_pipeline_config: Any,
    ) -> None:
        """_parse_single_chunk succeeds after two validation failures."""
        # Arrange
        fake_call = _make_fake_call_llm([
            _make_invalid_result(),
            _make_invalid_result(),
            _make_valid_result(),
        ])
        monkeypatch.setattr(
            "auto_cut_bot.pipeline.plugins.ac_source_prep.stages.source_script.llm_parse._call_llm",
            fake_call,
        )
        monkeypatch.setattr("time.sleep", lambda _: None)
        chunk_text = "第一集 墓地\n\n1-1 墓地 雨夜 外\n\n"

        # Act
        result = _parse_single_chunk(chunk_text, fake_pipeline_config)

        # Assert
        assert fake_call.call_count[0] == 3
        assert len(result["episodes"]) == 1
        assert result["_parse_meta"]["attempts"] == 3
        assert result["_parse_meta"]["status"] == "success"

    def test_parse_single_chunk_all_retries_exhausted(
        self, monkeypatch: pytest.MonkeyPatch, fake_pipeline_config: Any,
    ) -> None:
        """_parse_single_chunk returns parse_error after all 4 attempts fail."""
        # Arrange
        # MAX_RETRIES = 3, so 4 attempts total (0, 1, 2, 3)
        fake_call = _make_fake_call_llm([
            _make_invalid_result(),
            _make_invalid_result(),
            _make_invalid_result(),
            _make_invalid_result(),
        ])
        monkeypatch.setattr(
            "auto_cut_bot.pipeline.plugins.ac_source_prep.stages.source_script.llm_parse._call_llm",
            fake_call,
        )
        monkeypatch.setattr("time.sleep", lambda _: None)
        chunk_text = "Some text\n"

        # Act
        result = _parse_single_chunk(chunk_text, fake_pipeline_config)

        # Assert
        assert fake_call.call_count[0] == 4
        assert result["episodes"] == []
        assert result["_parse_meta"]["status"] == "parse_error"
        assert result["_parse_meta"]["attempts"] == 4

    def test_parse_single_chunk_exception_all_attempts(
        self, monkeypatch: pytest.MonkeyPatch, fake_pipeline_config: Any,
    ) -> None:
        """_parse_single_chunk raises after all LLM calls throw exceptions."""
        # Arrange
        fake_call = _make_fake_call_llm([], always_fail=True)
        monkeypatch.setattr(
            "auto_cut_bot.pipeline.plugins.ac_source_prep.stages.source_script.llm_parse._call_llm",
            fake_call,
        )
        monkeypatch.setattr("time.sleep", lambda _: None)
        chunk_text = "Some text\n"

        # Act / Assert
        with pytest.raises(RuntimeError, match="LLM backend failed"):
            _parse_single_chunk(chunk_text, fake_pipeline_config)

    # ── Low confidence → reparse paths ───────────────────────────────────────────────

    def test_parse_single_chunk_low_confidence_triggers_reparse(
        self, monkeypatch: pytest.MonkeyPatch, fake_pipeline_config: Any,
    ) -> None:
        """Low-confidence scenes trigger _reparse_low_confidence_scenes."""
        # Arrange
        fake_call = _make_fake_call_llm([_make_low_confidence_result()])
        monkeypatch.setattr(
            "auto_cut_bot.pipeline.plugins.ac_source_prep.stages.source_script.llm_parse._call_llm",
            fake_call,
        )
        monkeypatch.setattr("time.sleep", lambda _: None)

        reparse_calls: list[tuple] = []

        def _fake_reparse(
            result: dict[str, Any],
            low_conf: list[dict[str, Any]],
            original_text: str,
            cfg: Any,
        ) -> dict[str, Any]:
            reparse_calls.append((result, low_conf, original_text, cfg))
            return result

        monkeypatch.setattr(
            "auto_cut_bot.pipeline.plugins.ac_source_prep.stages.source_script.llm_parse._reparse_low_confidence_scenes",
            _fake_reparse,
        )
        chunk_text = "第一集 墓地\n\n1-1 墓地 雨夜 外\n\nLucifer：Hello.\n"

        # Act
        result = _parse_single_chunk(chunk_text, fake_pipeline_config)

        # Assert
        assert len(reparse_calls) == 1, "Expected _reparse_low_confidence_scenes to be called once"
        assert result["_parse_meta"]["status"] == "success"

    def test_parse_single_chunk_low_confidence_reparse_success(
        self, monkeypatch: pytest.MonkeyPatch, fake_pipeline_config: Any,
    ) -> None:
        """Reparse success updates scene confidence above threshold."""
        # Arrange
        fake_call = _make_fake_call_llm([_make_low_confidence_result()])
        monkeypatch.setattr(
            "auto_cut_bot.pipeline.plugins.ac_source_prep.stages.source_script.llm_parse._call_llm",
            fake_call,
        )
        monkeypatch.setattr("time.sleep", lambda _: None)

        def _fake_reparse_success(
            result: dict[str, Any],
            low_conf: list[dict[str, Any]],
            original_text: str,
            cfg: Any,
        ) -> dict[str, Any]:
            scene = result["episodes"][0]["scenes"][0]
            scene["confidence"] = 0.95
            scene["_reparsed"] = True
            scene["_original_confidence"] = 0.50
            return result

        monkeypatch.setattr(
            "auto_cut_bot.pipeline.plugins.ac_source_prep.stages.source_script.llm_parse._reparse_low_confidence_scenes",
            _fake_reparse_success,
        )
        chunk_text = "第一集 墓地\n\n1-1 墓地 雨夜 外\n\nLucifer：Hello.\n"

        # Act
        result = _parse_single_chunk(chunk_text, fake_pipeline_config)

        # Assert
        scene = result["episodes"][0]["scenes"][0]
        assert scene["confidence"] == 0.95
        assert scene["_reparsed"] is True
        assert scene["_original_confidence"] == 0.50
        assert result["_parse_meta"]["status"] == "success"

    def test_parse_single_chunk_low_confidence_reparse_still_below(
        self, monkeypatch: pytest.MonkeyPatch, fake_pipeline_config: Any,
    ) -> None:
        """Reparse still below threshold marks review_required."""
        # Arrange
        fake_call = _make_fake_call_llm([_make_low_confidence_result()])
        monkeypatch.setattr(
            "auto_cut_bot.pipeline.plugins.ac_source_prep.stages.source_script.llm_parse._call_llm",
            fake_call,
        )
        monkeypatch.setattr("time.sleep", lambda _: None)

        def _fake_reparse_still_below(
            result: dict[str, Any],
            low_conf: list[dict[str, Any]],
            original_text: str,
            cfg: Any,
        ) -> dict[str, Any]:
            scene = result["episodes"][0]["scenes"][0]
            scene["review_required"] = True
            scene["review_reason"] = (
                "Two parse attempts both scored below threshold (0.50, 0.55). "
                "Manual review recommended."
            )
            scene["_reparsed"] = True
            scene["_reparse_confidence"] = 0.55
            return result

        monkeypatch.setattr(
            "auto_cut_bot.pipeline.plugins.ac_source_prep.stages.source_script.llm_parse._reparse_low_confidence_scenes",
            _fake_reparse_still_below,
        )
        chunk_text = "第一集 墓地\n\n1-1 墓地 雨夜 外\n\nLucifer：Hello.\n"

        # Act
        result = _parse_single_chunk(chunk_text, fake_pipeline_config)

        # Assert
        scene = result["episodes"][0]["scenes"][0]
        assert scene["review_required"] is True
        assert "Two parse attempts" in scene["review_reason"]
        assert result["_parse_meta"]["status"] == "success"

    def test_parse_single_chunk_low_confidence_reparse_exception(
        self, monkeypatch: pytest.MonkeyPatch, fake_pipeline_config: Any,
    ) -> None:
        """Reparse exception marks the scene review_required with error info."""
        # Arrange
        fake_call = _make_fake_call_llm([_make_low_confidence_result()])
        monkeypatch.setattr(
            "auto_cut_bot.pipeline.plugins.ac_source_prep.stages.source_script.llm_parse._call_llm",
            fake_call,
        )
        monkeypatch.setattr("time.sleep", lambda _: None)

        def _fake_reparse_exception(
            result: dict[str, Any],
            low_conf: list[dict[str, Any]],
            original_text: str,
            cfg: Any,
        ) -> dict[str, Any]:
            scene = result["episodes"][0]["scenes"][0]
            scene["review_required"] = True
            scene["review_reason"] = (
                "Re-parse failed with error: Simulated error. "
                "Original confidence: 0.50. Manual review recommended."
            )
            scene["_reparse_error"] = "Simulated error"
            return result

        monkeypatch.setattr(
            "auto_cut_bot.pipeline.plugins.ac_source_prep.stages.source_script.llm_parse._reparse_low_confidence_scenes",
            _fake_reparse_exception,
        )
        chunk_text = "第一集 墓地\n\n1-1 墓地 雨夜 外\n\nLucifer：Hello.\n"

        # Act
        result = _parse_single_chunk(chunk_text, fake_pipeline_config)

        # Assert
        scene = result["episodes"][0]["scenes"][0]
        assert scene["review_required"] is True
        assert "Re-parse failed" in scene["review_reason"]
        assert scene["_reparse_error"] == "Simulated error"
        assert result["_parse_meta"]["status"] == "success"

    # ── Format detection / confidence scores / metadata ──────────────────────────────

    def test_parse_single_chunk_format_detection_called(
        self, monkeypatch: pytest.MonkeyPatch, fake_pipeline_config: Any,
    ) -> None:
        """_detect_format is called with the chunk text on each attempt."""
        # Arrange
        fake_call = _make_fake_call_llm([_make_valid_result()])
        monkeypatch.setattr(
            "auto_cut_bot.pipeline.plugins.ac_source_prep.stages.source_script.llm_parse._call_llm",
            fake_call,
        )
        monkeypatch.setattr("time.sleep", lambda _: None)

        detect_calls: list[tuple] = []

        def _fake_detect_format(text: str, sample_size: int = 5000) -> str:
            detect_calls.append((text, sample_size))
            return "chinese_numbered"

        monkeypatch.setattr(
            "auto_cut_bot.pipeline.plugins.ac_source_prep.stages.source_script.llm_parse._detect_format",
            _fake_detect_format,
        )
        chunk_text = "第一集 墓地\n\n1-1 墓地 雨夜 外\n\nLucifer：Hello.\n"

        # Act
        _parse_single_chunk(chunk_text, fake_pipeline_config)

        # Assert
        assert len(detect_calls) == 1, "Expected _detect_format to be called once"
        assert detect_calls[0][0] == chunk_text, "Expected _detect_format to receive chunk_text"

    def test_parse_single_chunk_confidence_scores_ensured(
        self, monkeypatch: pytest.MonkeyPatch, fake_pipeline_config: Any,
    ) -> None:
        """_ensure_confidence_scores is called on the LLM result."""
        # Arrange
        result_without_confidence: dict[str, Any] = {
            "episodes": [
                {
                    "episode_number": 1,
                    "scenes": [
                        {
                            "scene_id": "S1E1",
                            "scene_order": 1,
                            "heading": "1-1 墓地",
                            "location": "墓地",
                            "time_of_day": "雨夜",
                            "is_flashback": False,
                            "characters_present": [],
                            "dialogues": [],
                            "raw_description": "",
                            "meta_tags": {},
                            "confidence": 0.95,
                        }
                    ],
                }
            ]
        }
        fake_call = _make_fake_call_llm([result_without_confidence])
        monkeypatch.setattr(
            "auto_cut_bot.pipeline.plugins.ac_source_prep.stages.source_script.llm_parse._call_llm",
            fake_call,
        )
        monkeypatch.setattr("time.sleep", lambda _: None)

        ensure_calls: list[dict[str, Any]] = []

        def _fake_ensure(
            result: dict[str, Any],
        ) -> dict[str, Any]:
            ensure_calls.append(result)
            return _ensure_confidence_scores(result)

        monkeypatch.setattr(
            "auto_cut_bot.pipeline.plugins.ac_source_prep.stages.source_script.llm_parse._ensure_confidence_scores",
            _fake_ensure,
        )
        chunk_text = "第一集 墓地\n\n1-1 墓地 雨夜 外\n\n"

        # Act
        result = _parse_single_chunk(chunk_text, fake_pipeline_config)

        # Assert
        assert len(ensure_calls) == 1, "Expected _ensure_confidence_scores to be called once"
        assert result["episodes"][0]["scenes"][0]["confidence"] == 0.95

    def test_parse_single_chunk_parse_meta_populated(
        self, monkeypatch: pytest.MonkeyPatch, fake_pipeline_config: Any,
    ) -> None:
        """Success result includes _parse_meta with attempts and status."""
        # Arrange
        fake_call = _make_fake_call_llm([_make_valid_result()])
        monkeypatch.setattr(
            "auto_cut_bot.pipeline.plugins.ac_source_prep.stages.source_script.llm_parse._call_llm",
            fake_call,
        )
        monkeypatch.setattr("time.sleep", lambda _: None)
        chunk_text = "第一集 墓地\n\n1-1 墓地 雨夜 外\n\nLucifer：Hello.\n"

        # Act
        result = _parse_single_chunk(chunk_text, fake_pipeline_config)

        # Assert
        assert "_parse_meta" in result
        assert "attempts" in result["_parse_meta"]
        assert "status" in result["_parse_meta"]
        assert result["_parse_meta"]["status"] == "success"
        assert isinstance(result["_parse_meta"]["attempts"], int)
        assert result["_parse_meta"]["attempts"] >= 1
