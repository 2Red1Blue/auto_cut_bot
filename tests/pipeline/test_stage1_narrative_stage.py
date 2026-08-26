"""Pure runtime-adapter tests for the Kernel-owned Stage 1 command."""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from typing import cast
from uuid import UUID, uuid4

import pytest
from autocut_kernel.media import (
    AudioBoundaryMethod,
    AudioSampleBoundary,
    AudioSampleBoundarySet,
    AudioSourceOutcome,
    Coverage,
    CoverageOutcome,
    EvidenceContext,
    MediaKind,
    PresentationProbeExecution,
    PTSIndex,
    TickRange,
    TimeBase,
)
from autocut_kernel.media.ffprobe_port import ProbeResult
from autocut_kernel.media.types import ToolEvidence, VideoStreamEvidence
from autocut_kernel.pipeline.build_narrative_graph_command import (
    BuildNarrativeGraphCommand,
    BuildNarrativeGraphResult,
)
from autocut_kernel.pipeline.build_narrative_graph_request import (
    BuildNarrativeGraphRequest,
)
from autocut_kernel.registry.installed_local_run import LocalRunResource
from autocut_kernel.semantic_chain.draft_provider import DraftProviderPort
from autocut_kernel.semantic_chain.stage1_command_policy import Stage1CommandPolicy
from autocut_kernel.semantic_chain.story_design_command_policy import Stage2CommandPolicy
from autocut_kernel.source_manifest import (
    SourceOperationPolicy,
    SourceOperationPurpose,
    identity_frame_index,
)
from autocut_kernel.store import (
    ArtifactScope,
    BlobRef,
    CommandOutcome,
    CommittedArtifactMemberReference,
    Job,
    PersistedWholeSeriesSourceManifest,
    SemanticInputUnavailableError,
    WholeSeriesSourceManifestReference,
)
from autocut_kernel.store.models import canonical_payload_hash
from autocut_kernel.vlm import (
    ProxyTimelineMap,
    WindowFrameSample,
    WindowManifest,
    WindowManifestSet,
    WindowProxyBlobRef,
)

from auto_cut_bot.pipeline.runtime.errors import PipelineRunValidationError
from auto_cut_bot.pipeline.runtime.models import (
    PipelineCommand,
    PipelineExecutionProfile,
    PipelineRunRequest,
    PipelineStageContext,
)
from auto_cut_bot.pipeline.runtime.stage1_narrative_stage import (
    Stage1NarrativePipelineStage,
    stage1_narrative_kernel_idempotency_key,
)
from auto_cut_bot.pipeline.runtime.vlm_stage import vlm_batch_kernel_idempotency_key
from auto_cut_bot.pipeline.source_prep import (
    PersistedPreparedSources,
    PreparedSeriesSources,
    PreparedSourceEpisode,
)
from auto_cut_bot.pipeline.source_prep.models import SeriesSource, SeriesSourceCensus
from auto_cut_bot.pipeline.source_prep.probe import (
    PRESENTATION_PROBE_INVOCATION_SCHEMA_SHA256,
    DecodedFrame,
    PresentationTimelineProbeDraft,
    SourceMediaProbe,
)
from tests.authority.test_authority_profile_sources import synthetic_stage1_command_policy
from tests.pipeline.installed_profile_fixture import synthetic_installed_resource
from tests.pipeline.test_pipeline_vlm_stage import _profile

RUN_ID = "pipeline_run_" + "a" * 32


class _Provider:
    def dispatch(self, _request: object) -> object:
        raise AssertionError("the adapter test command owns no provider dispatch")

    def reconcile(self, _request: object) -> object:
        raise AssertionError("the adapter test command owns no provider reconciliation")


class _CommandSpy:
    def __init__(self, outcome: CommandOutcome) -> None:
        self.outcome = outcome
        self.requests: list[BuildNarrativeGraphRequest] = []
        self.thread_ids: list[int] = []

    def execute(self, request: BuildNarrativeGraphRequest) -> BuildNarrativeGraphResult:
        self.requests.append(request)
        self.thread_ids.append(threading.get_ident())
        return BuildNarrativeGraphResult(self.outcome)


