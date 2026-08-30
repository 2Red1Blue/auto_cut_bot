"""Generation constraints are not a replacement for V4 semantic admission."""

from __future__ import annotations

import copy
import hashlib
import json
from fractions import Fraction
from typing import Any

import pytest
from autocut_kernel.media.types import TimeBase
from autocut_kernel.vlm.parser import VlmResponseRejected
from autocut_kernel.vlm.semantic_contracts import VLM_PARSER_V4, parser_contract_sha256_for
from jsonschema import Draft202012Validator, ValidationError

from auto_cut_bot.pipeline.vlm.bounded_video_prompt import (
    VLM_BOUNDED_VIDEO_PROMPT_TEMPLATE,
    VLM_BOUNDED_VIDEO_PROMPT_VERSION,
    VLM_VIDEO_FIELD_ORDER,
    build_vlm_bounded_video_prompt,
    ordered_bounded_video_schema,
    vlm_bounded_video_response_schema,
    vlm_bounded_video_response_schema_json,
)
from auto_cut_bot.pipeline.vlm.prompt import build_vlm_prompt, vlm_prompt_template_sha256
from auto_cut_bot.pipeline.vlm.video_prompt import (
    VLM_VIDEO_PROMPT_TEMPLATE,
    VLM_VIDEO_PROMPT_VERSION,
    build_vlm_video_prompt,
    vlm_video_response_schema,
    vlm_video_response_schema_json,
)
from tests.pipeline.test_doubao_vlm_request_factory import _prepared_episode
from tests.vlm.test_semantic_pack_v4 import _parse, _wire
from tests.vlm.test_semantic_support_v4 import _context


def _bounded_wire() -> dict[str, Any]:
    # Build a short-ID test fixture; the production path never repairs responses.
    wire = json.dumps(_wire())
    for old, new in (("entity_1", "p001"), ("fact_1", "f001"), ("event_1", "e001"), ("candidate_1", "c001")):
        wire = wire.replace(f'"{old}"', f'"{new}"')
    return json.loads(wire)


def test_minimal_playback_context_has_no_hash_or_frame_list() -> None:
    manifest = _prepared_episode().manifest
    rendered = build_vlm_bounded_video_prompt(manifest)
    assert build_vlm_prompt(manifest, prompt_version=VLM_BOUNDED_VIDEO_PROMPT_VERSION) == rendered
    assert json.loads(rendered[len(VLM_BOUNDED_VIDEO_PROMPT_TEMPLATE):]) == {"duration_ms_floor": 100}
    assert "sha256:" not in rendered and "frame_refs" not in rendered
    assert "reference_frames" not in rendered and "file_id" not in rendered


def test_historical_prompt_schema_and_parser_stay_byte_exact() -> None:
    assert vlm_prompt_template_sha256(VLM_VIDEO_PROMPT_VERSION) == "sha256:ec079b2d1dc2154928d34f60effe83f9fa30ea7fce14678086c5277004942b4c"
    digest = "sha256:" + hashlib.sha256(vlm_video_response_schema_json().encode()).hexdigest()
    assert digest == "sha256:e389f11b66e885f2b686bc32f811e9fe49d4839f4f07b3f6a39495df69d6d67d"
    assert parser_contract_sha256_for(VLM_PARSER_V4) == "sha256:bd7a642ab2f3bf84dd99ea06297134a284f5e2c0d092b26fb832f9d3d7ccd63f"


def test_unregistered_prompt_version_has_no_current_fallback() -> None:
    unknown = VLM_BOUNDED_VIDEO_PROMPT_VERSION + "-unknown"
    with pytest.raises(ValueError, match="registered VLM prompt version"):
        build_vlm_prompt(_prepared_episode().manifest, prompt_version=unknown)
    with pytest.raises(ValueError, match="registered VLM prompt version"):
        vlm_prompt_template_sha256(unknown)


