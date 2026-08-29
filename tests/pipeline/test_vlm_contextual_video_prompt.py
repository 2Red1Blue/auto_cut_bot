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

from auto_cut_bot.pipeline.vlm.bounded_video_prompt import (
    vlm_bounded_video_response_schema,
    vlm_core_video_response_schema,
    vlm_stable_core_video_response_schema,
    vlm_validated_reciprocal_core_video_response_schema,
)
from auto_cut_bot.pipeline.vlm.contextual_video_prompt import (
    VLM_CONTEXTUAL_CLOSED_VOCABULARY_CORE_VIDEO_PROMPT_VERSION,
    VLM_CONTEXTUAL_COMPACT_CANONICAL_CORE_VIDEO_PROMPT_VERSION,
    VLM_CONTEXTUAL_CORE_VIDEO_PROMPT_VERSION,
    VLM_CONTEXTUAL_ENUM_DISAMBIGUATED_FACT_ANCHORED_EVENT_CORE_VIDEO_PROMPT_VERSION,
    VLM_CONTEXTUAL_FACT_ANCHORED_EVENT_CORE_VIDEO_PROMPT_VERSION,
    VLM_CONTEXTUAL_MINIMAL_CORE_VIDEO_PROMPT_VERSION,
    VLM_CONTEXTUAL_RECIPROCAL_CAUSAL_CORE_VIDEO_PROMPT_VERSION,
    VLM_CONTEXTUAL_REQUIRED_EMPTY_ARRAY_CORE_VIDEO_PROMPT_VERSION,
    VLM_CONTEXTUAL_STABLE_CORE_VIDEO_PROMPT_VERSION,
    VLM_CONTEXTUAL_STRICT_WIRE_CORE_VIDEO_PROMPT_VERSION,
    VLM_CONTEXTUAL_TIMELINE_CORE_VIDEO_PROMPT_VERSION,
    VLM_CONTEXTUAL_VALIDATED_RECIPROCAL_CAUSAL_CORE_VIDEO_PROMPT_VERSION,
    VLM_CONTEXTUAL_VIDEO_PROMPT_VERSION,
    build_vlm_contextual_video_prompt,
)
from auto_cut_bot.pipeline.vlm.request_factory import (
    build_doubao_vlm_request,
    registered_response_schema_json,
)
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
    v7 = replace(
        _policy(),
        prompt_version=VLM_CONTEXTUAL_VIDEO_PROMPT_VERSION,
        response_schema_json=registered_response_schema_json(
            _policy().parser_strategy_version,
            VLM_CONTEXTUAL_VIDEO_PROMPT_VERSION,
        ),
    )
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
    with pytest.raises(ValueError, match="contextual video prompt requires"):
        build_doubao_vlm_request(
            source_bundle=bundle,
            episode_index=0,
            job=bundle.source_job,
            artifact_revision=1,
            idempotency_key="missing-pack",
            policy=v7,
            retry_policy=factory_fixture._retry_policy(),
        )
    with pytest.raises(ValueError, match="contextual video prompt requires"):
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


def test_v7_contextual_prompt_keeps_v6_video_only_schema() -> None:
    """Narrative context may enrich interpretation, never widen media evidence."""

    schema = json.loads(
        registered_response_schema_json(
            _policy().parser_strategy_version,
            VLM_CONTEXTUAL_VIDEO_PROMPT_VERSION,
        )
    )
    assert schema == vlm_bounded_video_response_schema()
    support = schema["properties"]["entities"]["items"]["properties"]["support"]
    assert support["properties"]["support_kind"]["const"] == "video_observation"
    assert "supporting_frame_ids" not in support["properties"]


def test_v8_contextual_core_prompt_forbids_candidate_generation() -> None:
    pack = _pack()
    prompt = build_vlm_contextual_video_prompt(
        factory_fixture._prepared_episode().manifest,
        pack,
        prompt_version=VLM_CONTEXTUAL_CORE_VIDEO_PROMPT_VERSION,
    )
    schema = json.loads(
        registered_response_schema_json(
            _policy().parser_strategy_version,
            VLM_CONTEXTUAL_CORE_VIDEO_PROMPT_VERSION,
        )
    )
    assert "candidate_hypotheses 必须严格输出空数组 []" in prompt
    assert schema == vlm_core_video_response_schema()
    assert schema["properties"]["candidate_hypotheses"]["maxItems"] == 0


def test_v9_contextual_timeline_core_prompt_requires_event_fact_temporal_closure() -> None:
    pack = _pack()
    prompt = build_vlm_contextual_video_prompt(
        factory_fixture._prepared_episode().manifest,
        pack,
        prompt_version=VLM_CONTEXTUAL_TIMELINE_CORE_VIDEO_PROMPT_VERSION,
    )
    schema = json.loads(
        registered_response_schema_json(
            _policy().parser_strategy_version,
            VLM_CONTEXTUAL_TIMELINE_CORE_VIDEO_PROMPT_VERSION,
        )
    )
    assert "event.fact_refs 的每项均与事件区间相交" in prompt
    assert "candidate_hypotheses 必须严格输出空数组 []" in prompt
    assert schema == vlm_core_video_response_schema()


