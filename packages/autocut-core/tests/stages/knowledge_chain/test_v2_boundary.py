"""Typed ingress regressions for the historical knowledge-chain V2 layers."""

from __future__ import annotations

import asyncio

from autocut_core.stages.knowledge_chain.v2.layer1_global_segmenter import GlobalSegmenter


def test_layer1_rejects_malformed_llm_object_with_a_deterministic_fallback() -> None:
    async def malformed_provider(_prompt: str, **_kwargs: object) -> str:
        return '{"ch": "not-an-array"}'

    framework = asyncio.run(
        GlobalSegmenter(malformed_provider).run([{"ep": 1, "summary": "Episode one"}])
    )

    assert framework["chapters"][0]["boundary_reason"].startswith("fallback:")
    assert framework["chapters"][0]["end_ep"] == 1
