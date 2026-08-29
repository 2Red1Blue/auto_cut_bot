"""Compact prompting is explicit; existing request and profile bytes stay frozen."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest
from autocut_kernel.media import TimeBase
from autocut_kernel.pipeline import GenerateVlmEvidenceRequest
from autocut_kernel.vlm.parser_contract import vlm_parser_contract_sha256

from auto_cut_bot.pipeline.runtime import composition, vlm_stage
from auto_cut_bot.pipeline.runtime.models import PipelineExecutionProfile, PipelineStageContext
from auto_cut_bot.pipeline.runtime.semantic_authority import (
    SemanticRunAuthorityError,
    decode_semantic_run_authority,
    load_installed_semantic_run_authority,
)
from auto_cut_bot.pipeline.source_prep.command import PersistedPreparedSources
from auto_cut_bot.pipeline.vlm.contextual_video_prompt import (
    VLM_CONTEXTUAL_FACT_ANCHORED_EVENT_CORE_VIDEO_PROMPT_VERSION,
)
from auto_cut_bot.pipeline.vlm.doubao_ark_provider import DoubaoArkVlmProviderConfig
from auto_cut_bot.pipeline.vlm.prompt import (
    VLM_COMPACT_PROMPT_TEMPLATE,
    VLM_COMPACT_PROMPT_VERSION,
    VLM_PROMPT_TEMPLATE,
    VLM_PROMPT_VERSION,
    build_vlm_prompt,
    resolve_vlm_prompt_template,
    vlm_prompt_template_sha256,
)
from auto_cut_bot.pipeline.vlm.request_factory import (
    DoubaoVlmRequestPolicy,
    build_doubao_vlm_request,
)
from auto_cut_bot.pipeline.vlm.reuse import derive_vlm_reuse_identity
from tests.pipeline import test_doubao_vlm_request_factory as factory_fixture
from tests.pipeline.test_pipeline_runtime_composition import _environment
from tests.pipeline.test_pipeline_vlm_stage import (
    KernelStore,
    Provider,
    _bundle,
    _context,
    _source_success,
)

_V3_TEMPLATE_HASH = "sha256:ba81ba735fb3033154e534044f126d5f28b4f03ae37f9192a218e154d5751218"
_V3_RENDERED_HASH = "sha256:049e4f8c84815ee4fd8e9ccda8bb8013c4f32bb539f3deaeac200f889313e228"
_V3_REQUEST_HASH = "sha256:0e9bcd9c14ef0ac9f7507d959e1419f8fc7c6594be0da4496ae2c88998c8f665"
_V3_PAYLOAD_HASH = "sha256:e1d6e7ef12e81a40d0242ab57521852fd203fc0a8ac69462bf23ec976ba2010a"
_V3_PROFILE_HASH = "sha256:a252afd67daede1b9bc90eb1707a67c857635f7b76eff196afd87e360c07086e"


def _sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _request(
    bundle: PersistedPreparedSources, policy: DoubaoVlmRequestPolicy,
) -> GenerateVlmEvidenceRequest:
    return build_doubao_vlm_request(
        source_bundle=bundle, episode_index=0, job=bundle.source_job,
        artifact_revision=1, idempotency_key="compact-regression", policy=policy,
        retry_policy=factory_fixture._retry_policy(),
    )


def test_legacy_template_and_rendered_prompt_bytes_are_unchanged() -> None:
    manifest = factory_fixture._prepared_episode().manifest
    rendered = build_vlm_prompt(manifest)
    assert VLM_PROMPT_VERSION == "vlm-semantic-pack-v3"
    assert resolve_vlm_prompt_template(VLM_PROMPT_VERSION) == VLM_PROMPT_TEMPLATE
    assert vlm_prompt_template_sha256() == _V3_TEMPLATE_HASH
    assert _sha256(rendered) == _V3_RENDERED_HASH
    assert rendered == build_vlm_prompt(manifest, prompt_version=VLM_PROMPT_VERSION)
    assert "proxy_time_base" not in rendered


def test_legacy_factory_request_hash_and_payload_remain_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(factory_fixture, "uuid4", lambda: UUID("00000000-0000-0000-0000-000000000001"))
    request = _request(factory_fixture._source_bundle(), factory_fixture._policy())
    assert request.prompt_version == VLM_PROMPT_VERSION
    assert request.request_hash == _V3_REQUEST_HASH
    assert request.request_identity.request_payload_sha256 == _V3_PAYLOAD_HASH


def test_compact_prompt_uses_actual_proxy_clock_not_source_clock() -> None:
    manifest = factory_fixture._prepared_episode().manifest
    manifest = replace(
        manifest,
        timeline_map=replace(
            manifest.timeline_map, proxy_time_base=TimeBase(1, 24000),
            certificate_kind="piecewise_monotonic",
        ),
    )
    compact = build_vlm_prompt(manifest, prompt_version=VLM_COMPACT_PROMPT_VERSION)
    template = resolve_vlm_prompt_template(VLM_COMPACT_PROMPT_VERSION)
    assert compact.startswith(template)
    context = json.loads(compact[len(template):])
    assert context["proxy_time_base"] == {"numerator": 1, "denominator": 24000}
    assert manifest.source_time_base == TimeBase(1, 1000)
    assert context["proxy_range"] == {"start_pts": 0, "end_pts_exclusive": 100}
    assert context["allowed_frame_anchors"] == [
        {"frame_id": sample.frame_id, "proxy_pts": sample.proxy_pts}
        for sample in manifest.frame_samples
    ]
    legacy_context = json.loads(build_vlm_prompt(manifest)[len(VLM_PROMPT_TEMPLATE):])
    assert {key: value for key, value in context.items() if key != "proxy_time_base"} == legacy_context
    assert vlm_prompt_template_sha256(VLM_COMPACT_PROMPT_VERSION) == _sha256(template)
    assert vlm_prompt_template_sha256(VLM_COMPACT_PROMPT_VERSION) != _V3_TEMPLATE_HASH


def test_compact_instructions_preserve_complete_semantics_and_exact_evidence() -> None:
    assert VLM_COMPACT_PROMPT_VERSION == "vlm-semantic-pack-v4-compact"
    assert "单行紧凑 JSON" in VLM_COMPACT_PROMPT_TEMPLATE
    assert "不得省略" in VLM_COMPACT_PROMPT_TEMPLATE
    assert "直接观察" in VLM_COMPACT_PROMPT_TEMPLATE
    assert "独立 OCR" in VLM_COMPACT_PROMPT_TEMPLATE
    assert "screen_text" in VLM_COMPACT_PROMPT_TEMPLATE
    assert "完整的 sha256:" in VLM_COMPACT_PROMPT_TEMPLATE
    assert "最小充分" in VLM_COMPACT_PROMPT_TEMPLATE
    assert "proxy_time_base" in VLM_COMPACT_PROMPT_TEMPLATE


@pytest.mark.parametrize("version", ["", "unknown", "vlm-semantic-pack-v4", None, True, {}])
def test_unregistered_prompt_versions_fail_closed(version: object) -> None:
    with pytest.raises(ValueError, match="prompt version"):
        resolve_vlm_prompt_template(version)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="prompt version"):
        factory_fixture._policy(prompt_version=version)


def test_compact_factory_changes_only_prompt_identity_and_preserves_budgets() -> None:
    bundle = factory_fixture._source_bundle()
    legacy_policy = factory_fixture._policy()
    compact_policy = replace(legacy_policy, prompt_version=VLM_COMPACT_PROMPT_VERSION)
    legacy, compact = _request(bundle, legacy_policy), _request(bundle, compact_policy)
    assert compact.prompt_template == build_vlm_prompt(bundle.prepared.episodes[0].manifest, prompt_version=VLM_COMPACT_PROMPT_VERSION)
    assert compact.request_hash != legacy.request_hash
    assert compact.response_schema_json == legacy.response_schema_json
    assert compact.request_parameters_json == legacy.request_parameters_json
    assert compact.parse_policy == legacy.parse_policy
    assert compact.retry_policy == legacy.retry_policy
    assert compact.model_id == legacy.model_id
    assert compact.manifest == legacy.manifest
    assert "thinking" not in compact_policy.request_parameters
    assert compact_policy.max_output_tokens == legacy_policy.max_output_tokens == 32768


def test_installed_semantic_authority_explicitly_selects_video_without_changing_default() -> None:
    authority = load_installed_semantic_run_authority()
    assert authority.vlm_policy.prompt_version == VLM_CONTEXTUAL_FACT_ANCHORED_EVENT_CORE_VIDEO_PROMPT_VERSION
    assert factory_fixture._policy().prompt_version == VLM_PROMPT_VERSION
    default_policy = factory_fixture._policy()
    assert authority.vlm_policy.thinking_type == "disabled"
    assert replace(
        authority.vlm_policy, prompt_version=VLM_PROMPT_VERSION,
        adapter_strategy_version=default_policy.adapter_strategy_version, thinking_type=None,
        parser_strategy_version=default_policy.parser_strategy_version,
        parser_contract_sha256=None, response_schema_json=default_policy.response_schema_json,
        stage_strategy_version=default_policy.stage_strategy_version,
    ) == default_policy


def test_semantic_authority_checks_the_selected_template_hash() -> None:
    resource = Path(composition.__file__).parent / "_authority" / "semantic-run.json"
    document = json.loads(resource.read_text(encoding="utf-8"))
    document["vlm"]["prompt_template_sha256"] = _V3_TEMPLATE_HASH
    raw = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    with pytest.raises(SemanticRunAuthorityError, match="implementation binding"):
        decode_semantic_run_authority(raw, expected_sha256="sha256:" + hashlib.sha256(raw).hexdigest())
    document["vlm"]["prompt_version"] = VLM_PROMPT_VERSION
    legacy_policy = factory_fixture._policy()
    document["vlm"].update(
        parser_strategy_version=legacy_policy.parser_strategy_version,
        parser_contract_sha256=vlm_parser_contract_sha256(),
        response_schema_sha256=_sha256(legacy_policy.response_schema_json),
        stage_strategy_version=legacy_policy.stage_strategy_version,
    )
    legacy_raw = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    legacy = decode_semantic_run_authority(legacy_raw, expected_sha256="sha256:" + hashlib.sha256(legacy_raw).hexdigest())
    assert legacy.vlm_policy.prompt_version == VLM_PROMPT_VERSION


def test_semantic_composition_preserves_installed_prompt_choice(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    environment[composition.PIPELINE_PLAN_ENV] = composition.SEMANTIC_ONLY_PLAN
    runtime = composition.compose_pipeline_runtime_from_environment(environment)
    assert runtime is not None
    assert runtime.execution_profile.to_doubao_policy() == load_installed_semantic_run_authority().vlm_policy
    assert runtime.execution_profile.prompt_version == VLM_CONTEXTUAL_FACT_ANCHORED_EVENT_CORE_VIDEO_PROMPT_VERSION


@pytest.mark.parametrize("version", [VLM_PROMPT_VERSION, VLM_COMPACT_PROMPT_VERSION])
def test_frozen_profile_roundtrip_preserves_its_explicit_prompt(version: str) -> None:
    policy = factory_fixture._policy(prompt_version=version)
    profile = PipelineExecutionProfile.from_semantic_policies(policy, retry_policy=factory_fixture._retry_policy())
    rebuilt = PipelineExecutionProfile.from_mapping(json.loads(profile.canonical_json))
    assert rebuilt.canonical_json == profile.canonical_json
    assert rebuilt.to_doubao_policy() == policy
    if version == VLM_PROMPT_VERSION:
        assert profile.canonical_hash == _V3_PROFILE_HASH
    bundle = factory_fixture._source_bundle()
    assert _request(bundle, policy) == _request(bundle, rebuilt.to_doubao_policy())


@pytest.mark.parametrize("version", [VLM_PROMPT_VERSION, VLM_COMPACT_PROMPT_VERSION])
def test_reuse_projection_selects_the_original_requests_template(version: str) -> None:
    bundle = factory_fixture._source_bundle()
    request = _request(bundle, factory_fixture._policy(prompt_version=version))
    original_hash = request.request_hash
    identity = derive_vlm_reuse_identity(
        request, source_bundle=bundle,
        provider_scope=DoubaoArkVlmProviderConfig("unused-fixture", "tenant", "project"),
    )
    assert identity.semantic_policy.prompt_template == resolve_vlm_prompt_template(version)
    assert identity.semantic_policy.prompt_version == version
    assert identity.origin_request_payload == request.request_payload
    assert request.request_hash == original_hash


@pytest.mark.asyncio
async def test_new_semantic_authority_does_not_relabel_a_v3_reconcile_context(monkeypatch: pytest.MonkeyPatch) -> None:
    assert load_installed_semantic_run_authority().vlm_policy.prompt_version == VLM_CONTEXTUAL_FACT_ANCHORED_EVENT_CORE_VIDEO_PROMPT_VERSION
    bundle, blobs = _bundle()
    monkeypatch.setattr(vlm_stage, "read_persisted_prepared_sources_bundle", lambda *_args, **_kwargs: bundle)
    legacy_policy = factory_fixture._policy()
    profile = PipelineExecutionProfile.from_semantic_policies(legacy_policy, retry_policy=factory_fixture._retry_policy())
    context = replace(_context(status="indeterminate"), execution_profile=profile)
    provider = Provider({})
    stage = vlm_stage.VlmPipelineStage(KernelStore(_source_success(), blobs), provider)
    captured: list[GenerateVlmEvidenceRequest] = []

    async def inspect_without_dispatch(
        _context: PipelineStageContext, _bundle: PersistedPreparedSources,
        policy: DoubaoVlmRequestPolicy, requests: tuple[GenerateVlmEvidenceRequest, ...],
        _context_packs: object = None,
    ) -> None:
        assert policy == legacy_policy
        captured.extend(requests)

    monkeypatch.setattr(stage, "_execute_batch", inspect_without_dispatch)
    assert await stage.reconcile(context) is None
    assert captured and all(request.prompt_version == VLM_PROMPT_VERSION for request in captured)
    assert all(request.prompt_template == build_vlm_prompt(request.manifest) for request in captured)
    assert context.execution_profile.canonical_hash == _V3_PROFILE_HASH
    assert provider.dispatch_calls == provider.reconcile_calls == []