def test_v10_contextual_closed_vocabulary_keeps_core_schema_and_spells_out_fact_kinds() -> None:
    pack = _pack()
    prompt = build_vlm_contextual_video_prompt(
        factory_fixture._prepared_episode().manifest,
        pack,
        prompt_version=VLM_CONTEXTUAL_CLOSED_VOCABULARY_CORE_VIDEO_PROMPT_VERSION,
    )
    schema = json.loads(
        registered_response_schema_json(
            _policy().parser_strategy_version,
            VLM_CONTEXTUAL_CLOSED_VOCABULARY_CORE_VIDEO_PROMPT_VERSION,
        )
    )
    assert "fact_kind 仅 visible_presence、visible_state" in prompt
    assert "禁止 visible_reaction" in prompt
    assert "window_summary.summary不超过360个字符" in prompt
    assert schema == vlm_core_video_response_schema()


def test_v11_contextual_compact_canonical_prompt_requires_json_string_references() -> None:
    pack = _pack()
    prompt = build_vlm_contextual_video_prompt(
        factory_fixture._prepared_episode().manifest,
        pack,
        prompt_version=VLM_CONTEXTUAL_COMPACT_CANONICAL_CORE_VIDEO_PROMPT_VERSION,
    )
    schema = json.loads(
        registered_response_schema_json(
            _policy().parser_strategy_version,
            VLM_CONTEXTUAL_COMPACT_CANONICAL_CORE_VIDEO_PROMPT_VERSION,
        )
    )
    assert "最多输出12个实体、18个事实、10个事件" in prompt
    assert "带双引号的 JSON 字符串，例如 \"p001\"" in prompt
    assert schema == vlm_core_video_response_schema()


def test_v12_contextual_prompt_requires_reciprocal_causal_edges() -> None:
    pack = _pack()
    prompt = build_vlm_contextual_video_prompt(
        factory_fixture._prepared_episode().manifest,
        pack,
        prompt_version=VLM_CONTEXTUAL_RECIPROCAL_CAUSAL_CORE_VIDEO_PROMPT_VERSION,
    )
    schema = json.loads(
        registered_response_schema_json(
            _policy().parser_strategy_version,
            VLM_CONTEXTUAL_RECIPROCAL_CAUSAL_CORE_VIDEO_PROMPT_VERSION,
        )
    )
    assert "事件因果边必须成对" in prompt
    assert "反向引用完全一致" in prompt
    assert schema == vlm_core_video_response_schema()


def test_v13_binds_output_self_check_and_bounded_uncertainty() -> None:
    pack = _pack()
    prompt = build_vlm_contextual_video_prompt(
        factory_fixture._prepared_episode().manifest,
        pack,
        prompt_version=VLM_CONTEXTUAL_VALIDATED_RECIPROCAL_CAUSAL_CORE_VIDEO_PROMPT_VERSION,
    )
    schema = json.loads(
        registered_response_schema_json(
            _policy().parser_strategy_version,
            VLM_CONTEXTUAL_VALIDATED_RECIPROCAL_CAUSAL_CORE_VIDEO_PROMPT_VERSION,
        )
    )
    assert "机械自检" in prompt
    assert "0 到 5000" in prompt
    assert schema == vlm_validated_reciprocal_core_video_response_schema()
    assert (
        schema["properties"]["events"]["items"]["properties"]["support"]
        ["properties"]["interval_ms"]["properties"]["uncertainty_ms"]["maximum"]
        == 5_000
    )


def test_v14_closes_nonessential_redundant_graph_fields() -> None:
    pack = _pack()
    prompt = build_vlm_contextual_video_prompt(
        factory_fixture._prepared_episode().manifest,
        pack,
        prompt_version=VLM_CONTEXTUAL_STABLE_CORE_VIDEO_PROMPT_VERSION,
    )
    schema = json.loads(
        registered_response_schema_json(
            _policy().parser_strategy_version,
            VLM_CONTEXTUAL_STABLE_CORE_VIDEO_PROMPT_VERSION,
        )
    )
    assert "冗余图字段固定为空" in prompt
    assert schema == vlm_stable_core_video_response_schema()
    event_properties = schema["properties"]["events"]["items"]["properties"]
    assert event_properties["cause_event_refs"]["maxItems"] == 0
    assert event_properties["effect_event_refs"]["maxItems"] == 0
    assert schema["properties"]["continuity"]["properties"]["temporal_segments"]["maxItems"] == 0