def test_schema_is_smaller_video_only_and_does_not_drop_semantic_fields() -> None:
    old_json = vlm_video_response_schema_json()
    old = json.loads(old_json)
    schema = vlm_bounded_video_response_schema()
    Draft202012Validator.check_schema(schema)
    assert len(vlm_bounded_video_response_schema_json()) < len(old_json)
    for group in ("entities", "facts", "events", "candidate_hypotheses"):
        props = schema["properties"][group]["items"]["properties"]
        assert set(props) == set(old["properties"][group]["items"]["properties"])
        support = props["support"]
        assert "oneOf" not in support
        assert set(support["properties"]) == {"support_kind", "interval_ms", "confidence"}
        assert support["properties"]["support_kind"]["const"] == "video_observation"
    schema["properties"].clear()
    assert vlm_bounded_video_response_schema()["properties"]
    assert vlm_video_response_schema_json() == old_json


@pytest.mark.parametrize("group,key,prefix,limit", [
    ("entities", "local_entity_id", "p", 24),
    ("facts", "local_fact_id", "f", 48),
    ("events", "local_event_id", "e", 24),
    ("candidate_hypotheses", "local_candidate_id", "c", 8),
])
def test_short_id_ranges_cover_the_whole_existing_budget(group: str, key: str, prefix: str, limit: int) -> None:
    root = vlm_bounded_video_response_schema()["properties"]
    validator = Draft202012Validator(root[group]["items"]["properties"][key])
    for index in range(1, limit + 1):
        validator.validate(f"{prefix}{index:03d}")
    for value in (f"{prefix}000", f"{prefix}{limit + 1:03d}", f"{prefix}001\n", "f500", "sha256:" + "a" * 64):
        with pytest.raises(ValidationError):
            validator.validate(value)


def test_full_original_capacity_and_many_relevant_references_are_not_globally_capped() -> None:
    schema = vlm_bounded_video_response_schema()
    wire = _bounded_wire()
    groups = (("entities", "local_entity_id", "p", 24), ("facts", "local_fact_id", "f", 48),
              ("events", "local_event_id", "e", 24), ("candidate_hypotheses", "local_candidate_id", "c", 8))
    for group, key, prefix, limit in groups:
        assert schema["properties"][group]["maxItems"] == limit
        prototype = wire[group][0]
        wire[group] = [{**copy.deepcopy(prototype), key: f"{prefix}{index:03d}"} for index in range(1, limit + 1)]
    # All entity kinds share the same p namespace, not only people.
    for index, kind in enumerate(("person", "object", "location", "screen_text_source")):
        wire["entities"][index]["entity_kind"] = kind
    fact_refs = [item["local_fact_id"] for item in wire["facts"]]
    event_refs = [item["local_event_id"] for item in wire["events"]]
    for event in wire["events"]:
        event["fact_refs"] = fact_refs.copy()
        event["participant_refs"] = [item["local_entity_id"] for item in wire["entities"]]
    for candidate in wire["candidate_hypotheses"]:
        candidate["supporting_event_refs"] = event_refs.copy()
        candidate["payoff_event_refs"] = event_refs.copy()
        candidate["measurements"][0]["fact_refs"] = fact_refs.copy()
        candidate["measurements"][0]["event_refs"] = event_refs.copy()
    Draft202012Validator(schema).validate(wire)


@pytest.mark.parametrize("group,field,prefix,limit", [
    ("window_summary", "fact_refs", "f", 48), ("window_summary", "event_refs", "e", 24),
    ("continuity", "entry_state_fact_refs", "f", 48), ("continuity", "exit_state_fact_refs", "f", 48),
    ("events", "fact_refs", "f", 48), ("events", "participant_refs", "p", 24),
    ("events", "cause_event_refs", "e", 24), ("events", "effect_event_refs", "e", 24),
    ("candidate_hypotheses", "context_event_refs", "e", 24),
    ("candidate_hypotheses", "supporting_event_refs", "e", 24),
    ("candidate_hypotheses", "payoff_event_refs", "e", 24),
    ("measurements", "fact_refs", "f", 48), ("measurements", "event_refs", "e", 24),
])
def test_every_reference_array_is_bounded_to_its_actual_kind(group: str, field: str, prefix: str, limit: int) -> None:
    root = vlm_bounded_video_response_schema()["properties"]
    if group == "measurements":
        owner = root["candidate_hypotheses"]["items"]["properties"]["measurements"]["items"]
    elif group in ("window_summary", "continuity"):
        owner = root[group]
    else:
        owner = root[group]["items"]
    reference = owner["properties"][field]
    assert reference["maxItems"] == limit
    validator = Draft202012Validator(reference)
    validator.validate([f"{prefix}{index:03d}" for index in range(1, limit + 1)])
    wrong_prefix = "e" if prefix != "e" else "f"
    for values in ([f"{prefix}{limit + 1:03d}"], [f"{wrong_prefix}001"], [f"{prefix}001"] * 2):
        with pytest.raises(ValidationError):
            validator.validate(values)


