"""Focused regressions for the typed V2 assembly and runtime boundaries."""

from __future__ import annotations

from pathlib import Path

from autocut_core.stages.knowledge_chain.v2.layer3_global_assembly import GlobalAssembler
from autocut_core.stages.knowledge_chain.v2.types import (
    Chapter,
    ChapterOutput,
    GlobalFramework,
    JSONObject,
)
from autocut_core.stages.ports import LLMPort, LLMResponse


class FlatResponsePort(LLMPort):
    def run_batch(
        self,
        manifest_path: Path,
        *,
        backend: str,
        workers: int | str,
        requests_per_minute: float,
        semantic_retries: int,
        context_injection: dict[str, object] | None = None,
        job_ids: list[str] | None = None,
    ) -> None:
        del (
            manifest_path,
            backend,
            workers,
            requests_per_minute,
            semantic_retries,
            context_injection,
            job_ids,
        )

    def build_context_injection(
        self, stage_name: str, config: object, bus: object
    ) -> dict[str, object] | None:
        del stage_name, config, bus
        return None

    def call_llm(
        self,
        prompt: str,
        model: str,
        *,
        messages: list[dict[str, str]] | None = None,
        temperature: float = 0.1,
        max_tokens: int = 131072,
        response_format: dict[str, str] | None = None,
        timeout: float = 120.0,
    ) -> LLMResponse:
        del prompt, model, messages, temperature, max_tokens, response_format, timeout
        return {"content": "agent-flat-content"}


def _chapter(chapter_id: str, start: int, end: int) -> Chapter:
    return {
        "chapter_id": chapter_id,
        "start_ep": start,
        "end_ep": end,
        "title": chapter_id,
        "arc_type": "setup",
        "core_conflict": "fixture",
        "climax_episode": start,
        "boundary_reason": "fixture",
    }


def _beat(chapter_id: str, episode: int) -> JSONObject:
    return {
        "id": f"beat-{chapter_id}",
        "chapter_id": chapter_id,
        "thread_id": "thread-main",
        "episode": episode,
        "phase": "setup",
        "summary": "fixture",
        "evidence_event_ids": [],
        "requires_beat_ids": [],
    }


def test_assembler_deduplication_does_not_mutate_checkpoint_inputs() -> None:
    framework: GlobalFramework = {
        "series_title": "fixture",
        "chapters": [_chapter("ch-1", 1, 2), _chapter("ch-2", 2, 3)],
        "global_story_threads": [],
        "global_characters": [],
    }
    chapter_outputs: list[ChapterOutput] = [
        {
            "chapter": framework["chapters"][0],
            "beats": [_beat("ch-1", 2)],
            "excluded_episodes": [],
        },
        {
            "chapter": framework["chapters"][1],
            "beats": [_beat("ch-2", 2)],
            "excluded_episodes": [],
        },
    ]

    result = GlobalAssembler(framework, chapter_outputs, []).assemble()

    assert chapter_outputs[0]["beats"] == [_beat("ch-1", 2)]
    assert [beat.id for beat in result.beats] == ["beat-ch-2"]


def test_default_stream_adapter_accepts_agent_flat_response_shape() -> None:
    assert list(FlatResponsePort().stream_llm("prompt", "model")) == ["agent-flat-content"]
