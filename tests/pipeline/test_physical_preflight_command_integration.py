"""Adapter/Command/readback integration with synthetic Store and physical facts.

This executes no detector process, codec, speech model or real database.
"""

from dataclasses import replace
from pathlib import Path
from typing import cast

from autocut_kernel.pipeline.committed_physical_media import (
    read_committed_physical_media_evidence,
)
from autocut_kernel.pipeline.physical_media_contract import (
    PreparePhysicalMediaEvidenceRequest,
)
from autocut_kernel.pipeline.prepare_physical_media_evidence_command import (
    PreparePhysicalMediaEvidenceCommand,
)
from autocut_kernel.pipeline.prepare_timed_media_evidence_command import (
    resolve_committed_timed_media_request,
)

from auto_cut_bot.pipeline.media_preflight import LocalMediaPreflightPort
from auto_cut_bot.pipeline.media_preflight.physical_adapter import (
    ClaimOwnedPhysicalMediaProducer,
)
from tests.media.test_prepare_timed_media_evidence_command import _request
from tests.pipeline.test_physical_media_preflight import (
    _adapter_policy,
    _adapter_result,
    _PhysicalPortSpy,
)
from tests.pipeline.test_physical_media_prelude import Store, _limits


def _case(tmp_path: Path):
    store = Store(tmp_path)
    parent = _request(store)
    source = resolve_committed_timed_media_request(store, parent)
    policy = _adapter_policy(source)
    source_contexts = {
        "frame": source.frame_pts_index.context,
        "audio": source.audio_sample_boundaries.context,
    }
    policy = replace(policy, calibrations=tuple(
        replace(
            item,
            producer_id=source_contexts[item.producer_kind].producer_id,
            generation_policy_sha256=(
                source_contexts[item.producer_kind].generation_policy_sha256
            ),
        ) if item.producer_kind in source_contexts else item
        for item in policy.calibrations
    ))
    request = PreparePhysicalMediaEvidenceRequest(
        parent, policy.canonical_hash, 1_000_000, 1_000_000,
    )
    spy = _PhysicalPortSpy(_adapter_result(source, policy))
    adapter = ClaimOwnedPhysicalMediaProducer(cast(LocalMediaPreflightPort, spy), policy)
    return store, request, spy, PreparePhysicalMediaEvidenceCommand(store, adapter)


def test_adapter_result_commits_and_reads_without_speech_or_redispatch(tmp_path: Path):
    store, request, spy, command = _case(tmp_path)

    result = command.execute(request)

    assert result.outcome.state == "succeeded", result.outcome.failure_detail_json
    assert len(spy.requests) == 1
    evidence = read_committed_physical_media_evidence(
        store, request, result.outcome, limits=_limits(),
    )
    assert evidence.produced.producer_policy_sha256 == request.physical_policy_sha256
    assert evidence.produced.physical_root.frame_pts_index == request.parent.frame_pts_index
    assert len(store.records[result.outcome.artifact_set_id].members) == 3
    assert command.execute(request).outcome == result.outcome
    assert len(spy.requests) == 1 and store.closed_materializations == 2
    assert not store.rejections


def test_adapter_result_cannot_self_assert_foreign_calibration(tmp_path: Path):
    store, request, spy, command = _case(tmp_path)
    first, *rest = spy.result.calibration_bindings
    spy.result = replace(spy.result, calibration_bindings=(
        replace(first, calibration_record_sha256="sha256:" + "f" * 64), *rest,
    ))

    result = command.execute(request)

    assert result.outcome.state == "denied"
    assert not store.records and not store.puts
    assert len(spy.requests) == 1 and store.closed_materializations == 1
    assert command.execute(request).outcome.state == "denied"
    assert len(spy.requests) == 1