@pytest.mark.parametrize("group,field,prefix,limit,nullable", [
    ("facts", "subject_ref", "p", 24, False),
    ("facts", "object_ref", "p", 24, True),
    ("candidate_hypotheses", "anchor_event_ref", "e", 24, False),
])
def test_scalar_references_have_the_correct_kind_and_nullability(group: str, field: str, prefix: str, limit: int, nullable: bool) -> None:
    root = vlm_bounded_video_response_schema()["properties"]
    validator = Draft202012Validator(root[group]["items"]["properties"][field])
    validator.validate(f"{prefix}{limit:03d}")
    for value in ("f001", f"{prefix}{limit + 1:03d}"):
        with pytest.raises(ValidationError):
            validator.validate(value)
    assert validator.is_valid(None) is nullable


def test_conditional_cardinality_rules_are_preserved_without_array_rewriting() -> None:
    old = vlm_video_response_schema()["properties"]["candidate_hypotheses"]["items"]
    new = vlm_bounded_video_response_schema()["properties"]["candidate_hypotheses"]["items"]
    assert new["allOf"] == old["allOf"]
    assert new["properties"]["measurements"]["items"]["anyOf"] == old["properties"]["measurements"]["items"]["anyOf"]
    validator = Draft202012Validator(vlm_bounded_video_response_schema())
    wire = _bounded_wire()
    candidate = wire["candidate_hypotheses"][0]
    candidate["payoff_event_refs"] = []
    assert not validator.is_valid(wire)  # highlight still requires a payoff
    candidate.update(candidate_kind="hook", open_question="Who is there?")
    validator.validate(wire)
    candidate["payoff_event_refs"] = ["e001"]
    assert not validator.is_valid(wire)  # hook still forbids a payoff
    candidate["payoff_event_refs"] = []
    candidate["measurements"][0].update(fact_refs=[], event_refs=[])
    assert not validator.is_valid(wire)  # measurement still needs at least one reference


@pytest.mark.parametrize("group", ["entities", "facts", "events", "candidate_hypotheses", "temporal_segments"])
def test_all_support_sites_are_video_only_including_continuity(group: str) -> None:
    root = vlm_bounded_video_response_schema()["properties"]
    old_root = vlm_video_response_schema()["properties"]
    if group == "temporal_segments":
        root = root["continuity"]["properties"]
        old_root = old_root["continuity"]["properties"]
    support_schema = root[group]["items"]["properties"]["support"]
    old_support_schema = old_root[group]["items"]["properties"]["support"]
    support = _wire()["facts"][0]["support"]
    Draft202012Validator(support_schema).validate(support)
    anchored = {**support, "support_kind": "frame_anchored_observation", "frame_refs": ["f0001"]}
    Draft202012Validator(old_support_schema).validate(anchored)
    for invalid in (anchored, {**support, "frame_refs": ["f0001"]}, {**support, "frames": []}):
        with pytest.raises(ValidationError):
            Draft202012Validator(support_schema).validate(invalid)


def test_schema_admission_does_not_claim_reference_closure_or_repair_it() -> None:
    wire = _bounded_wire()
    wire["events"][0]["fact_refs"] = ["f048"]  # in range, but not declared
    before = copy.deepcopy(wire)
    Draft202012Validator(vlm_bounded_video_response_schema()).validate(wire)
    with pytest.raises(VlmResponseRejected):
        _parse(wire)
    assert wire == before


