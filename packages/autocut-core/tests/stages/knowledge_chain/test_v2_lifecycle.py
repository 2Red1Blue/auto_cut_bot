"""Offline lifecycle regressions for the historical knowledge-chain v2 Stage."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from autocut_core import ArtifactBus, PipelineConfig
from autocut_core.stages.knowledge_chain.v2 import (
    KnowledgeChainV2Output,
    run_knowledge_chain_v2,
)
from autocut_core.stages.knowledge_chain.v2 import stage as stage_module
from autocut_core.stages.knowledge_chain.v2.layer1_global_segmenter import GlobalSegmenter
from autocut_core.stages.knowledge_chain.v2.metrics import MetricsCollector
from autocut_core.stages.knowledge_chain.v2.schemas import StoryThread
from autocut_core.stages.knowledge_chain.v2.stage import KnowledgeChainV2Stage
from autocut_core.stages.ports import LLMPort, LLMStreamChunk


class FakeLLMPort(LLMPort):
    """Network-free port that supports both true stream chunks and fallback."""

    def __init__(self, chunks: list[str] | None = None) -> None:
        self.chunks = chunks
        self.calls: list[dict[str, Any]] = []

    def run_batch(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("knowledge-chain v2 must not call batch inference")

    def build_context_injection(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def call_llm(self, prompt: str, model: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"prompt": prompt, "model": model, **kwargs})
        return {"choices": [{"message": {"content": "fallback-content"}}]}

    def stream_llm(
        self, prompt: str, model: str, **kwargs: Any
    ) -> Generator[LLMStreamChunk, None, None]:
        self.calls.append({"prompt": prompt, "model": model, **kwargs})
        if self.chunks is None:
            yield from super().stream_llm(prompt, model, **kwargs)
            return
        yield from self.chunks
        yield {"done": True}


def _framework() -> dict[str, Any]:
    return {
        "chapters": [
            {
                "chapter_id": "ch01-1-1",
                "start_ep": 1,
                "end_ep": 1,
                "title": "Opening",
                "arc_type": "setup",
                "core_conflict": "conflict",
                "climax_episode": 1,
                "boundary_reason": "fixture",
            }
        ],
        "global_story_threads": [],
        "global_character_preview": [],
    }


def _chapter_output() -> dict[str, Any]:
    return {
        "chapter": _framework()["chapters"][0],
        "summary": "fixture chapter",
        "beats": [],
        "character_rollup": [],
        "relationship_rollup": [],
        "excluded_episodes": [],
    }


class FakeSegmenter:
    calls = 0

    def __init__(self, llm_provider: Any, extra_config: Any) -> None:
        self.llm_provider = llm_provider
        self.last_prompt = "segment fixture"

    async def run(self, _episodes: list[dict[str, Any]]) -> dict[str, Any]:
        type(self).calls += 1
        await self.llm_provider("segment fixture")
        return _framework()


class FakeProcessor:
    calls = 0

    def __init__(self, llm_provider: Any, *_args: Any, **_kwargs: Any) -> None:
        self.llm_provider = llm_provider

    def update_rolling_context(self, _chapter: dict[str, Any]) -> None:
        return None

    def _get_overlap_eps(self, _index: int) -> list[int]:
        return []

    async def process_chapter(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        type(self).calls += 1
        await self.llm_provider("chapter fixture")
        return _chapter_output()


class FakeAssembler:
    calls: list[tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]] = []

    def __init__(
        self,
        global_framework: dict[str, Any],
        chapter_outputs: list[dict[str, Any]],
        event_cards: list[dict[str, Any]],
    ) -> None:
        type(self).calls.append((global_framework, chapter_outputs, event_cards))

    def assemble(self) -> KnowledgeChainV2Output:
        return KnowledgeChainV2Output(
            metadata={"metrics": {}},
            story_threads=[StoryThread(id="thread-1", name="fixture thread")],
        )


def _stage(tmp_path: Path, port: LLMPort) -> KnowledgeChainV2Stage:
    return KnowledgeChainV2Stage(PipelineConfig(job_root=tmp_path, model="fixture-model"), port)


def _upstream(bus: ArtifactBus, tmp_path: Path) -> None:
    digests = tmp_path / "episode-digests.jsonl"
    digests.write_text(json.dumps({"episode": 1, "summary": "episode one"}) + "\n")
    cards = tmp_path / "event-cards.jsonl"
    cards.write_text(json.dumps({"id": "event-1", "episode": 1, "summary": "event"}) + "\n")
    catalog = tmp_path / "catalog.jsonl"
    catalog.write_text("{}\n")
    bus.put("episode_digests", {"path": str(digests)}, stage="episode_digests")
    bus.put("event_cards", {"path": str(cards)}, stage="event_cards")
    bus.put("highlight_hook_catalog", {"path": str(catalog)}, stage="event_cards")


def test_public_api_imports_and_uses_stage_assembler_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import autocut_core.stages.knowledge_chain.v2.layer1_global_segmenter as segmenter_module
    import autocut_core.stages.knowledge_chain.v2.layer2_chapter_processor as processor_module
    import autocut_core.stages.knowledge_chain.v2.layer3_global_assembly as assembly_module

    FakeAssembler.calls.clear()
    monkeypatch.setattr(segmenter_module, "GlobalSegmenter", FakeSegmenter)
    monkeypatch.setattr(processor_module, "ChapterProcessor", FakeProcessor)
    monkeypatch.setattr(assembly_module, "GlobalAssembler", FakeAssembler)

    async def fake_llm(*_args: Any, **_kwargs: Any) -> str:
        return "{}"

    result = asyncio.run(run_knowledge_chain_v2([{"ep": 1, "summary": "episode"}], [], fake_llm))

    assert result.metadata["schema_version"] == "2.0"
    assert result.metadata["total_episodes"] == 1
    assert len(FakeAssembler.calls) == 1


def test_global_segmenter_metadata_uses_module_defaultdict_without_local_binding() -> None:
    segmenter = GlobalSegmenter(lambda *_args, **_kwargs: "{}")
    metadata = segmenter._extract_vlm_metadata(
        [{"ep": 1, "story_beats": [{"function": "高潮", "summary": "turn"}]}]
    )

    assert metadata["highlight_candidates"][1] == ["turn"]


def test_stage_contract_prepare_missing_upstream_and_success(tmp_path: Path) -> None:
    stage = _stage(tmp_path, FakeLLMPort())
    bus = ArtifactBus(tmp_path)

    assert stage.contract.input_artifacts == [
        "episode_digests",
        "event_cards",
        "highlight_hook_catalog",
    ]
    assert stage.contract.output_artifacts == ["narrative_blueprint"]
    with pytest.raises(RuntimeError, match="Missing upstream artifacts"):
        stage.prepare(bus)

    _upstream(bus, tmp_path)
    tasks = stage.prepare(bus)
    assert len(tasks) == 1
    assert set(tasks[0].payload) == {"episode_digests", "event_cards", "catalog"}


def test_stage_execute_publishes_blueprint_with_deterministic_streaming_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeSegmenter.calls = 0
    FakeProcessor.calls = 0
    monkeypatch.setattr(stage_module, "GlobalSegmenter", FakeSegmenter)
    monkeypatch.setattr(stage_module, "ChapterProcessor", FakeProcessor)
    monkeypatch.setattr(stage_module, "GlobalAssembler", FakeAssembler)
    stage = _stage(tmp_path, FakeLLMPort(chunks=["part-", "one"]))
    bus = ArtifactBus(tmp_path)
    _upstream(bus, tmp_path)

    refs = stage.execute(bus, stage.prepare(bus))

    assert len(refs) == 1
    assert refs[0].name == "narrative_blueprint"
    assert (tmp_path / "narrative-blueprint.json").is_file()
    assert bus.resolve("knowledge_chain_v2", "narrative_blueprint") == refs[0]
    assert stage.validate(bus, refs) is True
    assert FakeSegmenter.calls == 1
    assert FakeProcessor.calls == 1


def test_resume_skips_checkpointed_work_but_force_reexecutes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeSegmenter.calls = 0
    FakeProcessor.calls = 0
    monkeypatch.setattr(stage_module, "GlobalSegmenter", FakeSegmenter)
    monkeypatch.setattr(stage_module, "ChapterProcessor", FakeProcessor)
    monkeypatch.setattr(stage_module, "GlobalAssembler", FakeAssembler)
    stage = _stage(tmp_path, FakeLLMPort(chunks=["ok"]))
    stage._debug_dir = tmp_path / "kc-v2-debug"
    stage._debug_dir.mkdir()
    stage._checkpoint_dir = tmp_path / "kc-v2-checkpoints"
    stage._checkpoint_dir.mkdir()
    stage._metrics = MetricsCollector()
    checkpoint = {"global_framework": _framework(), "chapter_outputs": [_chapter_output()]}
    (stage._checkpoint_dir / "checkpoint.json").write_text(json.dumps(checkpoint))

    asyncio.run(
        stage._run_pipeline([], [], stage_module.KnowledgeChainV2ExtraConfig(), resume=True)
    )
    assert FakeSegmenter.calls == 0
    assert FakeProcessor.calls == 0

    asyncio.run(
        stage._run_pipeline(
            [], [], stage_module.KnowledgeChainV2ExtraConfig(), resume=True, force=True
        )
    )
    assert FakeSegmenter.calls == 1
    assert FakeProcessor.calls == 1


def test_invalid_blueprint_fails_schema_validation_and_stream_fallback_is_complete(
    tmp_path: Path,
) -> None:
    port = FakeLLMPort()
    stage = _stage(tmp_path, port)
    (tmp_path / "narrative-blueprint.json").write_text(json.dumps({"story_threads": []}))

    assert stage.validate(ArtifactBus(tmp_path), []) is False

    (tmp_path / "narrative-blueprint.json").write_text(
        json.dumps({"story_threads": "bad", "characters": [], "beats": []})
    )
    assert stage.validate(ArtifactBus(tmp_path), []) is False

    (tmp_path / "narrative-blueprint.json").write_text(
        json.dumps({"chapters": [], "story_threads": [], "characters": [], "beats": []})
    )
    assert stage.validate(ArtifactBus(tmp_path), []) is False

    assert asyncio.run(stage._llm_adapter("fallback prompt")) == "fallback-content"
