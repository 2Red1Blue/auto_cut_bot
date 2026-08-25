"""Exact committed Source/Window/VLM reader integration tests."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from autocut_kernel.media import (
    AudioSampleBoundarySet,
    AudioSourceOutcome,
    Coverage,
    CoverageOutcome,
    EvidenceContext,
    MediaKind,
    PTSIndex,
    TickRange,
    TimeBase,
)
from autocut_kernel.media.ffprobe_port import ProbeResult
from autocut_kernel.media.types import ToolEvidence, VideoStreamEvidence, canonical_sha256
from autocut_kernel.pipeline import GenerateVlmEvidenceCommand, GenerateVlmEvidenceRequest
from autocut_kernel.source_manifest import SourceOperationPolicy, decode_source_manifest
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
    parse_vlm_response,
)
from autocut_kernel.vlm.provider_port import ProviderDispatchRequest
from autocut_kernel.vlm.window import ProxyTimelineMap

from auto_cut_bot.pipeline.source_prep.command import _identity_frame_index, _identity_policy
from auto_cut_bot.pipeline.source_prep.models import SeriesSource
from auto_cut_bot.pipeline.source_prep.probe import SourceMediaProbe

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
        json.dumps(
            canonical,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
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
    def __init__(self, response: bytes, request_id: str = "doubao-response-1") -> None:
        self.response = response
        self.request_id = request_id
        self.calls = 0

    def dispatch(self, request: ProviderDispatchRequest) -> ProviderResult:
        del request
        self.calls += 1
        return ProviderCompleted(self.response, self.request_id)

    def reconcile(self, query: ProviderReconcileQuery) -> ProviderResult:
        raise AssertionError(f"unexpected reconcile: {query}")


def _window(
    proxy_blob: BlobRef,
    episode_index: int = 0,
) -> tuple[WindowManifest, WindowManifestSet, SourceMediaProbe]:
    ordinal = episode_index + 1
    source_id = f"source-{ordinal:03d}"
    source_sha256 = "sha256:" + ("a" if episode_index == 0 else "5") * 64
    audio_clock_id = f"audio-clock-{episode_index}"
    time_base = TimeBase(1, 1_000)
    source = SeriesSource(
        f"episode-{ordinal:03d}.mp4",
        source_id,
        source_sha256,
        4_096,
    )
    video_range = TickRange(1_000, 1_100)
    video_probe = ProbeResult(
        VideoStreamEvidence(0, "h264", 1_920, 1_080, time_base),
        PTSIndex((1_000, 1_010, 1_050, 1_090)),
        ToolEvidence("ffprobe", "fixture-v1", "sha256:" + "b" * 64),
    )
    audio_context = EvidenceContext(
        source_id,
        source_sha256,
        MediaKind.AUDIO,
        audio_clock_id,
        time_base,
        1_000,
        100,
        "fixture-audio-v1",
        "sha256:" + "2" * 64,
    )
    audio_coverage = Coverage(
        source_id,
        source_sha256,
        audio_clock_id,
        time_base,
        1_000,
        1_100,
        CoverageOutcome.COMPLETE,
    )
    probe = SourceMediaProbe(
        source,
        video_probe,
        video_range,
        AudioSampleBoundarySet(
            "audio-none-v1",
            audio_context,
            audio_coverage,
            AudioSourceOutcome.NOT_APPLICABLE,
            (),
        ),
        "sha256:" + "3" * 64,
        "sha256:" + "4" * 64,
    )
    frame_pts = _identity_frame_index(probe, _identity_policy())
    manifest = WindowManifest(
        source_id=source_id,
        source_clock_id="video-stream-0",
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
    ), probe


def _source_payload(
    manifest: WindowManifest,
    manifest_set: WindowManifestSet,
    proxy_blob: BlobRef,
    probe: SourceMediaProbe,
) -> str:
    return _series_source_payload(((manifest, manifest_set, proxy_blob, probe),))


def _series_source_payload(
    episodes: tuple[
        tuple[WindowManifest, WindowManifestSet, BlobRef, SourceMediaProbe], ...
    ],
) -> str:
    sources = [probe.source.to_mapping() for _manifest, _set, _blob, probe in episodes]
    policy = SourceOperationPolicy(
        authorization_id="fixture-authority",
        series_id="fixture-series",
        expected_source_count=len(sources),
        authorized_purposes=("semantic_analysis", "render_source"),
    )
    census = {
        "authorization_id": policy.authorization_id,
        "authorization_policy_schema_version": policy.schema_version,
        "authorization_policy_sha256": policy.policy_sha256,
        "authorized_purposes": list(policy.authorized_purposes),
        "completion_policy": "all_or_nothing",
        "expected_source_count": policy.expected_source_count,
        "series_id": policy.series_id,
        "sources": sources,
    }
    payload = {
        "census": census,
        "census_sha256": canonical_sha256(census),
        "completion_policy": "all_or_nothing",
        "episodes": [
            {
                "media_probe": probe.to_mapping(),
                "proxy_blob": {
                    "byte_length": proxy_blob.byte_length,
                    "content_hash": proxy_blob.content_hash,
                    "media_type": proxy_blob.media_type,
                    "object_id": str(proxy_blob.object_id),
                },
                "window_manifest": manifest.to_mapping(),
                "window_manifest_set": manifest_set.to_mapping(),
            }
            for manifest, manifest_set, proxy_blob, probe in episodes
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


def _forge_committed_artifact_payload(
    reference: CommittedArtifactMemberReference,
    payload: dict[str, object],
) -> CommittedArtifactMemberReference:
    """Simulate a storage attacker that recomputes member and ArtifactSet hashes."""

    assert DSN is not None
    payload_json = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    content_hash = canonical_payload_hash(payload_json)
    with psycopg.connect(DSN, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("ALTER TABLE runtime.artifacts DISABLE TRIGGER USER")
            cursor.execute("ALTER TABLE runtime.artifact_sets DISABLE TRIGGER USER")
            try:
                cursor.execute(
                    """
                    UPDATE runtime.artifacts
                       SET payload_json = %s::jsonb, content_hash = %s
                     WHERE artifact_set_id = %s
                       AND artifact_type = %s
                       AND logical_id = %s
                    """,
                    (
                        payload_json,
                        content_hash,
                        reference.artifact_set_id,
                        reference.artifact_type,
                        reference.logical_id,
                    ),
                )
                assert cursor.rowcount == 1
                cursor.execute(
                    """
                    SELECT artifact.artifact_type, artifact.logical_id,
                           artifact.revision, artifact.namespace,
                           artifact.scope_kind, artifact.scope_key,
                           artifact.content_hash, artifact.payload_json::text
                      FROM runtime.artifact_set_members AS member
                      JOIN runtime.artifacts AS artifact
                        ON artifact.artifact_id = member.artifact_id
                     WHERE member.artifact_set_id = %s
                     ORDER BY member.ordinal
                    """,
                    (reference.artifact_set_id,),
                )
                members = tuple(
                    ArtifactMember(
                        str(row[0]),
                        str(row[1]),
                        int(row[2]),
                        replace(reference.scope, namespace=str(row[3]), kind=str(row[4]), key=str(row[5])),
                        str(row[6]),
                        str(row[7]),
                    )
                    for row in cursor.fetchall()
                )
                cursor.execute(
                    "UPDATE runtime.artifact_sets SET set_hash = %s WHERE artifact_set_id = %s",
                    (_set_hash(members), reference.artifact_set_id),
                )
                assert cursor.rowcount == 1
            finally:
                cursor.execute("ALTER TABLE runtime.artifacts ENABLE TRIGGER USER")
                cursor.execute("ALTER TABLE runtime.artifact_sets ENABLE TRIGGER USER")
    return replace(reference, content_hash=content_hash)


def _read_committed_artifact_payload(
    reference: CommittedArtifactMemberReference,
) -> dict[str, object]:
    assert DSN is not None
    with psycopg.connect(DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT payload_json
                  FROM runtime.artifacts
                 WHERE artifact_set_id = %s
                   AND artifact_type = %s
                   AND logical_id = %s
                """,
                (
                    reference.artifact_set_id,
                    reference.artifact_type,
                    reference.logical_id,
                ),
            )
            row = cursor.fetchone()
    assert row is not None
    return dict(row[0])


