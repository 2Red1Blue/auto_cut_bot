"""Typed public entry point for the historical Knowledge Chain V2 pipeline."""

from typing import cast

from .schemas import KnowledgeChainV2ExtraConfig, KnowledgeChainV2Output
from .types import (
    ChapterOutput,
    EpisodeSummary,
    EventCard,
    JSONObject,
    LLMProvider,
)


def _event_episode(event: EventCard) -> int:
    """Use the legacy ``ep`` spelling first, with ``episode`` compatibility."""

    return event.get("ep", event.get("episode", 0))


def _chapter_output(value: JSONObject) -> ChapterOutput:
    """Assert the Layer 2 boundary needed by the typed deterministic assembler."""

    chapter = value.get("chapter")
    if not isinstance(chapter, dict):
        raise ValueError("Layer2 chapter output is missing its validated chapter object")
    return cast(ChapterOutput, value)


async def run_knowledge_chain_v2(
    episode_summaries: list[EpisodeSummary],
    event_cards: list[EventCard],
    llm_provider: LLMProvider,
    extra_config: KnowledgeChainV2ExtraConfig | None = None,
) -> KnowledgeChainV2Output:
    extra_config = extra_config or KnowledgeChainV2ExtraConfig()
    from .layer1_global_segmenter import GlobalSegmenter
    from .layer2_chapter_processor import ChapterProcessor
    from .layer3_global_assembly import GlobalAssembler

    segmenter = GlobalSegmenter(llm_provider, extra_config)
    global_framework = await segmenter.run(episode_summaries)

    processor = ChapterProcessor(llm_provider, global_framework, extra_config)
    chapter_outputs: list[ChapterOutput] = []
    prev_summary = ""

    for ch_idx, chapter in enumerate(global_framework.get("chapters", [])):
        start_ep = chapter["start_ep"]
        end_ep = chapter["end_ep"]
        chapter_events = [
            event for event in event_cards if start_ep <= _event_episode(event) <= end_ep
        ]
        chapters = global_framework.get("chapters", [])
        if ch_idx < len(chapters) - 1:
            next_ch = chapters[ch_idx + 1]
            overlap_start = max(start_ep, next_ch["start_ep"])
            overlap_end = min(end_ep, next_ch["end_ep"])
            overlap_eps: set[int] = (
                set(range(overlap_start, overlap_end + 1))
                if overlap_start <= overlap_end
                else set()
            )
            chapter_events = [
                event for event in chapter_events if _event_episode(event) not in overlap_eps
            ]

        chapter_out = await processor.process_chapter(
            chapter=chapter,
            chapter_events=chapter_events,
            ch_index=ch_idx,
            prev_summary=prev_summary,
        )
        typed_output = _chapter_output(chapter_out)
        chapter_outputs.append(typed_output)
        summary = typed_output.get("summary")
        prev_summary = summary if isinstance(summary, str) else ""

    assembler = GlobalAssembler(global_framework, chapter_outputs, event_cards)
    result = assembler.assemble()
    result.metadata["schema_version"] = "2.0"
    result.metadata["total_episodes"] = len(episode_summaries)
    result.metadata["total_chapters"] = len(global_framework.get("chapters", []))
    result.metadata["total_llm_calls"] = 1 + 2 * len(chapter_outputs)
    return result


__all__ = ["run_knowledge_chain_v2", "KnowledgeChainV2Output", "KnowledgeChainV2ExtraConfig"]
