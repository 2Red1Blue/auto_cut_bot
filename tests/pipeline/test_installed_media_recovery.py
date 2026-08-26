"""Resumed media adapters must not bypass installed policy or accepted anchors."""

from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID

import pytest
from autocut_kernel.registry.installed_runtime import InstalledLocalRunProfileResolver
from autocut_kernel.registry.timed_speech import StoreAnchoredTimedSpeechProfileResolver
from autocut_kernel.store import CommandOutcome

from auto_cut_bot.pipeline.media_preflight.installed_policy import InstalledMediaPolicyError
from auto_cut_bot.pipeline.runtime import media_preflight_stage
from auto_cut_bot.pipeline.runtime.media_preflight_stage import MediaPreflightPipelineStage
from tests.pipeline.installed_profile_fixture import (
    synthetic_installed_resource,
    synthetic_media_policy,
)
from tests.pipeline.test_pipeline_vlm_stage import _context


def _matching_context():
    context = _context("media_preflight")
    old = context.execution_profile
    resource = synthetic_installed_resource(old.to_doubao_policy(), old.to_generation_retry_policy())
    policy = synthetic_media_policy(resource)
    profile = type(old).from_policies(
        old.to_doubao_policy(), policy, retry_policy=old.to_generation_retry_policy(),
        materialization_limits=replace(old.to_materialization_limits(),
            timed_speech_max_request_bytes=resource.local_run.native_timed_speech.max_request_bytes),
        stage1_policy=resource.narrative.command_policy,
    )
    return replace(context, execution_profile=profile), resource, policy


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ("execute", "reconcile"))
async def test_resumed_media_mismatch_stops_before_store_or_native(operation):
    context, resource, policy = _matching_context()
    old = context.execution_profile
    changed = type(old).from_policies(
        old.to_doubao_policy(), replace(policy, timed_speech_calibration_sha256="sha256:" + "9" * 64),
        retry_policy=old.to_generation_retry_policy(), materialization_limits=old.to_materialization_limits(),
        stage1_policy=old.build_stage1_command_policy(),
    )
    context = replace(context, execution_profile=changed)

    class NoStore:
        def read_outcome(self, *args):
            pytest.fail("mismatched media profile reached Store")

    class NoNative:
        def prepare(self, *args, **kwargs):
            pytest.fail("mismatched media profile reached native inference")

    stage = MediaPreflightPipelineStage(NoStore(), NoNative(), InstalledLocalRunProfileResolver(resource))
    before = context.execution_profile.canonical_json
    with pytest.raises(InstalledMediaPolicyError, match="aggregate calibration"):
        await getattr(stage, operation)(context)
    assert context.execution_profile.canonical_json == before


@pytest.mark.asyncio
async def test_media_batch_requires_accepted_installed_anchor_before_kernel_command(monkeypatch):
    context, resource, policy = _matching_context()
    events = []

    class MissingCalibrationStore:
        def read_calibration_record_anchor(self, *args, **kwargs):
            events.append("calibration")
            raise ValueError("synthetic missing calibration")

    def forbidden(*args, **kwargs):
        pytest.fail("Kernel command constructed before installed anchor verification")

    monkeypatch.setattr(media_preflight_stage, "PrepareTimedMediaEvidenceCommand", forbidden)
    stage = MediaPreflightPipelineStage(MissingCalibrationStore(), object(),
                                        InstalledLocalRunProfileResolver(resource))
    with pytest.raises(ValueError, match="missing calibration"):
        await stage._execute_batch(context, object(), (), policy)
    assert events == ["calibration"]


@pytest.mark.asyncio
async def test_media_batch_delegates_same_snapshot_only_after_installed_resolve(monkeypatch):
    """Ordering test; patched resolver is explicitly not calibration acceptance."""
    context, resource, policy = _matching_context()
    resolver = InstalledLocalRunProfileResolver(resource)
    store = object()
    events = []
    request = object()
    expected_result = SimpleNamespace(outcome=CommandOutcome(UUID(int=40), "pending"))

    def fake_resolve(self, actual_store):
        assert self is resolver and actual_store is store
        events.append("full-installed-resolve")

    class CapturingCommand:
        def __init__(self, actual_store, producer, actual_resolver):
            assert actual_store is store
            assert type(actual_resolver) is StoreAnchoredTimedSpeechProfileResolver
            assert actual_resolver.snapshot == resolver.snapshot
            assert events == ["full-installed-resolve"]
            events.append("kernel-command")

        def execute(self, actual_request):
            assert actual_request is request
            events.append("kernel-execute")
            return expected_result

    monkeypatch.setattr(InstalledLocalRunProfileResolver, "resolve", fake_resolve)
    monkeypatch.setattr(media_preflight_stage, "PrepareTimedMediaEvidenceCommand", CapturingCommand)
    stage = MediaPreflightPipelineStage(store, object(), resolver)
    assert await stage._execute_batch(context, object(), (request,), policy) is expected_result
    assert events == ["full-installed-resolve", "kernel-command", "kernel-execute"]