class _CommittedStore:
    """A public-reader-only Store double; it does not mint semantic inputs."""

    def __init__(
        self,
        *,
        source_outcome: CommandOutcome | None,
        source_record: PersistedWholeSeriesSourceManifest | None,
        vlm_outcome: CommandOutcome | None,
        aggregate: CommittedArtifactMemberReference | None,
    ) -> None:
        self.source_outcome = source_outcome
        self.source_record = source_record
        self.vlm_outcome = vlm_outcome
        self.aggregate = aggregate
        self.outcome_calls: list[tuple[Job, str]] = []
        self.thread_ids: list[int] = []

    def read_outcome(self, job: Job, idempotency_key: str) -> CommandOutcome | None:
        self.outcome_calls.append((job, idempotency_key))
        self.thread_ids.append(threading.get_ident())
        if idempotency_key.startswith("source-prep-kernel-v1:"):
            return self.source_outcome
        if idempotency_key.startswith("vlm-batch:"):
            return self.vlm_outcome
        raise AssertionError("Stage 1 queried an unregistered predecessor identity")

    def read_whole_series_source_manifest(
        self, job: Job, artifact_set_id: UUID,
    ) -> PersistedWholeSeriesSourceManifest:
        self.thread_ids.append(threading.get_ident())
        record = self.source_record
        assert record is not None
        assert job == record.source_job
        assert artifact_set_id == record.artifact_set_id
        return record

    def read_committed_vlm_semantic_pack_set_reference(
        self, job: Job, idempotency_key: str,
    ) -> CommittedArtifactMemberReference:
        self.thread_ids.append(threading.get_ident())
        assert idempotency_key.startswith("vlm-batch:")
        aggregate = self.aggregate
        if aggregate is None:
            raise SemanticInputUnavailableError("fixture has no committed aggregate")
        assert job == Job(RUN_ID, "test")
        return aggregate


def _hash(digit: str) -> str:
    return "sha256:" + digit * 64


