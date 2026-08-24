"""Exact committed Source/Window/VLM reader integration tests."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
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
from autocut_kernel.pipeline import GenerateVlmEvidenceCommand, GenerateVlmEvidenceRequest
from autocut_kernel.store import (
    ArtifactMember,
    BlobRef,
    CommandClaim,
    CommandSuccess,
    CommittedArtifactMemberReference,
    CommittedSemanticInputsRequest,
    CommittedVlmInputReference,
    Job,
    JobProfileMismatchError,
    PostgresRuntimeStore,
    SemanticInputIntegrityError,
    SemanticInputUnavailableError,
    StoreValidationError,
)
from autocut_kernel.store.models import canonical_payload_hash, canonical_recipe_scope
from autocut_kernel.vlm import (
    GENERATION_RETRY_STRATEGY_VERSION,
    GenerationRetryPolicy,
    ProviderCompleted,
    ProviderReconcileQuery,
    ProviderResult,
    VlmParsePolicy,
    WindowFrameSample,
    WindowManifest,
    WindowManifestSet,
    WindowProxyBlobRef,
)
from autocut_kernel.vlm.provider_port import ProviderDispatchRequest
from autocut_kernel.vlm.window import ProxyTimelineMap

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("AUTOCUT_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(
    not DSN,
    reason="set AUTOCUT_TEST_POSTGRES_DSN to run disposable PostgreSQL tests",
)
MIGRATIONS = Path("packages/autocut-kernel/migrations")


def _digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _set_hash(members: tuple[ArtifactMember, ...]) -> str:
    canonical = [
        {
            "artifact_type": member.artifact_type,
            "content_hash": member.content_hash,
            "logical_id": member.logical_id,
            "payload_json": json.loads(member.payload_json),
            "revision": member.revision,
            "scope": {
                "key": member.scope.key,
                "kind": member.scope.kind,
                "namespace": member.scope.namespace,
            },
        }
        for member in members
    ]
    return _digest_bytes(
        json.dumps(canonical, separators=(",", ":"), sort_keys=True).encode()
    )


@pytest.fixture(autouse=True)
def migrated_database() -> None:
    assert DSN is not None
    with psycopg.connect(DSN, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA IF EXISTS storage CASCADE")
            cursor.execute("DROP SCHEMA IF EXISTS runtime CASCADE")
            for name in (
                "0001_runtime_core.sql",
                "0002_runtime_core_constraints.sql",
                "0003_vlm_generation_and_run_finalization.sql",
                "0004_provider_media_objects.sql",
                "0006_ark_provider_recovery.sql",
                "0009_vlm_bounded_retry.sql",
                "0011_generation_retry_schedule.sql",
            ):
                cursor.execute((MIGRATIONS / name).read_text())


class _Provider:
    def __init__(self, response: bytes) -> None:
        self.response = response
        self.calls = 0

    def dispatch(self, request: ProviderDispatchRequest) -> ProviderResult:
        del request
        self.calls += 1
        return ProviderCompleted(self.response, "doubao-response-1")

    def reconcile(self, query: ProviderReconcileQuery) -> ProviderResult:
        raise AssertionError(f"unexpected reconcile: {query}")


def _window(proxy_blob: BlobRef) -> tuple[WindowManifest, WindowManifestSet]:
    source_sha256 = "sha256:" + "a" * 64
    time_base = TimeBase(1, 1_000)
    context = EvidenceContext(
        "source-001",
        source_sha256,
        MediaKind.VIDEO,
        "clock-001",
        time_base,
        1_000,
        100,
        "fixture-decoder-v1",
        "sha256:" + "b" * 64,
    )
    coverage = Coverage(
        "source-001",
        source_sha256,
        "clock-001",
        time_base,
        1_000,
        1_100,
        CoverageOutcome.COMPLETE,
    )
    pts_index = PTSIndex((1_000, 1_010, 1_050, 1_090, 1_100))
    frame_pts = FramePtsIndexSet(
        "frame-pts-root-v1",
        context,
        coverage,
        pts_index,
        canonical_sha256(list(pts_index.ticks)),
    )
    manifest = WindowManifest(
        source_id="source-001",
        source_clock_id="clock-001",
        source_sha256=source_sha256,
        stream_index=0,
        source_time_base=time_base,
        source_range=TickRange(1_000, 1_100),
        core_range=TickRange(1_000, 1_100),
        frame_pts_index_set=frame_pts,
        proxy_blob_ref=WindowProxyBlobRef(
            str(proxy_blob.object_id),
            proxy_blob.content_hash,
            proxy_blob.byte_length,
            proxy_blob.media_type,
        ),
        preprocess_policy_sha256="sha256:" + "c" * 64,
        window_sampling_policy_sha256="sha256:" + "d" * 64,
        timeline_map=ProxyTimelineMap.translation(
            time_base=time_base,
            proxy_range=TickRange(0, 100),
            source_start_pts=1_000,
            max_source_error_pts=1,
        ),
        frame_samples=(
            WindowFrameSample(1_010, 10, "sha256:" + "e" * 64),
            WindowFrameSample(1_050, 50, "sha256:" + "f" * 64),
            WindowFrameSample(1_090, 90, "sha256:" + "1" * 64),
        ),
    )
    return manifest, WindowManifestSet(
        manifest.source_id,
        manifest.source_clock_id,
        manifest.source_sha256,
        manifest.stream_index,
        manifest.source_time_base,
        manifest.core_range,
        (manifest,),
    )


def _source_payload(
    manifest: WindowManifest,
    manifest_set: WindowManifestSet,
    proxy_blob: BlobRef,
) -> str:
    source = {
        "byte_size": 4_096,
        "content_sha256": manifest.source_sha256,
        "relative_path": "episode-001.mp4",
        "source_id": manifest.source_id,
    }
    census = {
        "authorization_id": "fixture-authority",
        "completion_policy": "all_or_nothing",
        "series_id": "fixture-series",
        "sources": [source],
    }
    payload = {
        "census": census,
        "census_sha256": canonical_sha256(census),
        "completion_policy": "all_or_nothing",
        "episodes": [
            {
                "media_probe": {"source": source},
                "proxy_blob": {
                    "byte_length": proxy_blob.byte_length,
                    "content_hash": proxy_blob.content_hash,
                    "media_type": proxy_blob.media_type,
                    "object_id": str(proxy_blob.object_id),
                },
                "window_manifest": manifest.to_mapping(),
                "window_manifest_set": manifest_set.to_mapping(),
            }
        ],
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _member_reference(
    member: ArtifactMember,
    *,
    receipt_id: object,
    artifact_set_id: object,
    ordinal: int,
) -> CommittedArtifactMemberReference:
    return CommittedArtifactMemberReference(
        receipt_id=UUID(str(receipt_id)),
        artifact_set_id=UUID(str(artifact_set_id)),
        member_ordinal=ordinal,
        scope=member.scope,
        artifact_type=member.artifact_type,
        logical_id=member.logical_id,
        revision=member.revision,
        content_hash=member.content_hash,
    )


def _source_provenance_sha256(
    job: Job,
    source_member: ArtifactMember,
    source_reference: CommittedArtifactMemberReference,
    job_id: object,
    command_slot_id: object,
) -> str:
    payload = {
        "artifact_reference": {
            "artifact_type": source_member.artifact_type,
            "content_hash": source_member.content_hash,
            "logical_id": source_member.logical_id,
            "revision": source_member.revision,
            "scope": {
                "key": source_member.scope.key,
                "kind": source_member.scope.kind,
                "namespace": source_member.scope.namespace,
            },
        },
        "artifact_set_id": str(source_reference.artifact_set_id),
        "command_slot_id": str(command_slot_id),
        "kernel_job_id": str(job_id),
        "receipt_id": str(source_reference.receipt_id),
        "source_job": {"job_key": job.job_key, "profile": job.profile},
    }
    return canonical_payload_hash(json.dumps(payload))


def _seed_committed_inputs(
    store: PostgresRuntimeStore,
    job: Job,
    *,
    source_revision: int = 1,
    source_manifest_sha256: str | None = None,
) -> tuple[CommittedSemanticInputsRequest, _Provider]:
    proxy = b"exact-source-proxy"
    proxy_blob = store.put_immutable_blob(
        job,
        content=proxy,
        content_hash=_digest_bytes(proxy),
        media_type="video/mp4",
    )
    manifest, manifest_set = _window(proxy_blob)
    source_payload = _source_payload(manifest, manifest_set, proxy_blob)
    source_member = ArtifactMember(
        "whole_series_source_manifest",
        "whole_series_source_manifest",
        source_revision,
        canonical_recipe_scope(job),
        canonical_payload_hash(source_payload),
        source_payload,
    )
    source_slot = store.claim_command(
        CommandClaim(
            job,
            f"source-{source_revision}",
            "PrepareWholeSeriesSourcesCommand",
            _digest_bytes(f"source-{source_revision}".encode()),
        )
    )
    source_outcome = store.commit_command_success(
        CommandSuccess(
            source_slot.command_slot_id,
            _set_hash((source_member,)),
            (source_member,),
        )
    )
    assert source_outcome.receipt_id is not None
    assert source_outcome.artifact_set_id is not None
    assert source_outcome.job_id is not None
    source_reference = _member_reference(
        source_member,
        receipt_id=source_outcome.receipt_id,
        artifact_set_id=source_outcome.artifact_set_id,
        ordinal=0,
    )
    source_provenance = _source_provenance_sha256(
        job,
        source_member,
        source_reference,
        source_outcome.job_id,
        source_slot.command_slot_id,
    )
    request = GenerateVlmEvidenceRequest(
        job=job,
        idempotency_key="doubao-window-001",
        artifact_scope=canonical_recipe_scope(job),
        artifact_revision=1,
        manifest=manifest,
        manifest_set=manifest_set,
        proxy_blob=proxy_blob,
        prompt_template="Describe visible story evidence only.",
        prompt_version="doubao-semantic-v1",
        response_schema_json='{"type":"object"}',
        request_parameters_json='{"temperature":"0"}',
        model_id="doubao-seed-test",
        provider_id="doubao-ark",
        parse_policy=VlmParsePolicy(Decimal("0.8"), 8_192, 4, 128, 256),
        retry_policy=GenerationRetryPolicy(
            GENERATION_RETRY_STRATEGY_VERSION,
            1,
            (),
        ),
        episode_index=0,
        source_manifest_sha256=(
            source_member.content_hash
            if source_manifest_sha256 is None
            else source_manifest_sha256
        ),
        source_provenance_sha256=source_provenance,
    )
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
                    "summary": "角色进入画面。",
                    "supporting_frame_ids": [manifest.frame_samples[1].frame_id],
                }
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    provider = _Provider(raw)
    result = GenerateVlmEvidenceCommand(store, provider).execute(request)
    assert result.outcome.receipt_id is not None
    assert result.outcome.artifact_set_id is not None
    assert result.attempt is not None
    assert result.attempt.raw_response is not None
    by_type = {member.artifact_type: member for member in result.artifacts}
    vlm_reference = CommittedVlmInputReference(
        request_record=_member_reference(
            by_type["vlm_request_record"],
            receipt_id=result.outcome.receipt_id,
            artifact_set_id=result.outcome.artifact_set_id,
            ordinal=0,
        ),
        response_record=_member_reference(
            by_type["vlm_response_record"],
            receipt_id=result.outcome.receipt_id,
            artifact_set_id=result.outcome.artifact_set_id,
            ordinal=1,
        ),
        observation_set=_member_reference(
            by_type["vlm_observation_set"],
            receipt_id=result.outcome.receipt_id,
            artifact_set_id=result.outcome.artifact_set_id,
            ordinal=2,
        ),
        proxy_blob=proxy_blob,
        request_payload=result.attempt.request_payload,
        raw_response=result.attempt.raw_response,
    )
    return (
        CommittedSemanticInputsRequest(
            job=job,
            source_manifest=source_reference,
            source_proxy_blobs=(proxy_blob,),
            vlm_inputs=(vlm_reference,),
        ),
        provider,
    )


def test_exact_reader_replays_owner_bound_source_window_and_vlm() -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    request, provider = _seed_committed_inputs(store, Job("semantic-reader", "test"))

    first = store.read_committed_semantic_inputs(request)
    restarted = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    replay = restarted.read_committed_semantic_inputs(request)

    assert replay == first
    assert provider.calls == 1
    assert first.source_manifest.reference.content_hash == request.source_manifest.content_hash
    assert first.inputs[0].source_window.window_manifest_sha256 == (
        first.inputs[0].request_identity.window_manifest_sha256
    )
    assert first.inputs[0].observations.source_child.artifact_set_id == (
        request.vlm_inputs[0].observation_set.artifact_set_id
    )
    assert "transcript" not in first.inputs[0].observations.payload_json.lower()
    assert "vad" not in first.inputs[0].observations.payload_json.lower()


@pytest.mark.parametrize(
    "tamper",
    [
        "receipt",
        "artifact_set",
        "ordinal",
        "scope",
        "revision",
        "type",
        "hash",
        "proxy_length",
        "proxy_media_type",
        "request_length",
        "raw_media_type",
    ],
)
def test_exact_reader_rejects_every_forged_member_or_blob_identity(tamper: str) -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    request, _provider = _seed_committed_inputs(store, Job(f"semantic-tamper-{tamper}", "test"))
    source = request.source_manifest
    vlm = request.vlm_inputs[0]
    if tamper == "receipt":
        source = replace(source, receipt_id=uuid4())
    elif tamper == "artifact_set":
        source = replace(source, artifact_set_id=uuid4())
    elif tamper == "ordinal":
        vlm = replace(vlm, observation_set=replace(vlm.observation_set, member_ordinal=1))
    elif tamper == "scope":
        source = replace(source, scope=replace(source.scope, key="other-job"))
    elif tamper == "revision":
        source = replace(source, revision=2)
    elif tamper == "type":
        source = replace(source, artifact_type="recipe")
    elif tamper == "hash":
        source = replace(source, content_hash="sha256:" + "0" * 64)
    elif tamper == "proxy_length":
        vlm = replace(vlm, proxy_blob=replace(vlm.proxy_blob, byte_length=999))
    elif tamper == "proxy_media_type":
        vlm = replace(vlm, proxy_blob=replace(vlm.proxy_blob, media_type="video/webm"))
    elif tamper == "request_length":
        vlm = replace(vlm, request_payload=replace(vlm.request_payload, byte_length=999))
    else:
        vlm = replace(vlm, raw_response=replace(vlm.raw_response, media_type="text/plain"))
    forged = replace(request, source_manifest=source, vlm_inputs=(vlm,))

    with pytest.raises((SemanticInputUnavailableError, SemanticInputIntegrityError)):
        store.read_committed_semantic_inputs(forged)


def test_exact_reader_rejects_vlm_owner_join_to_another_source_manifest() -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    request, _provider = _seed_committed_inputs(
        store,
        Job("semantic-owner-mismatch", "test"),
        source_manifest_sha256="sha256:" + "9" * 64,
    )

    with pytest.raises(SemanticInputIntegrityError, match="source|owner"):
        store.read_committed_semantic_inputs(request)


def test_typed_request_rejects_missing_duplicate_and_wrong_profile_inputs() -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    request, _provider = _seed_committed_inputs(
        store,
        Job("semantic-request-closed", "test"),
    )

    with pytest.raises(StoreValidationError, match="vlm_inputs"):
        replace(request, vlm_inputs=())
    with pytest.raises(StoreValidationError, match="unique"):
        replace(request, vlm_inputs=(request.vlm_inputs[0], request.vlm_inputs[0]))
    with pytest.raises(JobProfileMismatchError, match="profile"):
        store.read_committed_semantic_inputs(
            replace(request, job=Job(request.job.job_key, "production"))
        )


def test_exact_reader_keeps_prior_source_revision_after_head_advance() -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    job = Job("semantic-prior-revision", "test")
    first_request, _provider = _seed_committed_inputs(store, job)
    first = store.read_committed_semantic_inputs(first_request)
    source_blob = first_request.source_proxy_blobs[0]
    manifest, manifest_set = _window(source_blob)
    second_payload = _source_payload(manifest, manifest_set, source_blob)
    second = ArtifactMember(
        "whole_series_source_manifest",
        "whole_series_source_manifest",
        2,
        canonical_recipe_scope(job),
        canonical_payload_hash(second_payload),
        second_payload,
    )
    second_slot = store.claim_command(
        CommandClaim(job, "source-2", "PrepareWholeSeriesSourcesCommand", _digest_bytes(b"source-2"))
    )
    store.commit_command_success(
        CommandSuccess(second_slot.command_slot_id, _set_hash((second,)), (second,))
    )

    replay = store.read_committed_semantic_inputs(first_request)

    assert replay == first
    assert replay.source_manifest.reference.revision == 1
