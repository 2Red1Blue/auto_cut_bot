"""Typed-boundary regressions for the historical Layer2 chapter processor."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any

from autocut_core.stages.knowledge_chain.v2.layer2_chapter_processor import (
    ChapterProcessor,
    _robust_parse_json,
)


class TruncatedProvider:
    def __call__(self, _prompt: str, **_kwargs: Any) -> Awaitable[str]:
        async def response() -> str:
            return '{"summary":"cut off","story_thread_updates":['

        return response()


def _framework() -> dict[str, Any]:
    return {
        "chapters": [{"chapter_id": "ch01", "start_ep": 1, "end_ep": 1}],
        "global_story_threads": [{"thread_id": "thread-main"}],
    }


def test_truncated_llm_json_is_not_repaired_into_a_partial_semantic_result() -> None:
    assert _robust_parse_json('{"summary":"cut off","story_thread_updates":[') is None

    processor = ChapterProcessor(TruncatedProvider(), _framework())
    fallback = asyncio.run(
        processor._run_pass1(
            _framework()["chapters"][0],
            [],
            [],
            "",
        )
    )

    assert fallback["summary"] == ""
    assert fallback["story_thread_updates"] == []


def test_pass1_validation_normalizes_a_copy_and_does_not_mutate_retry_input() -> None:
    processor = ChapterProcessor(TruncatedProvider(), _framework())
    raw = {
        "summary": "chapter",
        "story_thread_updates": [
            {
                "thread_id": "thread-main",
                "beats": [
                    {
                        "beat_sid": "B1",
                        "episode": 1,
                        "phase": "setup",
                        "event_eids": ["E1"],
                        "summary": "opening",
                        "depends_on_beat_sids": [],
                    }
                ],
            }
        ],
        "excluded_episodes": [],
        "new_facts": [],
        "resolved_questions": [],
        "new_open_questions": [],
    }
    original = str(raw)

    normalized, _ = processor._validate_pass1(
        raw,
        _framework()["chapters"][0],
        [{"id": "event-1", "ep": 1}],
        [],
        "ch01",
    )

    assert str(raw) == original
    assert normalized["beats"][0]["evidence_event_ids"] == ["event-1"]
