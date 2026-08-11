"""Tests for _reparse_low_confidence_scenes — adaptive re-parse of low-confidence scenes."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from auto_cut_bot.pipeline.plugins.ac_source_prep.stages.source_script.llm_parse import (
    _find_low_confidence_scenes,
    _reparse_low_confidence_scenes,
)


# ── Helpers ────────────────────────────────────────────────────────────────────────


def _make_reparse_scene(
    scene_id: str = "S1E1",
    scene_order: int = 1,
    heading: str = "1-1 墓地 雨夜 外",
    location: str = "墓地",
    confidence: float = 0.95,
    **kwargs: Any,
) -> dict[str, Any]:
    """Build a dict that simulates what parse_model_json returns for a single scene."""
    base: dict[str, Any] = {
        "scene_id": scene_id,
        "scene_order": scene_order,
        "heading": heading,
        "location": location,
        "time_of_day": "雨夜",
        "is_flashback": False,
        "characters_present": ["Lucifer"],
        "dialogues": [{"character": "Lucifer", "text": "Hello.", "sequence": 1}],
        "raw_description": "Lucifer walks through the graveyard.",
        "meta_tags": {},
        "confidence": confidence,
    }
    base.update(kwargs)
    return base


def _make_provider_response(scene: dict[str, Any]) -> dict[str, Any]:
    """Wrap a scene dict in a provider response envelope."""
    return {"choices": [{"message": {"content": json.dumps(scene, ensure_ascii=False)}}]}


def _patch_llm_chain(monkeypatch, call_provider_fn):
    """Patch the three LLM imports used inside _reparse_low_confidence_scenes.

    The function imports these locally at call time, so we patch the source modules:
    - autocut_core.semantic.engine.provider.call_provider
    - autocut_core.semantic.engine.concurrency.RateLimiter
    - autocut_core.backends._base.get_backend
    """
    monkeypatch.setattr(
        "autocut_core.semantic.engine.provider.call_provider",
        call_provider_fn,
    )
    monkeypatch.setattr(
        "autocut_core.semantic.engine.concurrency.RateLimiter",
        lambda *a, **kw: MagicMock(),
    )
    monkeypatch.setattr(
        "autocut_core.backends._base.get_backend",
        lambda name, config=None: MagicMock(),
    )


# ── Original-text fixture reused across tests ──────────────────────────────────────

ORIGINAL_TEXT = (
    "1-1 墓地 雨夜 外\n\n"
    "Lucifer：Humans call me Satan.\n\n"
    "1-2 宫殿 日内\n\n"
    "Emperor：Who dares enter?\n"
)


# ── Tests ──────────────────────────────────────────────────────────────────────────


class TestReparseLowConfidenceScenes:
    """Tests for _reparse_low_confidence_scenes."""

    # ── 1. test_reparse_success_replaces_scene ──────────────────────────────────

    def test_reparse_success_replaces_scene(
        self, monkeypatch, fake_pipeline_config, sample_low_confidence_result
    ):
        """When reparse returns confidence >= threshold, the original scene is replaced."""
        new_scene = _make_reparse_scene(confidence=0.95)
        _patch_llm_chain(
            monkeypatch,
            lambda backend, payload, **kwargs: _make_provider_response(new_scene),
        )

        low_conf = _find_low_confidence_scenes(
            sample_low_confidence_result, threshold=0.7
        )
        assert len(low_conf) == 1, "Sanity: one low-confidence scene expected"

        # Act
        result = _reparse_low_confidence_scenes(
            sample_low_confidence_result, low_conf, ORIGINAL_TEXT, fake_pipeline_config
        )

        # Assert
        scene = result["episodes"][0]["scenes"][0]
        assert scene["confidence"] == 0.95
        assert scene["_reparsed"] is True
        assert scene["_original_confidence"] == 0.50
        assert "review_required" not in scene

    # ── 2. test_reparse_failed_marks_review_required ────────────────────────────

    def test_reparse_failed_marks_review_required(
        self, monkeypatch, fake_pipeline_config, sample_low_confidence_result
    ):
        """When reparse returns confidence < threshold, review_required is set."""
        new_scene = _make_reparse_scene(confidence=0.40)
        _patch_llm_chain(
            monkeypatch,
            lambda backend, payload, **kwargs: _make_provider_response(new_scene),
        )

        low_conf = _find_low_confidence_scenes(
            sample_low_confidence_result, threshold=0.7
        )

        # Act
        result = _reparse_low_confidence_scenes(
            sample_low_confidence_result, low_conf, ORIGINAL_TEXT, fake_pipeline_config
        )

        # Assert
        scene = result["episodes"][0]["scenes"][0]
        assert scene["review_required"] is True
        assert "review_reason" in scene
        assert "Two parse attempts" in scene["review_reason"]
        assert scene["_reparsed"] is True
        assert scene["_reparse_confidence"] == 0.40

    # ── 3. test_reparse_exception_marks_review_required ─────────────────────────

    def test_reparse_exception_marks_review_required(
        self, monkeypatch, fake_pipeline_config, sample_low_confidence_result
    ):
        """When reparse raises an exception, review_required is set."""
        _patch_llm_chain(
            monkeypatch,
            lambda backend, payload, **kwargs: (
                _ for _ in ()
            ).throw(RuntimeError("LLM timeout")),
        )

        low_conf = _find_low_confidence_scenes(
            sample_low_confidence_result, threshold=0.7
        )

        # Act
        result = _reparse_low_confidence_scenes(
            sample_low_confidence_result, low_conf, ORIGINAL_TEXT, fake_pipeline_config
        )

        # Assert
        scene = result["episodes"][0]["scenes"][0]
        assert scene["review_required"] is True
        assert "Re-parse failed" in scene["review_reason"]
        assert "_reparse_error" in scene

    # ── 4. test_reparse_confidence_above_threshold ──────────────────────────────

    def test_reparse_confidence_above_threshold(
        self, monkeypatch, fake_pipeline_config, sample_low_confidence_result
    ):
        """Confidence above threshold (0.8 > 0.7) triggers replacement."""
        new_scene = _make_reparse_scene(confidence=0.80)
        _patch_llm_chain(
            monkeypatch,
            lambda backend, payload, **kwargs: _make_provider_response(new_scene),
        )

        low_conf = _find_low_confidence_scenes(
            sample_low_confidence_result, threshold=0.7
        )

        # Act
        result = _reparse_low_confidence_scenes(
            sample_low_confidence_result, low_conf, ORIGINAL_TEXT, fake_pipeline_config
        )

        # Assert
        scene = result["episodes"][0]["scenes"][0]
        assert scene["confidence"] == 0.80
        assert scene["_reparsed"] is True
        assert "review_required" not in scene

    # ── 5. test_reparse_confidence_exactly_at_threshold ─────────────────────────

    def test_reparse_confidence_exactly_at_threshold(
        self, monkeypatch, fake_pipeline_config, sample_low_confidence_result
    ):
        """Confidence exactly at threshold (0.7 == 0.7) triggers replacement."""
        new_scene = _make_reparse_scene(confidence=0.70)
        _patch_llm_chain(
            monkeypatch,
            lambda backend, payload, **kwargs: _make_provider_response(new_scene),
        )

        low_conf = _find_low_confidence_scenes(
            sample_low_confidence_result, threshold=0.7
        )

        # Act
        result = _reparse_low_confidence_scenes(
            sample_low_confidence_result, low_conf, ORIGINAL_TEXT, fake_pipeline_config
        )

        # Assert
        scene = result["episodes"][0]["scenes"][0]
        assert scene["confidence"] == 0.70
        assert scene["_reparsed"] is True
        assert "review_required" not in scene

    # ── 6. test_reparse_confidence_below_threshold ──────────────────────────────

    def test_reparse_confidence_below_threshold(
        self, monkeypatch, fake_pipeline_config, sample_low_confidence_result
    ):
        """Confidence below threshold (0.30 < 0.70) marks review_required."""
        new_scene = _make_reparse_scene(confidence=0.30)
        _patch_llm_chain(
            monkeypatch,
            lambda backend, payload, **kwargs: _make_provider_response(new_scene),
        )

        low_conf = _find_low_confidence_scenes(
            sample_low_confidence_result, threshold=0.7
        )

        # Act
        result = _reparse_low_confidence_scenes(
            sample_low_confidence_result, low_conf, ORIGINAL_TEXT, fake_pipeline_config
        )

        # Assert
        scene = result["episodes"][0]["scenes"][0]
        assert scene["review_required"] is True
        assert scene["_reparse_confidence"] == 0.30

    # ── 7. test_reparse_preserves_original_confidence ───────────────────────────

    def test_reparse_preserves_original_confidence(
        self, monkeypatch, fake_pipeline_config, sample_low_confidence_result
    ):
        """The _original_confidence field records the pre-reparse confidence."""
        new_scene = _make_reparse_scene(confidence=0.95)
        _patch_llm_chain(
            monkeypatch,
            lambda backend, payload, **kwargs: _make_provider_response(new_scene),
        )

        low_conf = _find_low_confidence_scenes(
            sample_low_confidence_result, threshold=0.7
        )
        original_confidence = low_conf[0]["confidence"]

        # Act
        result = _reparse_low_confidence_scenes(
            sample_low_confidence_result, low_conf, ORIGINAL_TEXT, fake_pipeline_config
        )

        # Assert
        scene = result["episodes"][0]["scenes"][0]
        assert scene["_original_confidence"] == original_confidence
        assert scene["_original_confidence"] == 0.50

    # ── 8. test_reparse_error_field_on_exception ────────────────────────────────

    def test_reparse_error_field_on_exception(
        self, monkeypatch, fake_pipeline_config, sample_low_confidence_result
    ):
        """When reparse raises, _reparse_error contains the exception message."""
        error_msg = "Connection refused"
        _patch_llm_chain(
            monkeypatch,
            lambda backend, payload, **kwargs: (
                _ for _ in ()
            ).throw(RuntimeError(error_msg)),
        )

        low_conf = _find_low_confidence_scenes(
            sample_low_confidence_result, threshold=0.7
        )

        # Act
        result = _reparse_low_confidence_scenes(
            sample_low_confidence_result, low_conf, ORIGINAL_TEXT, fake_pipeline_config
        )

        # Assert
        scene = result["episodes"][0]["scenes"][0]
        assert error_msg in scene["_reparse_error"]

    # ── 9. test_reparse_empty_low_conf_noop ─────────────────────────────────────

    def test_reparse_empty_low_conf_noop(
        self, monkeypatch, fake_pipeline_config, sample_parsed_result
    ):
        """When low_conf is empty, the result is returned unchanged."""
        # Act
        result = _reparse_low_confidence_scenes(
            sample_parsed_result, [], "any text", fake_pipeline_config
        )

        # Assert
        assert result is sample_parsed_result
        # Verify no scenes were modified
        scene_0 = result["episodes"][0]["scenes"][0]
        assert "_reparsed" not in scene_0
        assert "review_required" not in scene_0

    # ── 10. test_reparse_payload_includes_scene_context ─────────────────────────

    def test_reparse_payload_includes_scene_context(
        self, monkeypatch, fake_pipeline_config, sample_low_confidence_result
    ):
        """The reparse LLM payload includes scene context from the original text."""
        captured_payloads: list[dict[str, Any]] = []

        new_scene = _make_reparse_scene(confidence=0.95)
        provider_response = _make_provider_response(new_scene)

        def capture_and_return(backend, payload, **kwargs):
            captured_payloads.append(dict(payload))
            return provider_response

        _patch_llm_chain(monkeypatch, capture_and_return)

        low_conf = _find_low_confidence_scenes(
            sample_low_confidence_result, threshold=0.7
        )

        # Act
        _reparse_low_confidence_scenes(
            sample_low_confidence_result, low_conf, ORIGINAL_TEXT, fake_pipeline_config
        )

        # Assert
        assert len(captured_payloads) == 1
        payload = captured_payloads[0]
        messages = payload["messages"]

        # System prompt is the reparse instruction
        system_msg = next(m for m in messages if m["role"] == "system")
        assert "re-parsing a scene" in system_msg["content"]

        # User message includes scene ID, original confidence, and context
        user_msg = next(m for m in messages if m["role"] == "user")
        assert "S1E1" in user_msg["content"]
        assert "0.50" in user_msg["content"]
        assert "Context from the original script" in user_msg["content"]
        # The original text contains the scene heading "1-1 墓地"
        assert "墓地" in user_msg["content"]

        # Payload model matches config
        assert payload["model"] == "test-model"
        assert payload["temperature"] == 0.1  # ac_auto_cut sync
        assert payload["max_tokens"] == 131072  # DEFAULT_MAX_TOKENS
        assert payload["response_format"] == {"type": "json_object"}