from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import cast
from uuid import UUID, uuid4

import pytest
from autocut_kernel.media import (
    Coverage,
    CoverageOutcome,
    EvidenceContext,
    FramePtsIndexSet,
    MediaKind,
    PTSIndex,
    TickRange,
    TimeBase,
)
from autocut_kernel.media.types import canonical_sha256
from autocut_kernel.pipeline import FinalizeVlmBatchCommand
from autocut_kernel.store import (
    BlobRef,
    CommandClaim,
    CommandOutcome,
    CommandRejection,
    CommandSuccess,
    GenerationAttempt,
    Job,
    PersistedVlmGenerationChild,
    VlmRequestRecordReference,
    WholeSeriesSourceManifestReference,
)
from autocut_kernel.store.models import canonical_payload_hash, canonical_recipe_scope
from autocut_kernel.vlm import (
    GENERATION_RETRY_STRATEGY_VERSION,
    GenerationRetryPolicy,
    ProviderCompleted,
    ProviderDispatchRequest,
    ProviderIndeterminate,
    ProviderReconcileQuery,
    ProxyTimelineMap,
    VlmParsePolicy,
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
from auto_cut_bot.pipeline.runtime.source_prep_stage import (
    source_prep_kernel_idempotency_key,
)
from auto_cut_bot.pipeline.runtime.vlm_stage import (
    VLM_EPISODE_SELECTION_STRATEGY_VERSION,
    VlmPipelineStage,
)
from auto_cut_bot.pipeline.source_prep import (
    PersistedPreparedSources,
    PreparedSeriesSources,
    PreparedSourceEpisode,
)
from auto_cut_bot.pipeline.source_prep.models import SeriesSource, SeriesSourceCensus
from auto_cut_bot.pipeline.source_prep.probe import SourceMediaProbe
from auto_cut_bot.pipeline.vlm import DoubaoVlmRequestPolicy

RUN_ID = "pipeline_run_" + "a" * 32


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


class _Probe:
    def to_mapping(self) -> dict[str, object]:
        return {"fixture": "stage-adapter-v1"}


def _episode(index: int) -> tuple[PreparedSourceEpisode, bytes, SeriesSource]:
    proxy_content = f"exact-proxy-{index}".encode()
    proxy = BlobRef(uuid4(), _digest(proxy_content), len(proxy_content), "video/mp4")
    source_id = f"source-{index:03d}"
    source_hash = "sha256:" + f"{index + 1:x}" * 64
    time_base = TimeBase(1, 1_000)
    ticks = PTSIndex((1_000, 1_010, 1_050, 1_090, 1_100))
    evidence_context = EvidenceContext(
        source_id,
        source_hash,
        MediaKind.VIDEO,
        "video-clock-0",
        time_base,
        1_000,
        100,
        "test-decoder-v1",
        "sha256:" + "7" * 64,
    )
    frame_index = FramePtsIndexSet(
        "frame-pts-root-v1",
        evidence_context,
        Coverage(
            source_id,
            source_hash,
            "video-clock-0",
            time_base,
            1_000,
            1_100,
            CoverageOutcome.COMPLETE,
        ),
        ticks,
        canonical_sha256(list(ticks.ticks)),
    )
    manifest = WindowManifest(
        source_id=source_id,
        source_clock_id="video-clock-0",
        source_sha256=source_hash,
        stream_index=0,
        source_time_base=time_base,
        source_range=TickRange(1_000, 1_100),
        core_range=TickRange(1_000, 1_100),
        frame_pts_index_set=frame_index,
        proxy_blob_ref=WindowProxyBlobRef(
            str(proxy.object_id),
            proxy.content_hash,
            proxy.byte_length,
            proxy.media_type,
        ),
        preprocess_policy_sha256="sha256:" + "b" * 64,
        window_sampling_policy_sha256="sha256:" + "c" * 64,
        timeline_map=ProxyTimelineMap.translation(
            time_base=time_base,
            proxy_range=TickRange(0, 100),
            source_start_pts=1_000,
            max_source_error_pts=1,
        ),
        frame_samples=(WindowFrameSample(1_050, 50, "sha256:" + "d" * 64),),
    )
    manifest_set = WindowManifestSet(
        manifest.source_id,
        manifest.source_clock_id,
        manifest.source_sha256,
        manifest.stream_index,
        manifest.source_time_base,
        manifest.core_range,
        (manifest,),
    )
    source = SeriesSource(
        f"episode-{index:03d}.mp4",
        source_id,
        source_hash,
        len(proxy_content),
    )
    return (
        PreparedSourceEpisode(cast(SourceMediaProbe, _Probe()), proxy, manifest, manifest_set),
        proxy_content,
        source,
    )


def _bundle(count: int = 1) -> tuple[PersistedPreparedSources, dict[UUID, bytes]]:
    values = tuple(_episode(index) for index in range(count))
    job = Job(RUN_ID, "test")
    prepared = PreparedSeriesSources(
        SeriesSourceCensus(
            "authorized-source",
            "series-001",
            "all_or_nothing",
            tuple(value[2] for value in values),
        ),
        tuple(value[0] for value in values),
    )
    payload_json = json.dumps(
        prepared.to_mapping(), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    bundle = PersistedPreparedSources(
        prepared,
        job,
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        WholeSeriesSourceManifestReference(
            canonical_recipe_scope(job),
            "whole_series_source_manifest",
            1,
            canonical_payload_hash(payload_json),
        ),
    )
    return bundle, {value[0].proxy_blob.object_id: value[1] for value in values}


class KernelStore:
    def __init__(self, source_outcome: CommandOutcome | None, blobs: dict[UUID, bytes]) -> None:
        self.source_outcome = source_outcome
        self.blobs = dict(blobs)
        self.claims: dict[tuple[str, str], tuple[CommandClaim, CommandOutcome]] = {}
        self.attempts: dict[UUID, GenerationAttempt] = {}
        self.attempt_chains: dict[UUID, list[GenerationAttempt]] = {}
        self.generation_successes: dict[UUID, CommandSuccess] = {}

    def read_outcome(self, job: Job, idempotency_key: str) -> CommandOutcome | None:
        assert job == Job(RUN_ID, "test")
        if idempotency_key == source_prep_kernel_idempotency_key(RUN_ID):
            return self.source_outcome
        existing = self.claims.get((job.job_key, idempotency_key))
        return existing[1] if existing is not None else None

    def claim_command(self, claim: CommandClaim) -> CommandOutcome:
        key = (claim.job.job_key, claim.idempotency_key)
        existing = self.claims.get(key)
        if existing is not None:
            assert existing[0] == claim
            return existing[1]
        outcome = CommandOutcome(uuid4(), "running", is_fresh_claim=True, job_id=uuid4())
        self.claims[key] = (claim, outcome)
        return outcome

    def put_immutable_blob(
        self,
        job: Job,
        *,
        content: bytes,
        content_hash: str,
        media_type: str,
    ) -> BlobRef:
        del job
        assert content_hash == _digest(content)
        reference = BlobRef(uuid4(), content_hash, len(content), media_type)
        self.blobs[reference.object_id] = content
        return reference

    def read_immutable_blob(self, job: Job, reference: BlobRef) -> bytes:
        del job
        return self.blobs[reference.object_id]

    def reserve_generation_attempt(
        self,
        command_slot_id: UUID,
        request_hash: str,
        *,
        provider_id: str,
        provider_idempotency_key: str,
        request_payload: BlobRef,
        retry_policy_hash: str,
        max_attempts: int,
    ) -> GenerationAttempt:
        attempt = GenerationAttempt(
            uuid4(),
            uuid4(),
            command_slot_id,
            request_hash,
            provider_id,
            provider_idempotency_key,
            request_payload,
            "reserved",
            0,
            retry_policy_hash=retry_policy_hash,
            max_attempts=max_attempts,
            is_fresh_reservation=True,
        )
        self.attempts[command_slot_id] = attempt
        self.attempt_chains[command_slot_id] = [attempt]
        return attempt

    def reserve_next_generation_attempt(
        self,
        previous_attempt_id: UUID,
        *,
        expected_version: int,
        provider_idempotency_key: str,
    ) -> GenerationAttempt:
        previous = next(
            item for item in self.attempts.values() if item.attempt_id == previous_attempt_id
        )
        assert previous.version == expected_version
        attempt = GenerationAttempt(
            uuid4(),
            previous.job_id,
            previous.command_slot_id,
            previous.request_hash,
            previous.provider_id,
            provider_idempotency_key,
            previous.request_payload,
            "reserved",
            0,
            attempt_ordinal=previous.attempt_ordinal + 1,
            previous_attempt_id=previous.attempt_id,
            retry_policy_hash=previous.retry_policy_hash,
            max_attempts=previous.max_attempts,
            is_fresh_reservation=True,
        )
        self.attempts[previous.command_slot_id] = attempt
        self.attempt_chains[previous.command_slot_id].append(attempt)
        return attempt

    def _transition(self, attempt_id: UUID, **changes: object) -> GenerationAttempt:
        slot_id, attempt = next(
            (slot_id, item)
            for slot_id, item in self.attempts.items()
            if item.attempt_id == attempt_id
        )
        updated = replace(attempt, version=attempt.version + 1, **changes)
        self.attempts[slot_id] = updated
        chain = self.attempt_chains[slot_id]
        chain[chain.index(attempt)] = updated
        return updated

    def dispatch_generation_attempt(
        self,
        attempt_id: UUID,
        *,
        expected_version: int,
        provider_request_id: str | None = None,
    ) -> GenerationAttempt:
        del expected_version
        return self._transition(
            attempt_id,
            state="dispatched",
            provider_request_id=provider_request_id,
            dispatch_lease_token="fixture-dispatch-lease",
            dispatch_lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            is_fresh_reservation=False,
        )

    def acquire_generation_reconcile_lease(
        self,
        attempt_id: UUID,
        *,
        expected_version: int,
    ) -> GenerationAttempt | None:
        attempt = next(item for item in self.attempts.values() if item.attempt_id == attempt_id)
        assert attempt.version == expected_version
        if attempt.dispatch_lease_is_active():
            return None
        return self._transition(
            attempt_id,
            dispatch_lease_token="fixture-reconcile-lease",
            dispatch_lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )

    def record_generation_provider_request_id(
        self,
        attempt_id: UUID,
        *,
        expected_version: int,
        provider_request_id: str,
        dispatch_lease_token: str,
    ) -> GenerationAttempt:
        del expected_version, dispatch_lease_token
        return self._transition(attempt_id, provider_request_id=provider_request_id)

    def record_generation_response(
        self,
        attempt_id: UUID,
        *,
        expected_version: int,
        raw_response: BlobRef,
        dispatch_lease_token: str,
        provider_request_id: str | None = None,
    ) -> GenerationAttempt:
        del expected_version, dispatch_lease_token
        return self._transition(
            attempt_id,
            state="responded",
            raw_response=raw_response,
            provider_request_id=provider_request_id,
            dispatch_lease_token=None,
            dispatch_lease_expires_at=None,
        )

    def mark_generation_indeterminate(
        self,
        attempt_id: UUID,
        *,
        expected_version: int,
        dispatch_lease_token: str,
        provider_request_id: str | None = None,
    ) -> GenerationAttempt:
        del expected_version, dispatch_lease_token
        return self._transition(
            attempt_id,
            state="indeterminate",
            provider_request_id=provider_request_id,
            dispatch_lease_token=None,
            dispatch_lease_expires_at=None,
        )

    def reconcile_generation_response(
        self,
        attempt_id: UUID,
        *,
        expected_version: int,
        raw_response: BlobRef,
        dispatch_lease_token: str,
        provider_request_id: str | None = None,
    ) -> GenerationAttempt:
        del expected_version, dispatch_lease_token
        return self._transition(
            attempt_id,
            state="reconciled",
            raw_response=raw_response,
            provider_request_id=provider_request_id,
            dispatch_lease_token=None,
            dispatch_lease_expires_at=None,
        )

    def fail_generation_attempt(
        self,
        attempt_id: UUID,
        *,
        expected_version: int,
        failure_code: str,
        failure_detail_json: str,
        provider_request_id: str | None = None,
        failure_disposition: str = "nonretryable",
        dispatch_lease_token: str | None = None,
    ) -> GenerationAttempt:
        del expected_version, dispatch_lease_token
        return self._transition(
            attempt_id,
            state="failed",
            failure_code=failure_code,
            failure_detail_json=failure_detail_json,
            provider_request_id=provider_request_id,
            failure_disposition=failure_disposition,
            dispatch_lease_token=None,
            dispatch_lease_expires_at=None,
        )

    def commit_generation_success(
        self,
        attempt_id: UUID,
        *,
        expected_version: int,
        success: CommandSuccess,
    ) -> GenerationAttempt:
        del expected_version
        receipt_id = uuid4()
        artifact_set_id = uuid4()
        attempt = self._transition(
            attempt_id,
            state="committed",
            receipt_id=receipt_id,
            artifact_set_id=artifact_set_id,
            dispatch_lease_token=None,
            dispatch_lease_expires_at=None,
        )
        self.generation_successes[success.command_slot_id] = success
        self._replace_outcome(
            success.command_slot_id,
            CommandOutcome(
                success.command_slot_id,
                "succeeded",
                receipt_id=receipt_id,
                artifact_set_id=artifact_set_id,
                job_id=attempt.job_id,
            ),
        )
        return attempt

    def commit_generation_rejection(
        self,
        attempt_id: UUID,
        *,
        expected_version: int,
        rejection: CommandRejection,
    ) -> CommandOutcome:
        attempt = next(item for item in self.attempts.values() if item.attempt_id == attempt_id)
        assert attempt.version == expected_version
        outcome = CommandOutcome(
            rejection.command_slot_id,
            rejection.outcome,
            receipt_id=uuid4(),
            failure_code=rejection.failure_code,
            failure_detail_json=rejection.failure_detail_json,
            job_id=attempt.job_id,
        )
        self._replace_outcome(rejection.command_slot_id, outcome)
        return outcome

    def read_committed_vlm_generation_child(
        self,
        job: Job,
        idempotency_key: str,
    ) -> PersistedVlmGenerationChild:
        claim, outcome = self.claims[(job.job_key, idempotency_key)]
        attempt = self.attempts[outcome.command_slot_id]
        artifact = next(
            item
            for item in self.generation_successes[outcome.command_slot_id].artifacts
            if item.artifact_type == "vlm_request_record"
        )
        payload = json.loads(artifact.payload_json)
        assert outcome.receipt_id is not None
        assert outcome.artifact_set_id is not None
        return PersistedVlmGenerationChild(
            reference=VlmRequestRecordReference(
                artifact.scope,
                artifact.logical_id,
                artifact.revision,
                artifact.content_hash,
            ),
            payload_json=artifact.payload_json,
            source_job=job,
            kernel_job_id=attempt.job_id,
            command_slot_id=outcome.command_slot_id,
            idempotency_key=idempotency_key,
            request_hash=claim.request_hash,
            attempt_id=attempt.attempt_id,
            provider_idempotency_key=attempt.provider_idempotency_key,
            request_payload=attempt.request_payload,
            receipt_id=outcome.receipt_id,
            artifact_set_id=outcome.artifact_set_id,
            episode_index=payload["episode_index"],
            window_manifest_sha256=payload["window_manifest_sha256"],
            window_manifest_set_sha256=payload["window_manifest_set_sha256"],
            source_manifest_sha256=payload["source_manifest_sha256"],
            source_provenance_sha256=payload["source_provenance_sha256"],
            request_identity_sha256=payload["request_identity_sha256"],
        )

    def commit_command_success(self, success: CommandSuccess) -> CommandOutcome:
        outcome = CommandOutcome(
            success.command_slot_id,
            "succeeded",
            receipt_id=uuid4(),
            artifact_set_id=uuid4(),
            job_id=uuid4(),
        )
        self._replace_outcome(success.command_slot_id, outcome)
        return outcome

    def commit_command_rejection(self, rejection: CommandRejection) -> CommandOutcome:
        outcome = CommandOutcome(
            rejection.command_slot_id,
            rejection.outcome,
            receipt_id=uuid4(),
            failure_code=rejection.failure_code,
            failure_detail_json=rejection.failure_detail_json,
            job_id=uuid4(),
        )
        self._replace_outcome(rejection.command_slot_id, outcome)
        return outcome

    def _replace_outcome(self, slot_id: UUID, outcome: CommandOutcome) -> None:
        for key, (claim, current) in self.claims.items():
            if current.command_slot_id == slot_id:
                self.claims[key] = (claim, outcome)
                return
        raise AssertionError("unknown command slot")

    def read_generation_attempt_for_slot(
        self,
        job: Job,
        command_slot_id: UUID,
    ) -> GenerationAttempt | None:
        del job
        return self.attempts.get(command_slot_id)

    def read_generation_attempt_chain(
        self,
        job: Job,
        command_slot_id: UUID,
    ) -> tuple[GenerationAttempt, ...]:
        del job
        return tuple(self.attempt_chains.get(command_slot_id, ()))

    def read_whole_series_source_manifest(self, job: Job, artifact_set_id: UUID):
        del job, artifact_set_id
        raise AssertionError("tests inject the exact provenance-bearing source reader")


class Provider:
    def __init__(
        self,
        frame_ids: dict[str, str],
        *,
        indeterminate_first: bool = False,
        deny_first: bool = False,
    ) -> None:
        self.frame_ids = frame_ids
        self.indeterminate_first = indeterminate_first
        self.deny_first = deny_first
        self.dispatch_calls: list[ProviderDispatchRequest] = []
        self.reconcile_calls: list[ProviderReconcileQuery] = []

    def _completed(self, manifest_hash: str, request_id: str) -> ProviderCompleted:
        raw = json.dumps(
            {
                "schema_version": 1,
                "observations": [
                    {
                        "confidence": "0.91",
                        "kind": "change",
                        "proxy_interval": {
                            "end_pts": 60,
                            "start_pts": 40,
                            "uncertainty_pts": 2,
                        },
                        "summary": "Visible scene change.",
                        "supporting_frame_ids": [self.frame_ids[manifest_hash]],
                    }
                ],
            },
            separators=(",", ":"),
        ).encode()
        return ProviderCompleted(raw, request_id)

    def dispatch(self, request: ProviderDispatchRequest):
        self.dispatch_calls.append(request)
        manifest_hash = cast(str, json.loads(request.request_payload)["window_manifest_sha256"])
        request_id = f"provider-request-{len(self.dispatch_calls)}"
        if self.deny_first and len(self.dispatch_calls) == 1:
            return ProviderCompleted(
                b'{"schema_version":1,"observations":[]}',
                request_id,
            )
        if self.indeterminate_first:
            assert request.on_provider_request_id is not None
            request.on_provider_request_id(request_id)
            return ProviderIndeterminate("STREAM_INTERRUPTED", request_id)
        return self._completed(manifest_hash, request_id)

    def reconcile(self, query: ProviderReconcileQuery):
        self.reconcile_calls.append(query)
        attempt = next(
            item
            for item in self.dispatch_calls
            if item.provider_idempotency_key == query.provider_idempotency_key
        )
        manifest_hash = cast(str, json.loads(attempt.request_payload)["window_manifest_sha256"])
        assert query.provider_request_id is not None
        return self._completed(manifest_hash, query.provider_request_id)


def _profile() -> PipelineExecutionProfile:
    return PipelineExecutionProfile.from_doubao_policy(
        DoubaoVlmRequestPolicy(
            model_id="doubao-seed-2-1-pro-260628",
            parse_policy=VlmParsePolicy(Decimal("0.80"), 1_000_000, 4, 128, 512),
        ),
        retry_policy=GenerationRetryPolicy(
            GENERATION_RETRY_STRATEGY_VERSION,
            3,
            (2, 8),
        ),
    )


def _context(stage: str = "vlm", *, status: str = "running") -> PipelineStageContext:
    return PipelineStageContext(
        RUN_ID,
        PipelineRunRequest("test", source_reference="authorized-source"),
        PipelineCommand(
            "control-vlm-1",
            stage,
            status,  # type: ignore[arg-type]
            version=1,
            lease_id="lease-1" if status == "running" else None,
        ),
        _profile(),
    )


def _source_success() -> CommandOutcome:
    return CommandOutcome(
        uuid4(),
        "succeeded",
        receipt_id=uuid4(),
        artifact_set_id=uuid4(),
        job_id=uuid4(),
    )


def _stage(
    monkeypatch: pytest.MonkeyPatch,
    *,
    bundle: PersistedPreparedSources,
    blobs: dict[UUID, bytes],
    source_outcome: CommandOutcome | None,
    indeterminate_first: bool = False,
    deny_first: bool = False,
) -> tuple[VlmPipelineStage, KernelStore, Provider]:
    monkeypatch.setattr(
        "auto_cut_bot.pipeline.runtime.vlm_stage.read_persisted_prepared_sources_bundle",
        lambda *args, **kwargs: bundle,
    )
    store = KernelStore(source_outcome, blobs)
    frame_ids = {
        item.manifest.canonical_hash: item.manifest.frame_samples[0].frame_id
        for item in bundle.prepared.episodes
    }
    provider = Provider(
        frame_ids,
        indeterminate_first=indeterminate_first,
        deny_first=deny_first,
    )
    return VlmPipelineStage(store, provider), store, provider  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_wrong_stage_is_rejected_before_source_or_provider_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, blobs = _bundle()
    stage, _store, provider = _stage(
        monkeypatch,
        bundle=bundle,
        blobs=blobs,
        source_outcome=_source_success(),
    )

    with pytest.raises(PipelineRunValidationError, match="another stage"):
        await stage.execute(_context("source_prep"))
    assert provider.dispatch_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("source_outcome", [None, CommandOutcome(uuid4(), "running")])
async def test_missing_or_nonterminal_source_is_indeterminate_without_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    source_outcome: CommandOutcome | None,
) -> None:
    bundle, blobs = _bundle()
    stage, _store, provider = _stage(
        monkeypatch,
        bundle=bundle,
        blobs=blobs,
        source_outcome=source_outcome,
    )

    result = await stage.execute(_context())

    assert result.outcome == "indeterminate"
    assert provider.dispatch_calls == []


@pytest.mark.asyncio
async def test_legacy_execution_profile_is_refused_even_for_forged_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, blobs = _bundle()
    stage, _store, provider = _stage(
        monkeypatch,
        bundle=bundle,
        blobs=blobs,
        source_outcome=_source_success(),
    )
    context = _context()
    object.__setattr__(context, "execution_profile", PipelineExecutionProfile.legacy_unresolved())

    with pytest.raises(PipelineRunValidationError, match="legacy-unresolved"):
        await stage.execute(context)
    assert provider.dispatch_calls == []


@pytest.mark.asyncio
async def test_committed_bundle_dispatches_every_episode_then_projects_batch_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, blobs = _bundle(2)
    stage, store, provider = _stage(
        monkeypatch,
        bundle=bundle,
        blobs=blobs,
        source_outcome=_source_success(),
    )

    result = await stage.execute(_context())

    assert result.outcome == "succeeded"
    assert result.receipt_id is not None
    assert len(provider.dispatch_calls) == 2
    assert VLM_EPISODE_SELECTION_STRATEGY_VERSION == (
        "all-committed-episodes-sequential-v1"
    )
    aggregate = next(
        outcome
        for claim, outcome in store.claims.values()
        if claim.command_name == "FinalizeVlmBatchCommand"
    )
    assert result.receipt_id == aggregate.receipt_id


class CrashAfterAggregateReceipt:
    def __init__(self, delegate: FinalizeVlmBatchCommand) -> None:
        self.delegate = delegate
        self.crashed = False

    def execute(self, request):
        result = self.delegate.execute(request)
        if not self.crashed:
            self.crashed = True
            raise RuntimeError("crash after aggregate Kernel Receipt")
        return result


@pytest.mark.asyncio
async def test_crash_after_batch_receipt_reconciles_without_duplicate_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, blobs = _bundle()
    stage, store, provider = _stage(
        monkeypatch,
        bundle=bundle,
        blobs=blobs,
        source_outcome=_source_success(),
    )
    stage._finalizer = CrashAfterAggregateReceipt(  # pyright: ignore[reportPrivateUsage]
        FinalizeVlmBatchCommand(store)  # type: ignore[arg-type]
    )  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="aggregate Kernel Receipt"):
        await stage.execute(_context())
    recovered = await stage.reconcile(_context(status="indeterminate"))

    assert recovered is not None and recovered.outcome == "succeeded"
    assert len(provider.dispatch_calls) == 1
    assert provider.reconcile_calls == []


