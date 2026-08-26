"""Pure Runtime budget wiring; no producer, Store or calibration admission."""

from types import SimpleNamespace

import pytest
from autocut_kernel.pipeline.prepare_timed_media_evidence_command import (
    TimedMediaEvidenceProducerError,
    resolve_committed_timed_media_request,
)

from auto_cut_bot.pipeline.media_preflight import LocalMediaPreflightError
from auto_cut_bot.pipeline.runtime.media_preflight_stage import (
    _ClaimOwnedLocalProducer,
    media_evidence_read_limits,
)
from auto_cut_bot.pipeline.runtime.models import (
    EvidenceReadLimits,
    PipelineCommand,
    PipelineRunRequest,
    PipelineStageContext,
)
from tests.media.test_prepare_timed_media_evidence_command import _request, _Store
from tests.pipeline.runtime_profile_fixture import execution_profile


def _context(*, blob_bytes: int, total_bytes: int) -> PipelineStageContext:
    return PipelineStageContext(
        "pipeline_run_" + "1" * 32,
        PipelineRunRequest("test", source_reference="unit-source"),
        PipelineCommand("media-command", "media_preflight", "pending"),
        execution_profile(evidence_limits=EvidenceReadLimits(blob_bytes, total_bytes)),
    )


def test_evidence_budget_is_not_derived_from_source_or_service_transfer_ceiling() -> None:
    context = _context(blob_bytes=10_000_000, total_bytes=12_000_000)
    profile = context.execution_profile
    source_controls = profile.to_materialization_limits()
    limits = media_evidence_read_limits(profile)

    assert limits.max_blob_bytes == 10_000_000
    assert limits.max_total_blob_bytes == 12_000_000
    assert limits.max_candidates == profile.to_doubao_policy().parse_policy.max_candidate_hypotheses
    assert limits.materialization.effective_max_source_bytes == 10_000_000
    assert limits.materialization.effective_max_source_bytes > source_controls.effective_max_source_bytes
    assert limits.materialization.copy_chunk_bytes == source_controls.copy_chunk_bytes
    assert limits.materialization.staging_quota_bytes == source_controls.staging_quota_bytes
    assert context.execution_profile.to_materialization_limits() == source_controls


def test_changing_frozen_budget_changes_reader_limits_not_native_service_limits() -> None:
    first = _context(blob_bytes=100_000, total_bytes=500_000)
    second = _context(blob_bytes=200_000, total_bytes=700_000)
    assert first.execution_profile_hash != second.execution_profile_hash
    assert first.execution_profile.to_materialization_limits() == second.execution_profile.to_materialization_limits()
    assert media_evidence_read_limits(first.execution_profile) != media_evidence_read_limits(second.execution_profile)


def test_claim_owned_adapter_passes_installed_identity_without_using_service_hash(tmp_path) -> None:
    store = _Store()
    request = _request(store)
    resolved = resolve_committed_timed_media_request(store, request)
    policy = execution_profile().to_media_preflight_policy()
    installed_adapter = "sha256:" + "f" * 64
    assert installed_adapter != policy.timed_speech_service_sha256
    calls = []

    class CapturePort:
        def prepare(self, local_request, *, kernel_max_source_bytes, service_max_request_bytes):
            calls.append(local_request)
            assert local_request.timed_speech_adapter_sha256 == installed_adapter
            assert kernel_max_source_bytes == request.materialization_limits.max_source_bytes
            assert service_max_request_bytes == request.materialization_limits.timed_speech_max_request_bytes
            raise LocalMediaPreflightError("deliberate unit boundary stop")

    producer = _ClaimOwnedLocalProducer(CapturePort(), policy, installed_adapter)
    with pytest.raises(TimedMediaEvidenceProducerError, match="deliberate unit boundary stop"):
        producer.prepare(resolved, SimpleNamespace(reference=request.source_blob, path=tmp_path / "source"))
    assert len(calls) == 1
