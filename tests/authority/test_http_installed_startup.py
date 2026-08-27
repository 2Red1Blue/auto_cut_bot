"""HTTP lifecycle with a synthetic resource and explicit read-only fake Store."""

import pytest
from autocut_kernel.registry.installed_runtime import InstalledLocalRunProfileResolver
from autocut_kernel.store.models import MaterializationLimits

from auto_cut_bot.pipeline.runtime.composition import (
    DOUBAO_GENERATION_RETRY_POLICY,
    PipelineRuntime,
)
from auto_cut_bot.pipeline.runtime.models import EvidenceReadLimits, PipelineExecutionProfile
from auto_cut_bot.pipeline.vlm import DoubaoVlmRequestPolicy
from tests.authority.test_installed_runtime import (
    FakeInstalledReadStore,
    _bootstrapped,
    installed_fixture,
)
from tests.pipeline.runtime_profile_fixture import media_preflight_policy

# Import the explicitly synthetic resource fixture into this lifecycle suite.
assert installed_fixture is not None


def _profile(resource):
    return PipelineExecutionProfile.from_policies(
        DoubaoVlmRequestPolicy(model_id="doubao-seed-2-1-pro-260628"),
        media_preflight_policy(), retry_policy=DOUBAO_GENERATION_RETRY_POLICY,
        materialization_limits=MaterializationLimits(
            max_source_bytes=8 * 1024 * 1024, timed_speech_max_request_bytes=1024 * 1024,
            copy_chunk_bytes=64 * 1024, staging_quota_bytes=16 * 1024 * 1024,
        ),
        evidence_read_limits=EvidenceReadLimits(100_000, 500_000),
        stage1_policy=resource.narrative.command_policy,
        stage2_policy=resource.local_run.stage2_command_policy,
        stage3_policy=resource.local_run.stage3_command_policy,
    )


class FakeRecoveryWorker:
    def __init__(self, events):
        self.events = events

    async def startup_reconstruct(self):
        self.events.append("worker-recovery")
        return ("reconstructed-fixture",)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ("calibration-unavailable", "profile-unavailable"))
async def test_missing_dynamic_calibration_does_not_prevent_http_worker_recovery(installed_fixture, failure):
    resource, anchor = installed_fixture
    profile = _bootstrapped(resource)
    options = {}
    if failure == "calibration-unavailable":
        options["calibration_error"] = ValueError("fake missing calibration")
    else:
        options["profile_error"] = ValueError("fake missing profile")
    store = FakeInstalledReadStore(anchor, profile, **options)
    runtime = PipelineRuntime(object(), FakeRecoveryWorker(store.events), _profile(resource),
                             InstalledLocalRunProfileResolver(resource), store)
    assert await runtime.startup_reconstruct() == ("reconstructed-fixture",)
    assert store.events == ["worker-recovery"]


@pytest.mark.asyncio
async def test_http_worker_recovery_does_not_read_dynamic_calibration_at_startup(installed_fixture):
    resource, anchor = installed_fixture
    store = FakeInstalledReadStore(anchor, _bootstrapped(resource))
    runtime = PipelineRuntime(object(), FakeRecoveryWorker(store.events), _profile(resource),
                             InstalledLocalRunProfileResolver(resource), store)
    assert await runtime.startup_reconstruct() == ("reconstructed-fixture",)
    assert store.events == ["worker-recovery"]