def test_v19_uses_only_the_core_observation_prompt_and_stable_schema() -> None:
    pack = _pack()
    prompt = build_vlm_contextual_video_prompt(
        factory_fixture._prepared_episode().manifest,
        pack,
        prompt_version=VLM_CONTEXTUAL_MINIMAL_CORE_VIDEO_PROMPT_VERSION,
    )
    schema = json.loads(
        registered_response_schema_json(
            _policy().parser_strategy_version,
            VLM_CONTEXTUAL_MINIMAL_CORE_VIDEO_PROMPT_VERSION,
        )
    )
    assert "本轮只输出可由当前视频支持的核心观察" in prompt
    assert "候选只是高光/钩子假设" not in prompt
    assert "你会得到一段外部剧情辅助" not in prompt
    assert "visible_reaction" not in prompt
    assert "candidate_hypotheses 必须是 []" in prompt
    assert "continues_from_previous=false" in prompt
    assert "exit_state_fact_refs=[]" in prompt
    assert prompt.index("播放窗口：") < prompt.index('{"duration_ms_floor":') < prompt.index("剧情辅助：")
    assert schema == vlm_stable_core_video_response_schema()
    event_properties = schema["properties"]["events"]["items"]["properties"]
    assert event_properties["cause_event_refs"]["maxItems"] == 0
    assert event_properties["effect_event_refs"]["maxItems"] == 0


def test_v15_requires_explicit_empty_fields_without_closing_them_in_schema() -> None:
    pack = _pack()
    prompt = build_vlm_contextual_video_prompt(
        factory_fixture._prepared_episode().manifest,
        pack,
        prompt_version=VLM_CONTEXTUAL_REQUIRED_EMPTY_ARRAY_CORE_VIDEO_PROMPT_VERSION,
    )
    schema = json.loads(
        registered_response_schema_json(
            _policy().parser_strategy_version,
            VLM_CONTEXTUAL_REQUIRED_EMPTY_ARRAY_CORE_VIDEO_PROMPT_VERSION,
        )
    )
    assert "绝不能省略" in prompt
    assert "字符串 \"null\"" in prompt
    assert schema == vlm_validated_reciprocal_core_video_response_schema()
    event_properties = schema["properties"]["events"]["items"]["properties"]
    assert event_properties["cause_event_refs"]["maxItems"] > 0
    assert event_properties["effect_event_refs"]["maxItems"] > 0


def test_v16_uses_one_unambiguous_wire_protocol_for_ids_and_enums() -> None:
    pack = _pack()
    prompt = build_vlm_contextual_video_prompt(
        factory_fixture._prepared_episode().manifest,
        pack,
        prompt_version=VLM_CONTEXTUAL_STRICT_WIRE_CORE_VIDEO_PROMPT_VERSION,
    )
    schema = json.loads(
        registered_response_schema_json(
            _policy().parser_strategy_version,
            VLM_CONTEXTUAL_STRICT_WIRE_CORE_VIDEO_PROMPT_VERSION,
        )
    )
    assert "绝不能输出 /p008、裸 p008、\"null\"" in prompt
    assert "confrontation、argument、fight、dialogue、emotion 都不是合法 event_kind" in prompt
    assert "\"object_ref\":null" in prompt
    assert schema == vlm_validated_reciprocal_core_video_response_schema()


def test_v17_requires_fact_anchored_event_support() -> None:
    pack = _pack()
    prompt = build_vlm_contextual_video_prompt(
        factory_fixture._prepared_episode().manifest,
        pack,
        prompt_version=VLM_CONTEXTUAL_FACT_ANCHORED_EVENT_CORE_VIDEO_PROMPT_VERSION,
    )
    schema = json.loads(
        registered_response_schema_json(
            _policy().parser_strategy_version,
            VLM_CONTEXTUAL_FACT_ANCHORED_EVENT_CORE_VIDEO_PROMPT_VERSION,
        )
    )
    assert "每一个 event.fact_refs 必须恰好有一个" in prompt
    assert "event.support 必须逐字复制该唯一 fact 的完整 support 对象" in prompt
    assert "[198000,213000)" in prompt
    assert schema == vlm_validated_reciprocal_core_video_response_schema()


def test_v18_disambiguates_fact_kind_without_widening_the_wire_schema() -> None:
    pack = _pack()
    prompt = build_vlm_contextual_video_prompt(
        factory_fixture._prepared_episode().manifest,
        pack,
        prompt_version=(
            VLM_CONTEXTUAL_ENUM_DISAMBIGUATED_FACT_ANCHORED_EVENT_CORE_VIDEO_PROMPT_VERSION
        ),
    )
    schema = json.loads(
        registered_response_schema_json(
            _policy().parser_strategy_version,
            VLM_CONTEXTUAL_ENUM_DISAMBIGUATED_FACT_ANCHORED_EVENT_CORE_VIDEO_PROMPT_VERSION,
        )
    )
    assert "尤其禁止 visible_state_change" in prompt
    assert "状态改变写 visible_change" in prompt
    assert "visible_reaction" in prompt
    assert "\"object_ref\":null" in prompt
    assert "严格 JSON 解析器" in prompt
    assert schema == vlm_validated_reciprocal_core_video_response_schema()
