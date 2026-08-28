from __future__ import annotations

import json
from dataclasses import replace

import pytest
from autocut_kernel.context_pack import (
    EpisodeContextBinding,
    ExternalContextSnapshot,
    build_window_context_pack,
    normalize_narrative_context,
)

from auto_cut_bot.pipeline.vlm.contextual_video_prompt import (
    VLM_CONTEXTUAL_VIDEO_PROMPT_VERSION,
    build_vlm_contextual_video_prompt,
)
from auto_cut_bot.pipeline.vlm.request_factory import build_doubao_vlm_request
from tests.pipeline import test_doubao_vlm_request_factory as factory_fixture
from tests.pipeline.test_vlm_video_integration import _policy


def _pack():
    assets = {"data": {"book-1": {"bookId": "book-1", "bookName": "Show", "characters": [{"characterId": "c1", "name": "Alice"}]}}}
    episodes = {"data": {"book-1": {"bookId": "book-1", "episodes": [{"episodeId": "ep-1", "chapterId": "ch-1", "title": "Start", "summary": "Alice arrives.", "characters": ["c1"]}]}}}
    snapshot = ExternalContextSnapshot(
        "snapshot:1", "book-1", ("/assets/a", "/assets/e"), "https://metadata.example", "default", "sha256:" + "a" * 64
    )
    normalized = normalize_narrative_context(snapshot, asset_response=assets, episode_response=episodes)
    binding = EpisodeContextBinding("source-001", "sha256:" + "a" * 64, 0, "book-1", "ep-1", "ch-1", 1)
    return build_window_context_pack(normalized, binding, local_source_id="source-001", local_source_sha256="sha256:" + "a" * 64, local_episode_index=0)


def test_contextual_prompt_binds_only_pack_text_and_retains_video_evidence_boundary() -> None:
    pack = _pack()
    prompt = build_vlm_contextual_video_prompt(factory_fixture._prepared_episode().manifest, pack)
    assert "当前集标题：Start" in prompt
    assert "不是视频证据" in prompt
    assert "不得把其中的人名、关系、剧情或主题直接写成已观察到的 entity、fact、event" in prompt
    assert "ASR" in prompt and "字幕" in prompt


def test_v7_request_binds_exact_context_pack_and_v6_refuses_it() -> None:
    bundle = factory_fixture._source_bundle()
    pack = _pack()
    v7 = replace(_policy(), prompt_version=VLM_CONTEXTUAL_VIDEO_PROMPT_VERSION)
    request = build_doubao_vlm_request(
        source_bundle=bundle,
        episode_index=0,
        job=bundle.source_job,
        artifact_revision=1,
        idempotency_key="contextual-v7",
        policy=v7,
        retry_policy=factory_fixture._retry_policy(),
        context_pack=pack,
    )
    payload = json.loads(request.request_payload)
    assert payload["context_pack"] == pack.to_mapping()
    assert payload["context_pack_sha256"] == pack.canonical_hash
    assert request.context_pack == pack
    assert "Alice arrives." in request.prompt_template
    with pytest.raises(ValueError, match="prompt v7 requires"):
        build_doubao_vlm_request(
            source_bundle=bundle,
            episode_index=0,
            job=bundle.source_job,
            artifact_revision=1,
            idempotency_key="missing-pack",
            policy=v7,
            retry_policy=factory_fixture._retry_policy(),
        )
    with pytest.raises(ValueError, match="prompt v7 requires"):
        build_doubao_vlm_request(
            source_bundle=bundle,
            episode_index=0,
            job=bundle.source_job,
            artifact_revision=1,
            idempotency_key="v6-with-pack",
            policy=factory_fixture._policy(),
            retry_policy=factory_fixture._retry_policy(),
            context_pack=pack,
        )
