"""Synthetic policy/Store checks; never actual model or calibration acceptance."""

from __future__ import annotations

from dataclasses import replace

import pytest
from autocut_kernel.media import PTSIndex
from autocut_kernel.media.types import canonical_sha256
from autocut_kernel.vlm import ProxyTimelineMap, WindowFrameSample

from auto_cut_bot.pipeline.media_preflight.installed_policy import InstalledMediaPolicyError
from auto_cut_bot.pipeline.runtime import vlm_stage
from auto_cut_bot.pipeline.runtime.vlm_stage import VlmPipelineStage
from auto_cut_bot.pipeline.source_prep.command import (
    identity_window_sample_indices,
    identity_window_sampling_policy,
)
from auto_cut_bot.pipeline.vlm.policy_binding import (
    InstalledVlmPolicyError,
    validate_installed_source_sampling,
    validate_installed_vlm_policy,
)
from tests.pipeline.installed_profile_fixture import (
    synthetic_installed_resource,
    synthetic_media_policy,
)
from tests.pipeline.test_pipeline_vlm_stage import (
    KernelStore,
    Provider,
    _bundle,
    _context,
    _source_success,
)


def test_actual_policy_matches_its_synthetic_installed_source_without_stage1_claim():
    context = _context()
    policy = context.execution_profile.to_doubao_policy()
    retry = context.execution_profile.to_generation_retry_policy()
    resource = synthetic_installed_resource(policy, retry)
    assert validate_installed_vlm_policy(resource.narrative, policy, retry) is None


@pytest.mark.parametrize("field", (
    "prompt_template_sha256", "response_schema_sha256", "parser_contract_sha256",
    "request_parameters_sha256", "parse_policy_sha256", "retry_policy_sha256",
    "window_sampling_policy_sha256",
))
def test_each_executed_digest_is_compared(field):
    context = _context()
    policy = context.execution_profile.to_doubao_policy()
    retry = context.execution_profile.to_generation_retry_policy()
    resource = synthetic_installed_resource(policy, retry)
    narrative = resource.narrative
    changed = replace(narrative, reference=replace(narrative.reference, **{field: "sha256:" + "9" * 64}))
    with pytest.raises(InstalledVlmPolicyError, match=field):
        validate_installed_vlm_policy(changed, policy, retry)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ("execute", "reconcile"))
@pytest.mark.parametrize("mutation", ("model", "parameters", "parse", "retry", "calibration"))
async def test_resumed_policy_mismatch_blocks_before_store_or_provider(operation, mutation):
    context = _context()
    original = context.execution_profile
    policy = original.to_doubao_policy()
    retry = original.to_generation_retry_policy()
    resource = synthetic_installed_resource(policy, retry)
    media_policy = synthetic_media_policy(resource)
    if mutation == "model":
        policy = replace(policy, model_id="different-doubao-deployment")
    elif mutation == "parameters":
        policy = replace(policy, max_output_tokens=1024)
    elif mutation == "parse":
        policy = replace(policy, parse_policy=replace(policy.parse_policy, max_entities=7))
    elif mutation == "retry":
        retry = replace(retry, max_attempts=2, backoff_seconds=(2,))
    else:
        # An old accepted aggregate also binds its old shadow/narrative bytes.
        media_policy = replace(media_policy, timed_speech_calibration_sha256="sha256:" + "9" * 64)
    changed = type(original).from_policies(
        policy, media_policy, retry_policy=retry,
        materialization_limits=original.to_materialization_limits(),
        stage1_policy=resource.narrative.command_policy,
    )
    context = replace(context, execution_profile=changed)

    class NoStoreAccess:
        def read_outcome(self, *args):
            pytest.fail("mismatched resumed policy reached Store")

    provider = Provider({})
    stage = VlmPipelineStage(NoStoreAccess(), provider, installed_profile=resource)
    before = context.execution_profile.canonical_json
    expected_error = InstalledMediaPolicyError if mutation == "calibration" else InstalledVlmPolicyError
    with pytest.raises(expected_error):
        await getattr(stage, operation)(context)
    assert context.execution_profile.canonical_json == before
    assert provider.dispatch_calls == provider.reconcile_calls == []


def _identity_bundle():
    bundle, blobs = _bundle()
    episode = bundle.prepared.episodes[0]
    original = episode.manifest
    ticks = original.frame_pts_index_set.pts_index.ticks[:-1]
    index = replace(original.frame_pts_index_set, pts_index=PTSIndex(ticks),
                    pts_index_sha256=canonical_sha256(list(ticks)))
    selected = identity_window_sample_indices(len(ticks))
    manifest = replace(
        original, frame_pts_index_set=index,
        window_sampling_policy_sha256=canonical_sha256({
            **identity_window_sampling_policy(), "selected_indices": list(selected),
        }),
        timeline_map=ProxyTimelineMap.translation(
            time_base=original.source_time_base, proxy_range=original.source_range,
            source_start_pts=original.source_range.start_pts,
        ),
        frame_samples=tuple(WindowFrameSample(ticks[i], ticks[i], "sha256:" + "d" * 64) for i in selected),
    )
    return _replace_episode(bundle, replace(episode, manifest=manifest,
        manifest_set=replace(episode.manifest_set, manifests=(manifest,)))), blobs


def _replace_episode(bundle, episode):
    prepared = replace(bundle.prepared, episodes=(episode,))
    return replace(bundle, prepared=prepared, artifact_reference=replace(
        bundle.artifact_reference, content_hash=canonical_sha256(prepared.to_mapping()),
    ))


def test_matching_persisted_policy_and_identity_samples_build_requests(monkeypatch):
    context = _context()
    bundle, blobs = _identity_bundle()
    resource = synthetic_installed_resource(
        context.execution_profile.to_doubao_policy(),
        context.execution_profile.to_generation_retry_policy(),
    )
    original = context.execution_profile
    context = replace(context, execution_profile=type(original).from_policies(
        original.to_doubao_policy(), synthetic_media_policy(resource),
        retry_policy=original.to_generation_retry_policy(),
        materialization_limits=replace(original.to_materialization_limits(),
            timed_speech_max_request_bytes=resource.local_run.native_timed_speech.max_request_bytes),
        stage1_policy=resource.narrative.command_policy,
    ))
    monkeypatch.setattr(vlm_stage, "read_persisted_prepared_sources_bundle", lambda *a, **k: bundle)
    provider = Provider({})
    stage = VlmPipelineStage(KernelStore(_source_success(), blobs), provider,
                             installed_profile=resource)
    prepared = stage._requests(context)
    assert prepared is not None and len(prepared[1]) == 1
    assert prepared[1][0].manifest is bundle.prepared.episodes[0].manifest
    assert provider.dispatch_calls == provider.reconcile_calls == []


@pytest.mark.parametrize("mutation", ("policy", "actual_samples"))
def test_persisted_dynamic_policy_and_actual_samples_both_must_match(mutation):
    bundle, _ = _identity_bundle()
    assert validate_installed_source_sampling(bundle) is None
    episode = bundle.prepared.episodes[0]
    if mutation == "policy":
        manifest = replace(episode.manifest, window_sampling_policy_sha256="sha256:" + "c" * 64)
    else:
        manifest = replace(episode.manifest, frame_samples=episode.manifest.frame_samples[:-1])
    changed = _replace_episode(bundle, replace(episode, manifest=manifest,
        manifest_set=replace(episode.manifest_set, manifests=(manifest,))))
    with pytest.raises(InstalledVlmPolicyError, match="sampling differs"):
        validate_installed_source_sampling(changed)