def test_ordering_accepts_only_exact_registered_shape_and_returns_fresh_objects() -> None:
    canonical = json.loads(vlm_bounded_video_response_schema_json())
    ordered = ordered_bounded_video_schema(canonical)
    assert tuple(ordered["properties"]) == VLM_VIDEO_FIELD_ORDER
    assert tuple(ordered["required"]) == VLM_VIDEO_FIELD_ORDER
    assert json.dumps(ordered, sort_keys=True) == json.dumps(canonical, sort_keys=True)
    ordered["properties"].clear()
    assert tuple(ordered_bounded_video_schema(canonical)["properties"]) == VLM_VIDEO_FIELD_ORDER
    assert vlm_bounded_video_response_schema()["properties"]
    assert canonical["properties"]


@pytest.mark.parametrize("mutation", ["boolean_as_integer", "integer_as_float", "unknown_field", "old_schema", "not_object"])
def test_ordering_rejects_schema_drift_including_python_equal_json_types(mutation: str) -> None:
    schema: Any = json.loads(vlm_bounded_video_response_schema_json())
    original = copy.deepcopy(schema)
    if mutation == "boolean_as_integer":
        schema["additionalProperties"] = 0
        assert schema == original  # Python equality is not a JSON type/identity check.
    elif mutation == "integer_as_float":
        schema["properties"]["schema_version"]["const"] = 4.0
        assert schema == original
    elif mutation == "unknown_field":
        schema["description"] = "not registered"
    elif mutation == "old_schema":
        schema = vlm_video_response_schema()
    else:
        schema = []
    with pytest.raises(ValueError, match="exactly match the registered schema"):
        ordered_bounded_video_schema(schema)


@pytest.mark.parametrize("time_base,duration", [(TimeBase(1, 12_800), 1_281), (TimeBase(1001, 30_000), 100), (TimeBase(1, 1000), 2**53 + 1)])
def test_nonzero_origin_and_fractional_duration_use_exact_playback_floor(time_base: TimeBase, duration: int) -> None:
    manifest, _ = _context(time_base=time_base, proxy_start=987_654, duration=duration)
    exact = Fraction(duration * time_base.numerator * 1000, time_base.denominator)
    expected = {"duration_ms_floor": exact.numerator // exact.denominator}
    new_context = json.loads(build_vlm_bounded_video_prompt(manifest)[len(VLM_BOUNDED_VIDEO_PROMPT_TEMPLATE):])
    old_context = json.loads(build_vlm_video_prompt(manifest)[len(VLM_VIDEO_PROMPT_TEMPLATE):])
    assert new_context == expected
    assert new_context["duration_ms_floor"] == old_context["duration_ms_floor"]
    assert type(new_context["duration_ms_floor"]) is int


def test_submillisecond_window_and_nonmanifest_input_are_rejected() -> None:
    manifest, _ = _context(time_base=TimeBase(1, 1_000_000), duration=100)
    with pytest.raises(ValueError, match="at least one millisecond"):
        build_vlm_bounded_video_prompt(manifest)
    with pytest.raises(TypeError, match="exact WindowManifest"):
        build_vlm_bounded_video_prompt(object())  # type: ignore[arg-type]


def test_prompt_retains_semantic_obligations_without_repeating_all_enums() -> None:
    for instruction in (
        "不为压缩引用而省略独立事实", "包括人物、物体、地点、文字来源",
        "事件区间须与每个引用事实区间相交", "区间都须与候选相交", "context事件是解释背景",
        "当且仅当", "starts_mid_event=continues_from_previous=bool(entry_state_fact_refs)",
        "ends_mid_event=continues_into_next=bool(exit_state_fact_refs)", "保留全部必填字段",
        "模型自报", "不是经校准误差", "不可机械填同一值", "只依据窗口内未闭合状态",
        "不预知未输入的前后集内容", "reason说明选择理由", "anchor_summary复用核心事件摘要",
        "hook的payoff_or_open_question复用open_question",
        "结构符合Schema仍须通过语义校验",
    ):
        assert instruction in VLM_BOUNDED_VIDEO_PROMPT_TEMPLATE
    assert "visible_presence/visible_state" not in VLM_BOUNDED_VIDEO_PROMPT_TEMPLATE
    assert "hook_strength/reveal_strength" not in VLM_BOUNDED_VIDEO_PROMPT_TEMPLATE