@pytest.mark.asyncio
async def test_known_provider_request_id_reconciles_same_attempt_without_redispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, blobs = _bundle()
    stage, _store, provider = _stage(
        monkeypatch,
        bundle=bundle,
        blobs=blobs,
        source_outcome=_source_success(),
        indeterminate_first=True,
    )

    first = await stage.execute(_context())
    recovered = await stage.reconcile(_context(status="indeterminate"))

    assert first.outcome == "indeterminate"
    assert recovered is not None and recovered.outcome == "succeeded"
    assert len(provider.dispatch_calls) == 1
    assert len(provider.reconcile_calls) == 1
    assert provider.reconcile_calls[0].provider_request_id == "provider-request-1"


@pytest.mark.asyncio
async def test_rejected_episode_short_circuits_later_dispatch_and_denies_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, blobs = _bundle(2)
    stage, store, provider = _stage(
        monkeypatch,
        bundle=bundle,
        blobs=blobs,
        source_outcome=_source_success(),
        deny_first=True,
    )

    result = await stage.execute(_context())

    assert result.outcome == "denied"
    assert result.receipt_id is not None
    assert len(provider.dispatch_calls) == 1
    assert all(
        claim.command_name != "FinalizeVlmBatchCommand"
        for claim, _outcome in store.claims.values()
    )
