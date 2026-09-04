"""Explicit fake acceptance for runner tests; this is not database authority."""

from datetime import datetime, timezone
from uuid import uuid4

from autocut_kernel.registry.installed_production_qc import load_installed_production_qc_resource
from autocut_kernel.rendering.production_process import ProductionExecutableIdentity
from autocut_kernel.rendering.production_qc_collector_capability import (
    ProductionQcCollectorCapabilityRequest,
    ProductionQcCollectorExecutableIdentity,
    ProductionQcCollectorLiveProfile,
)
from autocut_kernel.store.models import (
    PersistedProductionRenderQcCollectorCapability,
    ProductionQcCollectorCapabilityBinding,
)


def fake_accepted_capability(
    ffmpeg: ProductionExecutableIdentity,
    ffprobe: ProductionExecutableIdentity,
) -> PersistedProductionRenderQcCollectorCapability:
    resource = load_installed_production_qc_resource()
    policy = resource.policy
    live = ProductionQcCollectorLiveProfile(
        policy.profile_id,
        policy.policy_source_sha256,
        policy.registry_snapshot_sha256,
        policy.required_check_set_version,
        policy.collector_registry_sha256,
        policy.runner_schema_version,
        policy.fixed_environment_sha256,
        ProductionQcCollectorExecutableIdentity(
            ffmpeg.executable_sha256, ffmpeg.executable_byte_length, ffmpeg.version_output_sha256
        ),
        ProductionQcCollectorExecutableIdentity(
            ffprobe.executable_sha256, ffprobe.executable_byte_length, ffprobe.version_output_sha256
        ),
    )
    binding = ProductionQcCollectorCapabilityBinding(
        ProductionQcCollectorCapabilityRequest(policy, live), resource.provenance
    )
    return PersistedProductionRenderQcCollectorCapability(
        binding.request,
        binding.provenance,
        binding.scope_key,
        uuid4(),
        uuid4(),
        uuid4(),
        binding.measurement_member.content_hash,
        binding.decision_member.content_hash,
        datetime.now(timezone.utc),
    )


class FakeProductionQcCapabilityReader:
    """Only fixture code installs the fake record; mismatching requests fail closed."""

    accepted_capability: PersistedProductionRenderQcCollectorCapability

    def resolve_accepted_production_qc_collector_capability(
        self, request: ProductionQcCollectorCapabilityRequest
    ) -> PersistedProductionRenderQcCollectorCapability:
        assert request == self.accepted_capability.request
        return self.accepted_capability
