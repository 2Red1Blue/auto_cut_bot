"""Real disposable-PostgreSQL v4 closure; media/provider I/O is fixture-only."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
from autocut_kernel.context_pack import ContextSelectionPolicy, video_only_window_context_pack
from autocut_kernel.media import V23CandidateWindowCompilePolicy
from autocut_kernel.pipeline import (
    CompileV23CandidateDecisionSetCommand,
    CompileV23CandidateDecisionSetRequest,
    FinalizeVlmBatchCommand,
    FinalizeVlmBatchRequest,
    GenerateVlmEvidenceCommand,
    GenerateVlmEvidenceRequest,
    VlmBatchChildOutcome,
    read_committed_v23_candidate_decision_set,
)
from autocut_kernel.store import (
    ArtifactMember,
    CommandClaim,
    CommandSuccess,
    CommittedArtifactMemberReference,
    CommittedSemanticInputsRequest,
    IdempotencyConflictError,
    Job,
    PersistedVlmSemanticPackV4,
    PostgresRuntimeStore,
    StoreValidationError,
)
from autocut_kernel.store.models import (
    artifact_set_hash,
    canonical_payload_hash,
    canonical_recipe_scope,
)
from autocut_kernel.store.vlm_v4 import VLM_BATCH_FINALIZER_STRATEGY_VERSION_V4
from autocut_kernel.vlm import GenerationRetryPolicy, ProviderCompleted, VlmParsePolicy
from autocut_kernel.vlm.semantic_contracts import parser_contract_sha256_for
from autocut_kernel.vlm.semantic_pack_v4 import VlmSemanticPackV4

from auto_cut_bot.pipeline.source_prep import (
    PrepareWholeSeriesSourcesCommand,
    PrepareWholeSeriesSourcesRequest,
)
from tests.pipeline.test_whole_series_source_prep_command import (
    SyntheticLayoutProbe,
    SyntheticSampleBuilder,
    _source_root,
)
from tests.vlm.test_semantic_pack_v4 import _wire

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("AUTOCUT_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="requires an explicit disposable PostgreSQL database")


class FixtureProvider:
    def __init__(self, raw: bytes) -> None:
        self.raw = raw
        self.calls = 0

    def dispatch(self, _request):
        self.calls += 1
        return ProviderCompleted(self.raw, "fixture-provider-response")

    def reconcile(self, _query):
        raise AssertionError("completed fixture calls must not reconcile")


class NoProvider:
    def dispatch(self, _request):
        raise AssertionError("replay must not invoke a provider")

    def reconcile(self, _query):
        raise AssertionError("replay must not invoke a provider")


@dataclass
class Prepared:
    store: PostgresRuntimeStore
    request: GenerateVlmEvidenceRequest
    source_reference: CommittedArtifactMemberReference
    source_root: Path


@pytest.fixture
def prepared(tmp_path: Path) -> Prepared:
    assert DSN is not None
    from psycopg.conninfo import conninfo_to_dict

    database = conninfo_to_dict(DSN).get("dbname", "")
    if not any(database.startswith(prefix) and len(database) > len(prefix)
               for prefix in ("autocut_test_", "autocut_resume_check_")):
        pytest.fail("schema-reset tests require a dedicated autocut_test_* or autocut_resume_check_* database")
    with psycopg.connect(DSN, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute("DROP SCHEMA IF EXISTS runtime CASCADE")
        cursor.execute("DROP SCHEMA IF EXISTS storage CASCADE")
        for migration in sorted(Path("packages/autocut-kernel/migrations").glob("*.sql")):
            cursor.execute(migration.read_text(encoding="utf-8"))
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    job = Job("v4-persisted-source-owner", "test")
    root = tmp_path / "sources"
    root.mkdir()
    (root / "episode.mp4").write_bytes(b"synthetic media bytes; no native media or remote provider")
    result = PrepareWholeSeriesSourcesCommand(
        store, builder=SyntheticSampleBuilder(SyntheticLayoutProbe()),
    ).execute(PrepareWholeSeriesSourcesRequest(
        job, "source-prep", canonical_recipe_scope(job), 1, _source_root(root),
    ))
    assert result.outcome.state == "succeeded" and result.prepared is not None
    assert result.outcome.receipt_id is not None and result.outcome.artifact_set_id is not None
    source = store.read_whole_series_source_manifest(job, result.outcome.artifact_set_id)
    episode = result.prepared.episodes[0]
    request = GenerateVlmEvidenceRequest(
        job=job, idempotency_key="vlm-child-v4", artifact_scope=canonical_recipe_scope(job),
        artifact_revision=1, manifest=episode.manifest, manifest_set=episode.manifest_set,
        proxy_blob=episode.proxy_blob, prompt_template="Describe only visible video observations.",
        prompt_version="fixture-video-support-v4", response_schema_json=json.dumps({
            "type": "object", "properties": {"schema_version": {"const": 4}},
        }),
        request_parameters_json='{"temperature":"0"}', model_id="fixture-model",
        provider_id="fixture-provider", parse_policy=VlmParsePolicy(
            max_response_bytes=100_000, max_entities=10, max_facts=100, max_events=100,
            max_candidate_hypotheses=10, max_temporal_segments=10, max_measurements=100,
            max_text_characters=1_000, max_total_text_characters=10_000,
        ),
        retry_policy=GenerationRetryPolicy("generation-retry-v1", 1, ()),
        parser_strategy_version="strict-semantic-pack-v4",
        parser_contract_sha256=parser_contract_sha256_for("strict-semantic-pack-v4"),
        source_manifest_sha256=source.reference.content_hash,
        source_provenance_sha256=source.canonical_hash,
    )
    return Prepared(
        store,
        request,
        CommittedArtifactMemberReference(
            source.receipt_id,
            source.artifact_set_id,
            0,
            source.reference.scope,
            source.reference.artifact_type,
            source.reference.logical_id,
            source.reference.revision,
            source.reference.content_hash,
        ),
        root,
    )


def _raw() -> bytes:
    return json.dumps(_wire(), ensure_ascii=False).encode("utf-8")


def _batch(prepared: Prepared) -> FinalizeVlmBatchRequest:
    request = prepared.request
    child = prepared.store.read_committed_vlm_generation_child(request.job, request.idempotency_key)
    return FinalizeVlmBatchRequest(
        request.job, "vlm-batch:v4", request.artifact_scope, 1, 1,
        child.source_manifest_sha256, child.source_provenance_sha256,
        (VlmBatchChildOutcome(
            child.episode_index, child.idempotency_key, child.window_manifest_sha256,
            child.source_manifest_sha256, child.source_provenance_sha256, child.request_hash,
            "succeeded", child.receipt_id, child.artifact_set_id,
        ),),
        strategy_version=VLM_BATCH_FINALIZER_STRATEGY_VERSION_V4,
    )


def _history(job: Job):
    assert DSN is not None
    with psycopg.connect(DSN) as connection, connection.cursor() as cursor:
        cursor.execute("""
            SELECT to_jsonb(job)::text,
                   (SELECT jsonb_agg(to_jsonb(slot) ORDER BY slot.command_slot_id)::text
                      FROM runtime.command_slots slot WHERE slot.job_id = job.job_id),
                   (SELECT jsonb_agg(to_jsonb(receipt) ORDER BY receipt.receipt_id)::text
                      FROM runtime.command_receipts receipt JOIN runtime.command_slots slot USING (command_slot_id)
                     WHERE slot.job_id = job.job_id),
                   (SELECT jsonb_agg(to_jsonb(artifact) ORDER BY artifact.artifact_id)::text
                      FROM runtime.artifacts artifact WHERE artifact.job_id = job.job_id)
              FROM runtime.jobs job WHERE job.job_key = %s
        """, (job.job_key,))
        return cursor.fetchone()


def test_source_v4_generation_batch_reopen_and_zero_provider_replay(prepared: Prepared) -> None:
    provider = FixtureProvider(_raw())
    first = GenerateVlmEvidenceCommand(prepared.store, provider).execute(prepared.request)
    assert first.outcome.state == "succeeded" and type(first.semantic_pack) is VlmSemanticPackV4
    batch_request = _batch(prepared)
    batch = FinalizeVlmBatchCommand(prepared.store).execute(batch_request)
    assert batch.outcome.state == "succeeded" and batch.artifact is not None
    payload = json.loads(batch.artifact.payload_json)
    assert payload["schema_version"] == 4
    assert payload["parser_strategy_version"] == "strict-semantic-pack-v4"
    assert payload["strategy_version"] == VLM_BATCH_FINALIZER_STRATEGY_VERSION_V4
    before = _history(prepared.request.job)
    restarted = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    child = restarted.read_committed_vlm_generation_child(prepared.request.job, prepared.request.idempotency_key)
    assert child.semantic_schema_version == 4 and child.parser_strategy_version == "strict-semantic-pack-v4"
    reference = restarted.read_committed_vlm_input_reference(prepared.request.job, prepared.request.idempotency_key)
    assert restarted.read_immutable_blob(prepared.request.job, reference.raw_response) == _raw()
    aggregate = restarted.read_committed_vlm_semantic_pack_set_reference(prepared.request.job, batch_request.idempotency_key)
    assert aggregate.content_hash == batch.artifact.content_hash
    replay = GenerateVlmEvidenceCommand(restarted, NoProvider()).execute(prepared.request)
    assert replay.outcome.receipt_id == first.outcome.receipt_id and replay.semantic_pack == first.semantic_pack
    assert FinalizeVlmBatchCommand(restarted).execute(batch_request).outcome.receipt_id == batch.outcome.receipt_id
    assert provider.calls == 1 and _history(prepared.request.job) == before
    committed = restarted.read_committed_semantic_inputs(CommittedSemanticInputsRequest(
        prepared.request.job, prepared.source_reference, aggregate,
    ))
    assert committed.vlm_batch_strategy_version == VLM_BATCH_FINALIZER_STRATEGY_VERSION_V4
    assert len(committed.inputs) == 1
    assert type(committed.inputs[0].semantic_pack) is PersistedVlmSemanticPackV4
    assert type(committed.inputs[0].semantic_pack.semantic_pack) is VlmSemanticPackV4



def test_v23_decision_set_commits_reopens_and_replays_without_provider(
    prepared: Prepared,
) -> None:
    """A V23 compatibility decision is an exact derived Artifact, not process memory."""

    provider = FixtureProvider(_raw())
    child_result = GenerateVlmEvidenceCommand(prepared.store, provider).execute(
        prepared.request
    )
    assert child_result.outcome.state == "succeeded"
    batch_request = _batch(prepared)
    batch = FinalizeVlmBatchCommand(prepared.store).execute(batch_request)
    assert batch.outcome.state == "succeeded"

    aggregate = prepared.store.read_committed_vlm_semantic_pack_set_reference(
        prepared.request.job, batch_request.idempotency_key
    )
    semantic_request = CommittedSemanticInputsRequest(
        prepared.request.job,
        prepared.source_reference,
        aggregate,
    )
    semantic = prepared.store.read_committed_semantic_inputs(semantic_request)
    item = semantic.inputs[0]
    persisted_pack = item.semantic_pack
    assert type(persisted_pack) is PersistedVlmSemanticPackV4
    source_time_base = (
        persisted_pack.semantic_pack.events[0].support.manifest.source_time_base
    )
    compile_request = CompileV23CandidateDecisionSetRequest(
        job=prepared.request.job,
        idempotency_key="v23-decision-set:episode-0",
        artifact_scope=canonical_recipe_scope(prepared.request.job),
        artifact_revision=1,
        semantic_inputs_request=semantic_request,
        episode_index=0,
        window_manifest_sha256=item.source_window.window_manifest_sha256,
        semantic_pack_sha256=persisted_pack.semantic_pack.canonical_hash,
        vlm_request_identity_sha256=item.request_identity.canonical_hash,
        compile_policy=V23CandidateWindowCompilePolicy(
            strategy_version="v23-direct-event-window-v1",
            time_base=source_time_base,
            initial_left_expansion_pts=5,
            initial_right_expansion_pts=5,
            max_direct_event_gap_pts=10,
            max_seed_duration_pts=5_000,
            max_source_coverage_ppm=1_000_000,
        ),
        max_payload_bytes=1_000_000,
    )

    compiled = CompileV23CandidateDecisionSetCommand(prepared.store).execute(
        compile_request
    )
    assert compiled.outcome.state == "succeeded"
    assert compiled.committed is not None
    assert provider.calls == 1
    before_replay = _history(prepared.request.job)

    restarted = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    replay = CompileV23CandidateDecisionSetCommand(restarted).execute(compile_request)
    assert replay.outcome.state == "succeeded"
    assert replay.outcome.receipt_id == compiled.outcome.receipt_id
    assert replay.committed is None
    reopened = read_committed_v23_candidate_decision_set(
        restarted, compile_request, replay.outcome
    )
    assert reopened.value == compiled.committed.value
    assert reopened.record == compiled.committed.record
    assert _history(prepared.request.job) == before_replay
    assert provider.calls == 1

    changed_policy = replace(
        compile_request,
        compile_policy=replace(
            compile_request.compile_policy,
            initial_left_expansion_pts=6,
        ),
    )
    with pytest.raises(IdempotencyConflictError):
        CompileV23CandidateDecisionSetCommand(restarted).execute(changed_policy)
    assert _history(prepared.request.job) == before_replay


def test_context_bound_v4_child_reopens_and_finalizes(prepared: Prepared) -> None:
    """A fresh consumer finalizes a Pack-bound child after the source path vanishes."""

    request = replace(
        prepared.request,
        idempotency_key="vlm-child-v4-context",
        context_pack=video_only_window_context_pack(
            ContextSelectionPolicy(), "metadata_unconfigured"
        ),
    )
    provider = FixtureProvider(_raw())
    result = GenerateVlmEvidenceCommand(prepared.store, provider).execute(request)
    assert result.outcome.state == "succeeded"
    contextual = replace(prepared, request=request)
    before = _history(request.job)
    # A different machine need not mount the original video path after SourcePrep
    # committed the proxy and source-manifest blobs.  Replay must remain a pure
    # durable read and must not obtain another provider result.
    shutil.rmtree(prepared.source_root)
    restarted = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    replay = GenerateVlmEvidenceCommand(restarted, NoProvider()).execute(request)
    assert replay.outcome.receipt_id == result.outcome.receipt_id
    batch = FinalizeVlmBatchCommand(restarted).execute(_batch(replace(contextual, store=restarted)))
    assert batch.outcome.state == "succeeded" and batch.artifact is not None
    reopened = restarted.read_committed_vlm_generation_child(request.job, request.idempotency_key)
    assert reopened.request_hash == request.request_hash
    assert provider.calls == 1 and _history(request.job) != before


class ForgedPackStore(PostgresRuntimeStore):
    """Inject a forged artifact through normal generic commit, never SQL mutation."""

    def __init__(self, mutation: str) -> None:
        super().__init__(lambda: psycopg.connect(DSN))
        self.mutation = mutation

    def commit_generation_success(self, attempt_id, *, expected_version, success):
        original = success.artifacts[2]
        payload: dict[str, Any] = json.loads(original.payload_json)
        if self.mutation == "schema":
            payload["schema_version"] = 3
        else:
            # It remains structurally valid, with the same claimed original raw hash.
            payload["window_summary"]["summary"] = "A different observation never returned by the provider."
        serialized = json.dumps(payload, ensure_ascii=False)
        forged = replace(original, payload_json=serialized, content_hash=canonical_payload_hash(serialized))
        members = (*success.artifacts[:2], forged)
        return super().commit_generation_success(
            attempt_id, expected_version=expected_version,
            success=CommandSuccess(success.command_slot_id, artifact_set_hash(members), members),
        )


@pytest.mark.parametrize("mutation", ["schema", "claimed_raw_hash"])
def test_reader_rejects_version_spoof_and_self_claimed_raw_provenance(
    prepared: Prepared, mutation: str,
) -> None:
    forged_store = ForgedPackStore(mutation)
    result = GenerateVlmEvidenceCommand(forged_store, FixtureProvider(_raw())).execute(prepared.request)
    assert result.outcome.state == "succeeded"  # Generic transaction does not attest semantics.
    before = _history(prepared.request.job)
    with pytest.raises(StoreValidationError, match="schema disagree|exact v4 verification") as caught:
        prepared.store.read_committed_vlm_generation_child(prepared.request.job, prepared.request.idempotency_key)
    if mutation == "claimed_raw_hash":
        assert "exact raw-response reparse" in str(caught.value.__cause__)
    assert _history(prepared.request.job) == before


@pytest.mark.parametrize("missing", ["raw_bytes", "raw_claim"])
def test_reader_cannot_bypass_missing_raw_blob_access(prepared: Prepared, missing: str) -> None:
    result = GenerateVlmEvidenceCommand(prepared.store, FixtureProvider(_raw())).execute(prepared.request)
    assert result.attempt is not None and result.attempt.raw_response is not None
    target = result.attempt.raw_response.object_id

    class MissingRawCursor(psycopg.Cursor):
        def execute(self, query, params=None, **kwargs):
            selector = "SELECT content_bytes" if missing == "raw_bytes" else "JOIN storage.blob_claims"
            self.hide = selector in str(query) and target in (params or ())
            return super().execute(query, params, **kwargs)

        def fetchone(self):
            return None if self.hide else super().fetchone()

    inaccessible = PostgresRuntimeStore(lambda: psycopg.connect(DSN, cursor_factory=MissingRawCursor))
    before = _history(prepared.request.job)
    from autocut_kernel.store import BlobIntegrityError

    with pytest.raises((StoreValidationError, BlobIntegrityError)):
        inaccessible.read_committed_vlm_generation_child(prepared.request.job, prepared.request.idempotency_key)
    assert _history(prepared.request.job) == before


@pytest.mark.parametrize("owner", ["unknown_provenance", "another_job"])
def test_reader_requires_exact_same_job_committed_source_owner(prepared: Prepared, owner: str) -> None:
    request = prepared.request
    if owner == "unknown_provenance":
        request = replace(request, source_provenance_sha256="sha256:" + "1" * 64)
    else:
        other = Job("v4-unrelated-consumer", "test")
        content = prepared.store.read_immutable_blob(request.job, request.proxy_blob)
        claimed = prepared.store.put_immutable_blob(
            other, content=content, content_hash="sha256:" + hashlib.sha256(content).hexdigest(),
            media_type=request.proxy_blob.media_type,
        )
        assert claimed == request.proxy_blob
        request = replace(request, job=other, artifact_scope=canonical_recipe_scope(other))
    result = GenerateVlmEvidenceCommand(prepared.store, FixtureProvider(_raw())).execute(request)
    assert result.outcome.state == "succeeded"
    with pytest.raises(StoreValidationError, match="exact v4 verification") as caught:
        prepared.store.read_committed_vlm_generation_child(request.job, request.idempotency_key)
    assert "same-Job committed Source owner" in str(caught.value.__cause__)


def _direct_batch_success(prepared: Prepared, payload: dict[str, object]) -> CommandSuccess:
    request = prepared.request
    serialized = json.dumps(payload, ensure_ascii=False)
    artifact = ArtifactMember(
        "vlm_semantic_pack_set", "vlm_semantic_pack_set", 2, request.artifact_scope,
        canonical_payload_hash(serialized), serialized,
    )
    request_hash = canonical_payload_hash(json.dumps({
        "artifact_revision": artifact.revision,
        "artifact_scope": {"namespace": artifact.scope.namespace, "kind": artifact.scope.kind, "key": artifact.scope.key},
        **{key: value for key, value in payload.items() if key != "completion_policy"},
        "job": {"job_key": request.job.job_key, "profile": request.job.profile},
    }))
    claim = prepared.store.claim_vlm_batch_command(CommandClaim(
        request.job, "vlm-batch:direct-forgery", "FinalizeVlmBatchCommand", request_hash,
        execution_kind="deterministic",
    ))
    return CommandSuccess(claim.command_slot_id, artifact_set_hash((artifact,)), (artifact,))


@pytest.mark.parametrize("mutation", ["legacy_strategy", "wrong_parser", "bool_schema"])
def test_direct_batch_commit_cannot_relabel_v4_as_v3_or_forge_version_fields(
    prepared: Prepared, mutation: str,
) -> None:
    GenerateVlmEvidenceCommand(prepared.store, FixtureProvider(_raw())).execute(prepared.request)
    result = FinalizeVlmBatchCommand(prepared.store).execute(_batch(prepared))
    assert result.artifact is not None
    payload = json.loads(result.artifact.payload_json)
    if mutation == "legacy_strategy":
        payload["strategy_version"] = "vlm-batch-finalizer-v1"
        del payload["schema_version"], payload["parser_strategy_version"]
    elif mutation == "wrong_parser":
        payload["parser_strategy_version"] = "strict-semantic-pack-v3"
    else:
        payload["schema_version"] = True
    success = _direct_batch_success(prepared, payload)
    before = _history(prepared.request.job)
    with pytest.raises(StoreValidationError, match="cannot mix|version binding"):
        prepared.store.commit_vlm_batch_success(success)
    assert _history(prepared.request.job) == before


def test_old_v3_batch_bytes_unchanged_and_v4_strategy_rejects_v3_child(prepared: Prepared) -> None:
    request = replace(
        prepared.request, parser_strategy_version="strict-semantic-pack-v3",
        response_schema_json='{"type":"object"}', parser_contract_sha256=None,
    )
    wire = _wire()
    wire["schema_version"] = 3
    wire["continuity"]["temporal_segments"] = []
    for collection in ("entities", "facts", "events", "candidate_hypotheses"):
        for item in wire[collection]:
            item["support"] = {
                "confidence": "0.90", "proxy_interval": {"start_pts": 5, "end_pts": 15, "uncertainty_pts": 0},
                "supporting_frame_ids": [request.manifest.frame_samples[-1].frame_id],
            }
    raw = json.dumps(wire, ensure_ascii=False).encode("utf-8")
    result = GenerateVlmEvidenceCommand(prepared.store, FixtureProvider(raw)).execute(request)
    assert result.outcome.state == "succeeded"
    legacy = replace(prepared, request=request)
    batch_request = replace(_batch(legacy), strategy_version="vlm-batch-finalizer-v1")
    batch = FinalizeVlmBatchCommand(prepared.store).execute(batch_request)
    assert batch.artifact is not None
    original = json.loads(batch.artifact.payload_json)
    assert set(original) == {
        "children", "completion_policy", "declared_episode_count", "request_policy",
        "source_manifest_sha256", "source_provenance_sha256", "strategy_version",
    }
    before = _history(request.job)
    restarted = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    assert restarted.read_committed_vlm_generation_child(request.job, request.idempotency_key).semantic_schema_version == 3
    assert GenerateVlmEvidenceCommand(restarted, NoProvider()).execute(request).outcome.receipt_id == result.outcome.receipt_id
    assert FinalizeVlmBatchCommand(restarted).execute(batch_request).outcome.receipt_id == batch.outcome.receipt_id
    assert _history(request.job) == before
    forged = {**original, "strategy_version": VLM_BATCH_FINALIZER_STRATEGY_VERSION_V4,
              "schema_version": 4, "parser_strategy_version": "strict-semantic-pack-v4"}
    with pytest.raises(StoreValidationError, match="cannot mix"):
        prepared.store.commit_vlm_batch_success(_direct_batch_success(prepared, forged))


def test_reader_rejects_frozen_parser_digest_after_installed_implementation_changes(
    prepared: Prepared, monkeypatch: pytest.MonkeyPatch,
) -> None:
    GenerateVlmEvidenceCommand(prepared.store, FixtureProvider(_raw())).execute(prepared.request)
    before = _history(prepared.request.job)
    monkeypatch.setattr(
        "autocut_kernel.store.vlm_v4.parser_contract_sha256_for",
        lambda _strategy: "sha256:" + "f" * 64,
    )
    with pytest.raises(StoreValidationError, match="exact v4 verification") as caught:
        prepared.store.read_committed_vlm_generation_child(prepared.request.job, prepared.request.idempotency_key)
    assert "frozen parser contract" in str(caught.value.__cause__)
    assert _history(prepared.request.job) == before