def _bundle(*, purposes: tuple[SourceOperationPurpose, ...] = ("semantic_analysis",)) -> tuple[PersistedPreparedSources, dict[UUID, bytes]]:
    """Build one strict-decoder-compatible committed-source test record."""
    job = Job(RUN_ID, "test")
    source = SeriesSource("episode-000.mp4", "source-000", _hash("1"), 4_096)
    time_base = TimeBase(1, 1_000)
    proxy = BlobRef(uuid4(), source.content_sha256, source.byte_size, "video/mp4")
    video_range = TickRange(0, 30)
    audio_context = EvidenceContext(
        source.source_id, source.content_sha256, MediaKind.AUDIO, "audio-stream-1",
        time_base, 0, 30, "fixture-audio", _hash("2"),
    )
    audio_boundaries = AudioSampleBoundarySet(
        "fixture-audio-boundaries",
        audio_context,
        Coverage(source.source_id, source.content_sha256, "audio-stream-1", time_base, 0, 30, CoverageOutcome.COMPLETE),
        AudioSourceOutcome.BOUNDARIES_AVAILABLE,
        (
            AudioSampleBoundary("audio-boundary-0", source.source_id, source.content_sha256, "audio-stream-1", time_base, 0, AudioBoundaryMethod.DECODER),
            AudioSampleBoundary("audio-boundary-1", source.source_id, source.content_sha256, "audio-stream-1", time_base, 10, AudioBoundaryMethod.DECODER),
            AudioSampleBoundary("audio-boundary-2", source.source_id, source.content_sha256, "audio-stream-1", time_base, 20, AudioBoundaryMethod.DECODER),
            AudioSampleBoundary("audio-boundary-3", source.source_id, source.content_sha256, "audio-stream-1", time_base, 30, AudioBoundaryMethod.DECODER),
        ),
    )
    probe = SourceMediaProbe(
        source,
        ProbeResult(
            VideoStreamEvidence(0, "h264", 16, 16, time_base),
            PTSIndex((0, 10, 20)),
            ToolEvidence("ffprobe", "fixture-v1", _hash("3")),
        ),
        video_range,
        audio_boundaries,
        _hash("4"),
        _hash("5"),
        presentation_video_frame_boundaries=(
            DecodedFrame(0, 10), DecodedFrame(10, 20), DecodedFrame(20, 30),
        ),
        presentation_audio_frame_boundaries=(
            DecodedFrame(0, 10), DecodedFrame(10, 20), DecodedFrame(20, 30),
        ),
        _presentation_timeline_draft=PresentationTimelineProbeDraft(
            PresentationProbeExecution(
                "ffprobe-decoded-presentation-v2",
                PRESENTATION_PROBE_INVOCATION_SCHEMA_SHA256,
                _hash("6"), _hash("7"), _hash("8"), source.content_sha256,
            ),
            0, time_base,
            (DecodedFrame(0, 10), DecodedFrame(10, 20), DecodedFrame(20, 30)),
            1, time_base,
            (DecodedFrame(0, 10), DecodedFrame(10, 20), DecodedFrame(20, 30)),
        ),
    )
    frame_index = identity_frame_index(probe)
    manifest = WindowManifest(
        source.source_id, frame_index.context.clock_id, source.content_sha256, 0, time_base,
        video_range, video_range, frame_index,
        WindowProxyBlobRef(str(proxy.object_id), proxy.content_hash, proxy.byte_length, proxy.media_type),
        _hash("9"), _hash("a"),
        ProxyTimelineMap.translation(time_base=time_base, proxy_range=TickRange(0, 30), source_start_pts=0),
        (WindowFrameSample(0, 0, _hash("b")), WindowFrameSample(20, 20, _hash("c"))),
    )
    probe = probe.bind_presentation_timeline(
        source_blob=proxy,
        frame_pts_index_set_sha256=frame_index.canonical_hash,
        source_proxy_timeline_map_sha256=manifest.timeline_map.canonical_hash,
        window_manifest_sha256=manifest.canonical_hash,
    )
    manifest_set = WindowManifestSet(
        source.source_id, manifest.source_clock_id, source.content_sha256, 0, time_base,
        video_range, (manifest,),
    )
    prepared = PreparedSeriesSources(
        SeriesSourceCensus(
            SourceOperationPolicy("fixture-authority", "fixture-series", 1, purposes),
            "all_or_nothing", (source,),
        ),
        (PreparedSourceEpisode(probe, proxy, manifest, manifest_set),),
    )
    payload_json = json.dumps(prepared.to_mapping(), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return (
        PersistedPreparedSources(
            prepared, job, uuid4(), uuid4(), uuid4(), uuid4(),
            WholeSeriesSourceManifestReference(
                ArtifactScope("pipeline", "job", RUN_ID), "whole_series_source_manifest", 1,
                canonical_payload_hash(payload_json),
            ),
        ),
        {proxy.object_id: b"fixture-source-bytes"},
    )


def _context(*, stage1_policy: Stage1CommandPolicy | None = None,
             stage2_policy: Stage2CommandPolicy | None = None) -> PipelineStageContext:
    base = _profile()
    policy = base.build_stage1_command_policy() if stage1_policy is None else stage1_policy
    profile = PipelineExecutionProfile.from_policies(
        base.to_doubao_policy(),
        base.to_media_preflight_policy(),
        retry_policy=base.to_generation_retry_policy(),
        materialization_limits=base.to_materialization_limits(),
        stage1_policy=policy,
        stage2_policy=base.build_stage2_command_policy() if stage2_policy is None else stage2_policy,
        stage3_policy=base.build_stage3_command_policy(),
        evidence_read_limits=base.to_evidence_read_limits(),
    )
    return PipelineStageContext(
        RUN_ID,
        PipelineRunRequest("test", source_reference="authorized-source"),
        PipelineCommand("stage1-control", "stage1_narrative", "running", version=1, lease_id="lease"),
        profile,
    )


def _source_record(bundle: PersistedPreparedSources) -> PersistedWholeSeriesSourceManifest:
    payload_json = json.dumps(
        bundle.prepared.to_mapping(), ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    )
    assert canonical_payload_hash(payload_json) == bundle.artifact_reference.content_hash
    return PersistedWholeSeriesSourceManifest(
        bundle.artifact_reference,
        payload_json,
        tuple(item.proxy_blob for item in bundle.prepared.episodes),
        bundle.kernel_job_id,
        bundle.receipt_id,
        bundle.artifact_set_id,
        bundle.command_slot_id,
        source_job=bundle.source_job,
    )


def _source_success(bundle: PersistedPreparedSources) -> CommandOutcome:
    return CommandOutcome(
        bundle.command_slot_id,
        "succeeded",
        receipt_id=bundle.receipt_id,
        artifact_set_id=bundle.artifact_set_id,
        job_id=bundle.kernel_job_id,
    )


def _aggregate() -> CommittedArtifactMemberReference:
    return CommittedArtifactMemberReference(
        uuid4(), uuid4(), 0, ArtifactScope("pipeline", "job", RUN_ID),
        "vlm_semantic_pack_set", "vlm_semantic_pack_set", 1, "sha256:" + "2" * 64,
    )


def _stage(
    store: _CommittedStore,
    outcome: CommandOutcome,
    *,
    installed_profile: LocalRunResource | None = None,
) -> tuple[Stage1NarrativePipelineStage, _CommandSpy]:
    spy = _CommandSpy(outcome)
    return (
        Stage1NarrativePipelineStage(
            store,
            cast(DraftProviderPort, _Provider()),
            command=cast(BuildNarrativeGraphCommand, spy),
            installed_profile=installed_profile,
        ),
        spy,
    )


def test_stage1_key_is_stable_and_binds_exact_committed_source_provenance() -> None:
    bundle, _ = _bundle()
    first = stage1_narrative_kernel_idempotency_key(
        run_id=RUN_ID, source_bundle=bundle, execution_profile_hash="sha256:" + "a" * 64,
    )
    assert first == stage1_narrative_kernel_idempotency_key(
        run_id=RUN_ID, source_bundle=bundle, execution_profile_hash="sha256:" + "a" * 64,
    )
    assert first != stage1_narrative_kernel_idempotency_key(
        run_id=RUN_ID, source_bundle=bundle, execution_profile_hash="sha256:" + "b" * 64,
    )
    assert first.startswith("stage1-narrative:")


@pytest.mark.asyncio
async def test_execute_reads_exact_predecessors_off_event_loop_and_delegates_once() -> None:
    bundle, _ = _bundle()
    policy = replace(synthetic_stage1_command_policy(), artifact_revision=2)
    context = _context(stage1_policy=policy)
    store = _CommittedStore(
        source_outcome=_source_success(bundle), source_record=_source_record(bundle),
        vlm_outcome=CommandOutcome(uuid4(), "succeeded", receipt_id=uuid4(), artifact_set_id=uuid4(), job_id=uuid4()),
        aggregate=_aggregate(),
    )
    stage, spy = _stage(
        store,
        CommandOutcome(uuid4(), "succeeded", receipt_id=uuid4(), artifact_set_id=uuid4(), job_id=uuid4()),
        installed_profile=synthetic_installed_resource(command_policy=policy),
    )

    result = await stage.execute(context)

    assert result.outcome == "succeeded" and result.receipt_id == spy.outcome.receipt_id
    assert len(spy.requests) == 1
    request = spy.requests[0]
    assert request.inputs.source_manifest.receipt_id == bundle.receipt_id  # type: ignore[attr-defined]
    assert request.inputs.source_manifest.artifact_set_id == bundle.artifact_set_id  # type: ignore[attr-defined]
    assert request.inputs.source_manifest.member_ordinal == 0  # type: ignore[attr-defined]
    assert request.inputs.source_manifest.revision == 1  # type: ignore[attr-defined]
    assert request.inputs.vlm_semantic_pack_set == store.aggregate  # type: ignore[attr-defined]
    assert request.artifact_revision == 2  # type: ignore[attr-defined]
    assert tuple(key for _, key in store.outcome_calls) == (
        "source-prep-kernel-v1:" + RUN_ID,
        vlm_batch_kernel_idempotency_key(
            run_id=RUN_ID, source_bundle=bundle,
            execution_profile_hash=context.execution_profile_hash,
        ),
    )
    assert threading.get_ident() not in store.thread_ids
    assert threading.get_ident() not in spy.thread_ids


@pytest.mark.asyncio
async def test_execute_and_reconcile_reuse_the_same_exact_request_and_receipt() -> None:
    bundle, _ = _bundle()
    policy = synthetic_stage1_command_policy()
    store = _CommittedStore(
        source_outcome=_source_success(bundle), source_record=_source_record(bundle),
        vlm_outcome=CommandOutcome(uuid4(), "succeeded", receipt_id=uuid4(), artifact_set_id=uuid4(), job_id=uuid4()),
        aggregate=_aggregate(),
    )
    outcome = CommandOutcome(
        uuid4(), "succeeded", receipt_id=uuid4(), artifact_set_id=uuid4(), job_id=uuid4(),
    )
    stage, spy = _stage(store, outcome, installed_profile=synthetic_installed_resource(command_policy=policy))

    context = _context(stage1_policy=policy)
    executed = await stage.execute(context)
    result = await stage.reconcile(context)

    assert executed.outcome == "succeeded" and executed.receipt_id == outcome.receipt_id
    assert result is not None
    assert result.outcome == "succeeded" and result.receipt_id == outcome.receipt_id
    assert len(spy.requests) == 2
    assert spy.requests[0] == spy.requests[1]
    assert spy.requests[0].idempotency_key.startswith("stage1-narrative:")


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ("pending", "running", None))
async def test_unready_source_never_mints_a_stage1_success(state: str | None) -> None:
    source = None if state is None else CommandOutcome(uuid4(), state)  # type: ignore[arg-type]
    store = _CommittedStore(source_outcome=source, source_record=None, vlm_outcome=None, aggregate=None)
    stage, spy = _stage(
        store,
        CommandOutcome(uuid4(), "succeeded", receipt_id=uuid4(), artifact_set_id=uuid4(), job_id=uuid4()),
    )

    assert (await stage.execute(_context())).outcome == "indeterminate"
    assert await stage.reconcile(_context()) is None
    assert spy.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ("denied", "failed"))
