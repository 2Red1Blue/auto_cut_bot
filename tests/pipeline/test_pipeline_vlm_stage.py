from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
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
from autocut_kernel.pipeline import (
    FinalizeVlmBatchCommand,
    VlmBatchRequestPolicyMismatchError,
)
from autocut_kernel.store import (
    BlobRef,
    CommandClaim,
    CommandOutcome,
    CommandRejection,
    CommandSuccess,
    CommittedArtifactMemberReference,
    CommittedVlmInputReference,
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

from auto_cut_bot.pipeline.runtime.errors import (
    PipelineRunValidationError,
    PipelineStageIsolationError,
)
from auto_cut_bot.pipeline.runtime.models import (
    EvidenceReadLimits,
    PipelineCommand,
    PipelineExecutionProfile,
    PipelineRunRequest,
    PipelineStageContext,
    VlmFullStageRecomputeRequest,
)
from auto_cut_bot.pipeline.runtime.source_prep_stage import (
    source_prep_kernel_idempotency_key,
)
from auto_cut_bot.pipeline.runtime.vlm_stage import (
    VLM_EPISODE_MAX_CONCURRENCY,
    VLM_EPISODE_SELECTION_STRATEGY_VERSION,
    VlmPipelineStage,
    vlm_batch_kernel_idempotency_key,
    vlm_kernel_idempotency_key,
)
from auto_cut_bot.pipeline.source_prep import (
    PersistedPreparedSources,
    PreparedSeriesSources,
    PreparedSourceEpisode,
    SourceOperationPolicy,
    SourceOperationPurpose,
)
from auto_cut_bot.pipeline.source_prep.models import SeriesSource, SeriesSourceCensus
from auto_cut_bot.pipeline.source_prep.probe import SourceMediaProbe
from auto_cut_bot.pipeline.vlm import DoubaoVlmRequestPolicy
from tests.pipeline.runtime_profile_fixture import (
    media_preflight_policy,
    stage1_command_policy,
    stage2_command_policy,
    stage3_command_policy,
)

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


def _bundle(
    count: int = 1,
    *,
    authorization_id: str = "authorized-source",
    authorized_purposes: tuple[SourceOperationPurpose, ...] = (
        "semantic_analysis",
        "render_source",
    ),
) -> tuple[PersistedPreparedSources, dict[UUID, bytes]]:
    values = tuple(_episode(index) for index in range(count))
    job = Job(RUN_ID, "test")
    prepared = PreparedSeriesSources(
        SeriesSourceCensus(
            SourceOperationPolicy(
                authorization_id,
                "series-001",
                count,
                authorized_purposes,
            ),
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

    def claim_vlm_batch_command(self, claim: CommandClaim) -> CommandOutcome:
        return self.claim_command(claim)

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

    def read_committed_vlm_input_reference(
        self,
        job: Job,
        idempotency_key: str,
    ) -> CommittedVlmInputReference:
        child = self.read_committed_vlm_generation_child(job, idempotency_key)
        success = self.generation_successes[child.command_slot_id]
        raw_response = self.attempts[child.command_slot_id].raw_response
        assert raw_response is not None
        references = tuple(
            CommittedArtifactMemberReference(
                receipt_id=child.receipt_id,
                artifact_set_id=child.artifact_set_id,
                member_ordinal=ordinal,
                scope=artifact.scope,
                artifact_type=artifact.artifact_type,
                logical_id=artifact.logical_id,
                revision=artifact.revision,
                content_hash=artifact.content_hash,
            )
            for ordinal, artifact in enumerate(success.artifacts)
        )
        assert len(references) == 3
        request_record = json.loads(success.artifacts[0].payload_json)
        proxy_mapping = request_record["proxy_blob"]
        proxy_blob = BlobRef(
            UUID(proxy_mapping["object_id"]),
            proxy_mapping["content_hash"],
            proxy_mapping["byte_length"],
            proxy_mapping["media_type"],
        )
        return CommittedVlmInputReference(
            request_record=references[0],
            response_record=references[1],
            semantic_pack=references[2],
            proxy_blob=proxy_blob,
            request_payload=child.request_payload,
            raw_response=raw_response,
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

    def commit_vlm_batch_success(self, success: CommandSuccess) -> CommandOutcome:
        return self.commit_command_success(success)

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
        support = {
            "confidence": "0.91",
            "proxy_interval": {
                "end_pts": 60,
                "start_pts": 40,
                "uncertainty_pts": 2,
            },
            "supporting_frame_ids": [self.frame_ids[manifest_hash]],
        }
        raw = json.dumps(
            {
                "schema_version": 3,
                "window_summary": {
                    "summary": "Visible scene change.",
                    "dominant_temporal_mode": "present",
                    "fact_refs": ["fact_1"],
                    "event_refs": ["event_1"],
                    "confidence": "0.91",
                },
                "continuity": {
                    "starts_mid_event": False,
                    "ends_mid_event": False,
                    "continues_from_previous": False,
                    "continues_into_next": False,
                    "entry_state_fact_refs": [],
                    "exit_state_fact_refs": [],
                    "temporal_segments": [],
                },
                "entities": [
                    {
                        "local_entity_id": "entity_1",
                        "entity_kind": "object",
                        "display_label": "Visible subject",
                        "visual_description": "A visible subject in the scene.",
                        "support": support,
                    }
                ],
                "facts": [
                    {
                        "local_fact_id": "fact_1",
                        "fact_kind": "visible_change",
                        "subject_ref": "entity_1",
                        "object_ref": None,
                        "summary": "The visible scene changes.",
                        "support": support,
                    }
                ],
                "events": [
                    {
                        "local_event_id": "event_1",
                        "event_kind": "transition",
                        "summary": "Visible scene change.",
                        "participant_refs": ["entity_1"],
                        "fact_refs": ["fact_1"],
                        "cause_event_refs": [],
                        "effect_event_refs": [],
                        "open_question": None,
                        "temporal_mode": "present",
                        "support": support,
                    }
                ],
                "candidate_hypotheses": [
                    {
                        "local_candidate_id": "candidate_1",
                        "candidate_kind": "highlight",
                        "anchor_event_ref": "event_1",
                        "supporting_event_refs": ["event_1"],
                        "context_event_refs": [],
                        "payoff_event_refs": ["event_1"],
                        "open_question": None,
                        "reason": "The visible transition is a concrete highlight.",
                        "anchor_summary": "The scene visibly changes.",
                        "payoff_or_open_question": "The transition completes visibly.",
                        "dialogue_excerpt": None,
                        "editing_modes": ["action"],
                        "narrative_functions": ["payoff"],
                        "tags": ["action"],
                        "measurements": [
                            {
                                "measurement_kind": "visual_salience",
                                "value": "0.91",
                                "confidence": "0.91",
                                "fact_refs": ["fact_1"],
                                "event_refs": ["event_1"],
                            }
                        ],
                        "support": support,
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
                b'{"schema_version":3,"candidate_hypotheses":[]}',
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
    from autocut_kernel.store.models import MaterializationLimits

    return PipelineExecutionProfile.from_policies(
        DoubaoVlmRequestPolicy(
            model_id="doubao-seed-2-1-pro-260628",
            parse_policy=VlmParsePolicy(
                max_response_bytes=1_000_000,
                max_entities=8,
                max_facts=16,
                max_events=8,
                max_candidate_hypotheses=4,
                max_temporal_segments=8,
                max_measurements=16,
                max_text_characters=512,
                max_total_text_characters=8_192,
            ),
        ),
        media_preflight_policy(),
        retry_policy=GenerationRetryPolicy(
            GENERATION_RETRY_STRATEGY_VERSION,
            3,
            (2, 8),
        ),
        materialization_limits=MaterializationLimits(
            max_source_bytes=8 * 1024 * 1024,
            timed_speech_max_request_bytes=8 * 1024 * 1024,
            copy_chunk_bytes=64 * 1024,
            staging_quota_bytes=16 * 1024 * 1024,
        ),
        stage1_policy=stage1_command_policy(),
        stage2_policy=stage2_command_policy(),
        stage3_policy=stage3_command_policy(),
        evidence_read_limits=EvidenceReadLimits(100_000, 500_000),
    )


def test_vlm_context_rejects_historical_v3_execution_profile() -> None:
    mapping = _profile().to_mapping()
    mapping["schema_version"] = "pipeline-execution-profile-v3"
    del mapping["materialization_limits"]
    del mapping["stage1_command_policy"]
    del mapping["stage2_command_policy"]
    del mapping["stage3_command_policy"]
    del mapping["evidence_read_limits"]
    mapping["parse_policy"] = {
        "max_observations": 64,
        "max_response_bytes": 64_000,
        "max_summary_characters": 512,
        "max_total_summary_characters": 8_192,
        "minimum_confidence": "0.80",
    }
    historical = PipelineExecutionProfile.from_mapping(mapping)

    with pytest.raises(PipelineRunValidationError, match="current execution profile"):
        PipelineStageContext(
            RUN_ID,
            PipelineRunRequest("test", source_reference="authorized-source"),
            PipelineCommand("control-vlm-history", "vlm", "pending"),
            historical,
        )
    with pytest.raises(PipelineRunValidationError, match="read-only"):
        historical.to_doubao_policy()


def _context(
    stage: str = "vlm",
    *,
    status: str = "running",
    recompute_request: VlmFullStageRecomputeRequest | None = None,
) -> PipelineStageContext:
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
        recompute_request,
    )


def _source_success() -> CommandOutcome:
    return CommandOutcome(
        uuid4(),
        "succeeded",
        receipt_id=uuid4(),
        artifact_set_id=uuid4(),
        job_id=uuid4(),
    )


def _replace_census_sources(
    bundle: PersistedPreparedSources,
    sources: tuple[SeriesSource, ...],
) -> PersistedPreparedSources:
    prepared = replace(
        bundle.prepared,
        census=SeriesSourceCensus(
            bundle.prepared.census.policy,
            "all_or_nothing",
            sources,
        ),
    )
    payload_json = json.dumps(
        prepared.to_mapping(), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    return replace(
        bundle,
        prepared=prepared,
        artifact_reference=replace(
            bundle.artifact_reference,
            content_hash=canonical_payload_hash(payload_json),
        ),
    )


def _replace_source_policy(
    bundle: PersistedPreparedSources,
    policy: SourceOperationPolicy,
) -> PersistedPreparedSources:
    prepared = replace(
        bundle.prepared,
        census=SeriesSourceCensus(
            policy,
            "all_or_nothing",
            bundle.prepared.census.sources,
        ),
    )
    payload_json = json.dumps(
        prepared.to_mapping(), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    return replace(
        bundle,
        prepared=prepared,
        artifact_reference=replace(
            bundle.artifact_reference,
            content_hash=canonical_payload_hash(payload_json),
        ),
    )


def test_source_grant_changes_flow_into_manifest_provenance_and_vlm_identity() -> None:
    bundle, _blobs = _bundle()
    semantic_only = _replace_source_policy(
        bundle,
        SourceOperationPolicy(
            "authorized-source",
            "series-001",
            1,
            ("semantic_analysis",),
        ),
    )
    policy = _profile().to_doubao_policy()

    assert semantic_only.artifact_reference.content_hash != (bundle.artifact_reference.content_hash)
    assert semantic_only.canonical_hash != bundle.canonical_hash
    assert vlm_kernel_idempotency_key(
        run_id=RUN_ID,
        episode_index=0,
        source_bundle=semantic_only,
        policy=policy,
        execution_profile_hash=_profile().canonical_hash,
    ) != vlm_kernel_idempotency_key(
        run_id=RUN_ID,
        episode_index=0,
        source_bundle=bundle,
        policy=policy,
        execution_profile_hash=_profile().canonical_hash,
    )


def _stage(
    monkeypatch: pytest.MonkeyPatch,
    *,
    bundle: PersistedPreparedSources,
    blobs: dict[UUID, bytes],
    source_outcome: CommandOutcome | None,
    indeterminate_first: bool = False,
    deny_first: bool = False,
    stop_after_probe: bool = False,
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
    return (
        VlmPipelineStage(  # type: ignore[arg-type]
            store,
            provider,
            stop_after_probe=stop_after_probe,
        ),
        store,
        provider,
    )


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
@pytest.mark.parametrize("operation", ["execute", "reconcile"])
async def test_source_grant_without_semantic_analysis_blocks_batch_before_claim(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    bundle, blobs = _bundle(authorized_purposes=("render_source",))
    stage, store, provider = _stage(
        monkeypatch,
        bundle=bundle,
        blobs=blobs,
        source_outcome=_source_success(),
    )

    with pytest.raises(PipelineRunValidationError, match="semantic_analysis"):
        if operation == "execute":
            await stage.execute(_context(status="indeterminate"))
        else:
            await stage.reconcile(_context(status="indeterminate"))

    assert store.claims == {}
    assert provider.dispatch_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["execute", "reconcile"])
async def test_source_grant_membership_mismatch_blocks_batch_before_claim(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    bundle, blobs = _bundle()
    source = bundle.prepared.census.sources[0]
    forged = replace(source, source_id="source-not-owned-by-episode")
    mismatched_bundle = _replace_census_sources(bundle, (forged,))
    stage, store, provider = _stage(
        monkeypatch,
        bundle=mismatched_bundle,
        blobs=blobs,
        source_outcome=_source_success(),
    )

    with pytest.raises(PipelineRunValidationError, match="do not match"):
        if operation == "execute":
            await stage.execute(_context(status="indeterminate"))
        else:
            await stage.reconcile(_context(status="indeterminate"))

    assert store.claims == {}
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
    assert VLM_EPISODE_SELECTION_STRATEGY_VERSION == "probe-first-then-bounded-parallel-v3"
    assert VLM_EPISODE_MAX_CONCURRENCY == 10
    aggregate = next(
        (claim, outcome)
        for claim, outcome in store.claims.values()
        if claim.command_name == "FinalizeVlmBatchCommand"
    )
    assert result.receipt_id == aggregate[1].receipt_id
    assert aggregate[0].idempotency_key == vlm_batch_kernel_idempotency_key(
        run_id=RUN_ID,
        source_bundle=bundle,
        policy=_context().execution_profile.to_doubao_policy(),
        execution_profile_hash=_context().execution_profile_hash,
    )


@pytest.mark.asyncio
async def test_selected_only_recompute_dispatches_one_episode_without_batch_finalizer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, blobs = _bundle(3)
    stage, store, provider = _stage(
        monkeypatch,
        bundle=bundle,
        blobs=blobs,
        source_outcome=_source_success(),
    )
    selected = VlmFullStageRecomputeRequest(
        "pipeline_run_" + "b" * 32,
        5,
        completion_scope="selected_only",
        episode_numbers=(2,),
    )

    result = await stage.execute(_context(recompute_request=selected))

    assert result.outcome == "succeeded"
    assert len(provider.dispatch_calls) == 1
    payload = json.loads(provider.dispatch_calls[0].request_payload)
    assert payload["window_manifest_sha256"] == (
        bundle.prepared.episodes[1].manifest.canonical_hash
    )
    assert not any(
        claim.command_name == "FinalizeVlmBatchCommand" for claim, _outcome in store.claims.values()
    )


@pytest.mark.asyncio
async def test_selected_only_recompute_finalizes_when_selection_covers_entire_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, blobs = _bundle(1)
    stage, store, provider = _stage(
        monkeypatch,
        bundle=bundle,
        blobs=blobs,
        source_outcome=_source_success(),
    )
    selected = VlmFullStageRecomputeRequest(
        "pipeline_run_" + "b" * 32,
        5,
        completion_scope="selected_only",
        episode_numbers=(1,),
    )

    result = await stage.execute(_context(recompute_request=selected))

    assert result.outcome == "succeeded"
    assert len(provider.dispatch_calls) == 1
    aggregate_claims = [
        (claim, outcome)
        for claim, outcome in store.claims.values()
        if claim.command_name == "FinalizeVlmBatchCommand"
    ]
    assert len(aggregate_claims) == 1
    assert result.receipt_id == aggregate_claims[0][1].receipt_id


@pytest.mark.asyncio
async def test_selected_only_recompute_rejects_episode_outside_source_before_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, blobs = _bundle(2)
    stage, _store, provider = _stage(
        monkeypatch,
        bundle=bundle,
        blobs=blobs,
        source_outcome=_source_success(),
    )
    selected = VlmFullStageRecomputeRequest(
        "pipeline_run_" + "b" * 32,
        5,
        completion_scope="selected_only",
        episode_numbers=(3,),
    )

    with pytest.raises(PipelineRunValidationError, match="outside the committed source census"):
        await stage.execute(_context(recompute_request=selected))
    assert provider.dispatch_calls == []


@pytest.mark.asyncio
async def test_probe_inspection_runs_the_real_first_episode_then_holds_before_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, blobs = _bundle(2)
    stage, store, provider = _stage(
        monkeypatch,
        bundle=bundle,
        blobs=blobs,
        source_outcome=_source_success(),
        stop_after_probe=True,
    )

    first = await stage.execute(_context())
    recovered = await stage.reconcile(_context(status="indeterminate"))

    assert first.outcome == "indeterminate"
    assert recovered is None
    assert len(provider.dispatch_calls) == 1
    assert json.loads(provider.dispatch_calls[0].request_payload)["window_manifest_sha256"] == (
        bundle.prepared.episodes[0].manifest.canonical_hash
    )
    assert not any(
        claim.command_name == "FinalizeVlmBatchCommand" for claim, _outcome in store.claims.values()
    )

    # Restarting without the operational hold reconciles the same persisted
    # command. The first episode is reused by its Kernel idempotency key; only
    # the remaining episode can cause a second provider dispatch.
    continued = VlmPipelineStage(store, provider)  # type: ignore[arg-type]
    completed = await continued.reconcile(_context(status="indeterminate"))

    assert completed is not None and completed.outcome == "succeeded"
    assert len(provider.dispatch_calls) == 2


class _ProbeThenParallelCommand:
    """Thread-safe probe/parallel scheduling check, not a Kernel substitute."""

    def __init__(self, expected_concurrency: int) -> None:
        self._expected_concurrency = expected_concurrency
        self._lock = threading.Lock()
        self.probe_started = threading.Event()
        self._release_probe = threading.Event()
        self._parallel_started = threading.Event()
        self.active = 0
        self.max_active = 0
        self.requests: list[object] = []

    def release_probe(self) -> None:
        self._release_probe.set()

    def execute(self, request: object) -> object:
        with self._lock:
            self.requests.append(request)
            request_count = len(self.requests)
            if request_count == 1:
                self.probe_started.set()
        if request_count == 1:
            assert self._release_probe.wait(timeout=2)
            return SimpleNamespace(
                outcome=CommandOutcome(
                    uuid4(),
                    "succeeded",
                    receipt_id=uuid4(),
                    artifact_set_id=uuid4(),
                    job_id=uuid4(),
                )
            )
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            if request_count == self._expected_concurrency + 1:
                self._parallel_started.set()
        assert self._parallel_started.wait(timeout=2)
        with self._lock:
            self.active -= 1
        return SimpleNamespace(
            outcome=CommandOutcome(
                uuid4(),
                "succeeded",
                receipt_id=uuid4(),
                artifact_set_id=uuid4(),
                job_id=uuid4(),
            )
        )


class _ParallelBatchFinalizer:
    def __init__(self) -> None:
        self.requests: list[object] = []

    def execute(self, request: object) -> object:
        self.requests.append(request)
        return SimpleNamespace(
            outcome=CommandOutcome(
                uuid4(),
                "succeeded",
                receipt_id=uuid4(),
                artifact_set_id=uuid4(),
                job_id=uuid4(),
            )
        )


@pytest.mark.asyncio
async def test_current_policy_requires_a_successful_probe_before_ten_episode_parallel_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, blobs = _bundle(VLM_EPISODE_MAX_CONCURRENCY + 1)
    stage, _store, _provider = _stage(
        monkeypatch,
        bundle=bundle,
        blobs=blobs,
        source_outcome=_source_success(),
    )
    command = _ProbeThenParallelCommand(VLM_EPISODE_MAX_CONCURRENCY)
    finalizer = _ParallelBatchFinalizer()
    stage._command = command  # type: ignore[assignment,reportPrivateUsage]
    stage._finalizer = finalizer  # type: ignore[assignment,reportPrivateUsage]

    execution = asyncio.create_task(stage.execute(_context()))
    assert await asyncio.to_thread(command.probe_started.wait, 2)
    # Until a full terminal success is returned, no sibling provider command
    # has escaped from the batch adapter.
    assert len(command.requests) == 1
    assert command.requests[0].episode_index == 0
    command.release_probe()
    result = await execution

    assert result.outcome == "succeeded"
    assert len(command.requests) == VLM_EPISODE_MAX_CONCURRENCY + 1
    assert command.max_active == VLM_EPISODE_MAX_CONCURRENCY
    assert len(finalizer.requests) == 1


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


class IncompatiblePersistedBatchFinalizer:
    def execute(self, request):
        raise VlmBatchRequestPolicyMismatchError(
            declared_episode_count=request.declared_episode_count,
            ordered_policy_hashes=("sha256:" + "1" * 64, "sha256:" + "2" * 64),
        )


@pytest.mark.asyncio
async def test_incompatible_persisted_batch_isolated_without_provider_redispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, blobs = _bundle(2)
    stage, _store, provider = _stage(
        monkeypatch,
        bundle=bundle,
        blobs=blobs,
        source_outcome=_source_success(),
    )
    stage._finalizer = IncompatiblePersistedBatchFinalizer()  # type: ignore[assignment]

    with pytest.raises(PipelineStageIsolationError) as first:
        await stage.execute(_context())
    dispatch_count = len(provider.dispatch_calls)
    with pytest.raises(PipelineStageIsolationError) as replay:
        await stage.reconcile(_context(status="indeterminate"))

    assert dispatch_count == 2
    assert len(provider.dispatch_calls) == dispatch_count
    assert first.value.failure_code == "VLM_BATCH_CHILD_REQUEST_POLICY_MISMATCH"
    assert replay.value.failure_detail == first.value.failure_detail


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
async def test_invalid_episode_starts_bounded_retry_and_blocks_later_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, blobs = _bundle(VLM_EPISODE_MAX_CONCURRENCY + 1)
    stage, store, provider = _stage(
        monkeypatch,
        bundle=bundle,
        blobs=blobs,
        source_outcome=_source_success(),
        deny_first=True,
    )

    result = await stage.execute(_context())

    assert result.outcome == "indeterminate"
    assert result.receipt_id is None
    # A failed probe reserves the bounded retry path; no sibling or later
    # batch has escaped before its durable retry is reconciled.
    assert len(provider.dispatch_calls) == 1
    assert all(
        claim.command_name != "FinalizeVlmBatchCommand" for claim, _outcome in store.claims.values()
    )