def _rehash_source_grant(payload: dict[str, object], *, policy: bool = True) -> None:
    census = payload["census"]
    assert isinstance(census, dict)
    if policy:
        policy_mapping = {
            "authorization_id": census["authorization_id"],
            "authorized_purposes": census["authorized_purposes"],
            "expected_source_count": census["expected_source_count"],
            "schema_version": census["authorization_policy_schema_version"],
            "series_id": census["series_id"],
        }
        census["authorization_policy_sha256"] = canonical_sha256(policy_mapping)
    payload["census_sha256"] = canonical_sha256(census)


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
    manifest, manifest_set, probe = _window(proxy_blob)
    source_payload = _source_payload(manifest, manifest_set, proxy_blob, probe)
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
        parse_policy=VlmParsePolicy(
            max_response_bytes=64_000,
            max_entities=8,
            max_facts=16,
            max_events=16,
            max_candidate_hypotheses=8,
            max_temporal_segments=8,
            max_measurements=16,
            max_text_characters=512,
            max_total_text_characters=8_192,
        ),
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
    support = {
        "confidence": "0.91",
        "proxy_interval": {
            "end_pts": 60,
            "start_pts": 40,
            "uncertainty_pts": 2,
        },
        "supporting_frame_ids": [manifest.frame_samples[1].frame_id],
    }
    raw = json.dumps(
        {
            "schema_version": 3,
            "window_summary": {
                "summary": "角色进入画面。",
                "dominant_temporal_mode": "present",
                "fact_refs": ["fact_1"],
                "event_refs": [],
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
                    "entity_kind": "person",
                    "display_label": "Visible person",
                    "visual_description": "A person entering the frame.",
                    "support": support,
                }
            ],
            "facts": [
                {
                    "local_fact_id": "fact_1",
                    "fact_kind": "visible_action",
                    "subject_ref": "entity_1",
                    "object_ref": None,
                    "summary": "角色进入画面。",
                    "support": support,
                }
            ],
            "events": [],
            "candidate_hypotheses": [],
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
        semantic_pack=_member_reference(
            by_type["vlm_semantic_pack"],
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


def _seed_two_window_inputs(
    store: PostgresRuntimeStore,
    job: Job,
    *,
    second_from_previous: bool = True,
    second_state_summary: str = "角色持续进入画面。",
) -> CommittedSemanticInputsRequest:
    proxy_blobs = tuple(
        store.put_immutable_blob(
            job,
            content=f"exact-source-proxy-{index}".encode(),
            content_hash=_digest_bytes(f"exact-source-proxy-{index}".encode()),
            media_type="video/mp4",
        )
        for index in range(2)
    )
    window_values = tuple(
        _window(proxy_blob, index)
        for index, proxy_blob in enumerate(proxy_blobs)
    )
    windows = tuple(
        (manifest, manifest_set, proxy_blob, probe)
        for proxy_blob, (manifest, manifest_set, probe) in zip(
            proxy_blobs,
            window_values,
            strict=True,
        )
    )
    source_payload = _series_source_payload(windows)
    source_member = ArtifactMember(
        "whole_series_source_manifest",
        "whole_series_source_manifest",
        1,
        canonical_recipe_scope(job),
        canonical_payload_hash(source_payload),
        source_payload,
    )
    source_slot = store.claim_command(
        CommandClaim(
            job,
            "source-series",
            "PrepareWholeSeriesSourcesCommand",
            _digest_bytes(b"source-series"),
        )
    )
    source_outcome = store.commit_command_success(
        CommandSuccess(source_slot.command_slot_id, _set_hash((source_member,)), (source_member,))
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
    vlm_references: list[CommittedVlmInputReference] = []
    for episode_index, (manifest, manifest_set, proxy_blob, _probe) in enumerate(windows):
        state_summary = (
            "角色持续进入画面。" if episode_index == 0 else second_state_summary
        )
        support = {
            "confidence": "0.91",
            "proxy_interval": {
                "end_pts": 60,
                "start_pts": 40,
                "uncertainty_pts": 2,
            },
            "supporting_frame_ids": [manifest.frame_samples[1].frame_id],
        }
        continues_from_previous = episode_index == 1 and second_from_previous
        continues_into_next = episode_index == 0
        raw = json.dumps(
            {
                "schema_version": 3,
                "window_summary": {
                    "summary": state_summary,
                    "dominant_temporal_mode": "present",
                    "fact_refs": ["fact_1"],
                    "event_refs": [],
                    "confidence": "0.91",
                },
                "continuity": {
                    "starts_mid_event": continues_from_previous,
                    "ends_mid_event": continues_into_next,
                    "continues_from_previous": continues_from_previous,
                    "continues_into_next": continues_into_next,
                    "entry_state_fact_refs": (
                        ["fact_1"] if continues_from_previous else []
                    ),
                    "exit_state_fact_refs": (
                        ["fact_1"] if continues_into_next else []
                    ),
                    "temporal_segments": [],
                },
                "entities": [
                    {
                        "local_entity_id": "entity_1",
                        "entity_kind": "person",
                        "display_label": "Visible person",
                        "visual_description": "A person entering the frame.",
                        "support": support,
                    }
                ],
                "facts": [
                    {
                        "local_fact_id": "fact_1",
                        "fact_kind": "visible_action",
                        "subject_ref": "entity_1",
                        "object_ref": None,
                        "summary": state_summary,
                        "support": support,
                    }
                ],
                "events": [],
                "candidate_hypotheses": [],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        generation_request = GenerateVlmEvidenceRequest(
            job=job,
            idempotency_key=f"doubao-window-{episode_index}",
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
            parse_policy=VlmParsePolicy(
                max_response_bytes=64_000,
                max_entities=8,
                max_facts=16,
                max_events=16,
                max_candidate_hypotheses=8,
                max_temporal_segments=8,
                max_measurements=16,
                max_text_characters=512,
                max_total_text_characters=8_192,
            ),
            retry_policy=GenerationRetryPolicy(
                GENERATION_RETRY_STRATEGY_VERSION,
                1,
                (),
            ),
            episode_index=episode_index,
            source_manifest_sha256=source_member.content_hash,
            source_provenance_sha256=source_provenance,
        )
        result = GenerateVlmEvidenceCommand(
            store,
            _Provider(raw, f"doubao-response-{episode_index}"),
        ).execute(generation_request)
        assert result.outcome.receipt_id is not None
        assert result.outcome.artifact_set_id is not None
        assert result.attempt is not None
        assert result.attempt.raw_response is not None
        by_type = {member.artifact_type: member for member in result.artifacts}
        vlm_references.append(
            CommittedVlmInputReference(
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
                semantic_pack=_member_reference(
                    by_type["vlm_semantic_pack"],
                    receipt_id=result.outcome.receipt_id,
                    artifact_set_id=result.outcome.artifact_set_id,
                    ordinal=2,
                ),
                proxy_blob=proxy_blob,
                request_payload=result.attempt.request_payload,
                raw_response=result.attempt.raw_response,
            )
        )
    return CommittedSemanticInputsRequest(
        job=job,
        source_manifest=source_reference,
        source_proxy_blobs=proxy_blobs,
        vlm_inputs=tuple(vlm_references),
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
    assert first.inputs[0].semantic_pack.source_child.artifact_set_id == (
        request.vlm_inputs[0].semantic_pack.artifact_set_id
    )
    assert first.inputs[0].semantic_pack.semantic_pack.candidate_hypotheses == ()
    assert "transcript" not in first.inputs[0].semantic_pack.payload_json.lower()
    assert "vad" not in first.inputs[0].semantic_pack.payload_json.lower()


def test_source_reader_restart_rebuilds_exact_typed_operation_grant() -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    request, _provider = _seed_committed_inputs(
        store,
        Job("source-grant-restart", "test"),
    )

    persisted = PostgresRuntimeStore(
        lambda: psycopg.connect(DSN)
    ).read_whole_series_source_manifest(
        request.job,
        request.source_manifest.artifact_set_id,
    )
    decoded = decode_source_manifest(persisted.payload_json, persisted.proxy_blobs)

    decoded.census.require_purpose("semantic_analysis")
    decoded.census.require_purpose("render_source")
    assert decoded.census.policy.authorization_id == "fixture-authority"
    assert decoded.census.policy.series_id == "fixture-series"
    assert decoded.census.policy.policy_sha256 == (
        json.loads(persisted.payload_json)["census"][
            "authorization_policy_sha256"
        ]
    )
    assert decoded.census.sources[0].source_id == (
        decoded.episodes[0].manifest.source_id
    )


@pytest.mark.parametrize(
    "attack",
    [
        "legacy-no-grant",
        "missing-purpose",
        "empty-purpose",
        "unknown-purpose",
        "duplicate-purpose",
        "unordered-purpose",
        "policy-hash",
        "delete-source",
        "add-source",
        "source-id",
        "source-hash",
    ],
)
def test_source_and_semantic_readers_reject_rehashed_grant_attacks(
    attack: str,
) -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    request, _provider = _seed_committed_inputs(
        store,
        Job(f"source-grant-attack-{attack}", "test"),
    )
    payload = _read_committed_artifact_payload(request.source_manifest)
    census = payload["census"]
    assert isinstance(census, dict)
    sources = census["sources"]
    assert isinstance(sources, list)
    assert isinstance(sources[0], dict)

    if attack == "legacy-no-grant":
        legacy = {
            "authorization_id": census["authorization_id"],
            "completion_policy": census["completion_policy"],
            "series_id": census["series_id"],
            "sources": sources,
        }
        payload["census"] = legacy
        payload["census_sha256"] = canonical_sha256(legacy)
    elif attack == "missing-purpose":
        del census["authorized_purposes"]
        _rehash_source_grant(payload, policy=False)
    elif attack == "empty-purpose":
        census["authorized_purposes"] = []
        _rehash_source_grant(payload)
    elif attack == "unknown-purpose":
        census["authorized_purposes"] = ["semantic_analysis", "unknown"]
        _rehash_source_grant(payload)
    elif attack == "duplicate-purpose":
        census["authorized_purposes"] = [
            "semantic_analysis",
            "semantic_analysis",
        ]
        _rehash_source_grant(payload)
    elif attack == "unordered-purpose":
        census["authorized_purposes"] = ["render_source", "semantic_analysis"]
        _rehash_source_grant(payload)
    elif attack == "policy-hash":
        census["authorization_policy_sha256"] = "sha256:" + "0" * 64
        _rehash_source_grant(payload, policy=False)
    elif attack == "delete-source":
        sources.clear()
        _rehash_source_grant(payload)
    elif attack == "add-source":
        added = dict(sources[0])
        added["relative_path"] = "episode-999.mp4"
        added["source_id"] = "source-999"
        added["content_sha256"] = "sha256:" + "9" * 64
        sources.append(added)
        _rehash_source_grant(payload)
    elif attack == "source-id":
        sources[0]["source_id"] = "source-forged"
        _rehash_source_grant(payload)
    else:
        sources[0]["content_sha256"] = "sha256:" + "9" * 64
        _rehash_source_grant(payload)

    forged_reference = _forge_committed_artifact_payload(
        request.source_manifest,
        payload,
    )
    forged_request = replace(request, source_manifest=forged_reference)
    restarted = PostgresRuntimeStore(lambda: psycopg.connect(DSN))

    with pytest.raises(StoreValidationError, match="canonical source-prep"):
        restarted.read_whole_series_source_manifest(
            request.job,
            request.source_manifest.artifact_set_id,
        )
    with pytest.raises(SemanticInputIntegrityError, match="Source/Window"):
        restarted.read_committed_semantic_inputs(forged_request)


def test_source_and_semantic_readers_reject_rehashed_cross_source_identity_hash_swap() -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    request = _seed_two_window_inputs(
        store,
        Job("source-grant-cross-source-swap", "test"),
    )
    payload = _read_committed_artifact_payload(request.source_manifest)
    census = payload["census"]
    assert isinstance(census, dict)
    sources = census["sources"]
    assert isinstance(sources, list) and len(sources) == 2
    assert isinstance(sources[0], dict) and isinstance(sources[1], dict)
    sources[0]["source_id"], sources[1]["source_id"] = (
        sources[1]["source_id"],
        sources[0]["source_id"],
    )
    sources[0]["content_sha256"], sources[1]["content_sha256"] = (
        sources[1]["content_sha256"],
        sources[0]["content_sha256"],
    )
    _rehash_source_grant(payload)
    forged_reference = _forge_committed_artifact_payload(
        request.source_manifest,
        payload,
    )
    forged_request = replace(request, source_manifest=forged_reference)

    with pytest.raises(StoreValidationError, match="canonical source-prep"):
        store.read_whole_series_source_manifest(
            request.job,
            request.source_manifest.artifact_set_id,
        )
    with pytest.raises(SemanticInputIntegrityError, match="Source/Window"):
        store.read_committed_semantic_inputs(forged_request)


def test_exact_reader_rejects_self_consistent_persisted_pack_forged_from_raw() -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    request, provider = _seed_committed_inputs(
        store,
        Job("semantic-forged-pack", "test"),
    )
    committed = store.read_committed_semantic_inputs(request)
    manifest, manifest_set, _probe = _window(request.source_proxy_blobs[0])
    altered_raw = json.loads(provider.response)
    altered_raw["window_summary"]["summary"] = "另一个可见动作。"
    altered_raw["facts"][0]["summary"] = "另一个可见动作。"
    forged_pack = parse_vlm_response(
        json.dumps(altered_raw, ensure_ascii=False, separators=(",", ":")).encode(),
        manifest=manifest,
        manifest_set=manifest_set,
        request_identity=committed.inputs[0].request_identity,
        policy=VlmParsePolicy(
            max_response_bytes=64_000,
            max_entities=8,
            max_facts=16,
            max_events=16,
            max_candidate_hypotheses=8,
            max_temporal_segments=8,
            max_measurements=16,
            max_text_characters=512,
            max_total_text_characters=8_192,
        ),
    )
    forged_reference = _forge_committed_artifact_payload(
        request.vlm_inputs[0].semantic_pack,
        forged_pack.to_mapping(),
    )
    forged_request = replace(
        request,
        vlm_inputs=(
            replace(request.vlm_inputs[0], semantic_pack=forged_reference),
        ),
    )

    with pytest.raises(SemanticInputIntegrityError, match="member/blob/owner"):
        store.read_committed_semantic_inputs(forged_request)


@pytest.mark.parametrize(
    ("tamper_path", "replacement"),
    [
        (("episodes", 0, "media_probe", "source", "source_id"), "other-source"),
        (("episodes", 0, "window_manifest", "frame_samples", 0, "source_pts"), 1_011),
        (
            (
                "episodes",
                0,
                "window_manifest",
                "timeline_map",
                "segments",
                0,
                "source_range",
                "start_pts",
            ),
            1_001,
        ),
    ],
    ids=("nested-owner", "nested-frame", "nested-mapping"),
)
def test_exact_reader_rejects_forged_nested_source_window_mapping(
    tamper_path: tuple[str | int, ...],
    replacement: object,
) -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    request, _provider = _seed_committed_inputs(
        store,
        Job(f"semantic-nested-{tamper_path[-1]}", "test"),
    )
    with psycopg.connect(DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT payload_json
                  FROM runtime.artifacts
                 WHERE artifact_set_id = %s AND artifact_type = 'whole_series_source_manifest'
                """,
                (request.source_manifest.artifact_set_id,),
            )
            row = cursor.fetchone()
            assert row is not None
            payload = dict(row[0])
    owner: object = payload
    for key in tamper_path[:-1]:
        owner = owner[key]  # type: ignore[index]
    owner[tamper_path[-1]] = replacement  # type: ignore[index]
    forged_source = _forge_committed_artifact_payload(request.source_manifest, payload)

    with pytest.raises(SemanticInputIntegrityError, match="Source/Window"):
        store.read_committed_semantic_inputs(
            replace(request, source_manifest=forged_source)
        )


def test_exact_reader_accepts_verifiably_continuous_adjacent_packs() -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    request = _seed_two_window_inputs(
        store,
        Job("semantic-continuity-valid", "test"),
    )

    committed = store.read_committed_semantic_inputs(request)

    assert tuple(item.source_window.source_id for item in committed.inputs) == (
        "source-001",
        "source-002",
    )
    assert committed.inputs[0].semantic_pack.semantic_pack.continuity.continues_into_next
    assert committed.inputs[1].semantic_pack.semantic_pack.continuity.continues_from_previous


@pytest.mark.parametrize(
    ("second_from_previous", "second_state_summary", "match"),
    [
        (False, "角色持续进入画面。", "flags"),
        (True, "完全不同的可见状态。", "state facts"),
    ],
    ids=("flag-mismatch", "state-mismatch"),
)
def test_exact_reader_rejects_inconsistent_adjacent_pack_continuity(
    second_from_previous: bool,
    second_state_summary: str,
    match: str,
) -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    request = _seed_two_window_inputs(
        store,
        Job(f"semantic-continuity-{match}", "test"),
        second_from_previous=second_from_previous,
        second_state_summary=second_state_summary,
    )

    with pytest.raises(SemanticInputIntegrityError, match=match):
        store.read_committed_semantic_inputs(request)


@pytest.mark.parametrize(
    "tamper",
    [
        "receipt",
        "artifact_set",
        "ordinal",
        "vlm_v2_type",
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
        vlm = replace(vlm, semantic_pack=replace(vlm.semantic_pack, member_ordinal=1))
    elif tamper == "vlm_v2_type":
        vlm = replace(
            vlm,
            semantic_pack=replace(
                vlm.semantic_pack,
                artifact_type="vlm_observation_set",
            ),
        )
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
    manifest, manifest_set, probe = _window(source_blob)
    second_payload = _source_payload(manifest, manifest_set, source_blob, probe)
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