async def test_terminal_predecessors_are_rejected_not_relabelled_as_stage1(state: str) -> None:
    terminal = CommandOutcome(uuid4(), state, receipt_id=uuid4(), job_id=uuid4())  # type: ignore[arg-type]
    store = _CommittedStore(source_outcome=terminal, source_record=None, vlm_outcome=None, aggregate=None)
    stage, spy = _stage(store, terminal)

    with pytest.raises(PipelineRunValidationError, match="terminal source-preparation predecessor"):
        await stage.execute(_context())
    assert spy.requests == []


@pytest.mark.asyncio
async def test_terminal_vlm_predecessor_and_missing_aggregate_do_not_become_success() -> None:
    bundle, _ = _bundle()
    source = _source_success(bundle)
    terminal = CommandOutcome(uuid4(), "failed", receipt_id=uuid4(), job_id=uuid4())
    store = _CommittedStore(source_outcome=source, source_record=_source_record(bundle), vlm_outcome=terminal, aggregate=None)
    stage, spy = _stage(store, terminal)

    with pytest.raises(PipelineRunValidationError, match="terminal VLM predecessor"):
        await stage.execute(_context())
    assert spy.requests == []

    unavailable = _CommittedStore(
        source_outcome=source, source_record=_source_record(bundle),
        vlm_outcome=CommandOutcome(uuid4(), "succeeded", receipt_id=uuid4(), artifact_set_id=uuid4(), job_id=uuid4()),
        aggregate=None,
    )
    stage, spy = _stage(unavailable, terminal)
    with pytest.raises(PipelineRunValidationError, match="exact committed VLM SemanticPackSet"):
        await stage.execute(_context())
    assert spy.requests == []


@pytest.mark.asyncio
async def test_installed_policy_mismatch_stops_before_any_store_or_provider_access() -> None:
    policy = synthetic_stage1_command_policy()
    store = _CommittedStore(source_outcome=None, source_record=None, vlm_outcome=None, aggregate=None)
    stage, spy = _stage(
        store,
        CommandOutcome(uuid4(), "succeeded", receipt_id=uuid4(), artifact_set_id=uuid4(), job_id=uuid4()),
        installed_profile=synthetic_installed_resource(command_policy=policy),
    )

    with pytest.raises(PipelineRunValidationError, match="differs from installed narrative policy"):
        await stage.execute(_context(stage1_policy=replace(policy, artifact_revision=2)))
    assert store.outcome_calls == []
    assert spy.requests == []


@pytest.mark.asyncio
async def test_installed_declared_policy_hash_mismatch_stops_before_store_access() -> None:
    policy = synthetic_stage1_command_policy()
    installed = synthetic_installed_resource(command_policy=policy)
    bad_reference = replace(
        installed.narrative.reference,
        stage1_command_policy_sha256="sha256:" + "f" * 64,
    )
    installed = replace(installed, narrative=replace(installed.narrative, reference=bad_reference))
    store = _CommittedStore(source_outcome=None, source_record=None, vlm_outcome=None, aggregate=None)
    stage, spy = _stage(
        store,
        CommandOutcome(uuid4(), "succeeded", receipt_id=uuid4(), artifact_set_id=uuid4(), job_id=uuid4()),
        installed_profile=installed,
    )

    with pytest.raises(PipelineRunValidationError, match="differs from installed narrative policy"):
        await stage.execute(_context(stage1_policy=policy))
    assert store.outcome_calls == []
    assert spy.requests == []
