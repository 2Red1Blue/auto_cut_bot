from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import replace
from pathlib import Path

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
    FinalizeVlmBatchRequest,
    GenerateVlmEvidenceCommand,
    GenerateVlmEvidenceRequest,
    VlmBatchChildOutcome,
)
from autocut_kernel.store import (
    VLM_BATCH_IDEMPOTENCY_PREFIX,
    CommandClaim,
    CommandRejection,
    CommandStateError,
    Job,
    PostgresRuntimeStore,
    StoreValidationError,
)
from autocut_kernel.store.models import canonical_recipe_scope
from autocut_kernel.vlm import (
    GENERATION_RETRY_STRATEGY_VERSION,
    GenerationRetryPolicy,
    ProviderCompleted,
    ProviderFailed,
    ProviderFailureDisposition,
    ProviderIndeterminate,
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


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _frame_pts_set(
    *,
    source_id: str,
    source_sha256: str,
    clock_id: str,
    time_base: TimeBase,
    origin_tick: int,
    end_tick: int,
    ticks: tuple[int, ...],
) -> FramePtsIndexSet:
    context = EvidenceContext(
        source_id,
        source_sha256,
        MediaKind.VIDEO,
        clock_id,
        time_base,
        origin_tick,
        end_tick - origin_tick,
        "test-decoder-v1",
        "sha256:" + "7" * 64,
    )
    coverage = Coverage(
        source_id,
        source_sha256,
        clock_id,
        time_base,
        origin_tick,
        end_tick,
        CoverageOutcome.COMPLETE,
    )
    pts_index = PTSIndex(ticks)
    return FramePtsIndexSet(
        "frame-pts-root-v1",
        context,
        coverage,
        pts_index,
        canonical_sha256(list(pts_index.ticks)),
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
                "0018_command_execution_kind.sql",
            ):
                cursor.execute((MIGRATIONS / name).read_text())


class FakeProvider:
    def __init__(
        self,
        dispatch_result: ProviderResult,
        reconcile_result: ProviderResult | None = None,
    ) -> None:
        self.dispatch_result = dispatch_result
        self.reconcile_result = reconcile_result or dispatch_result
        self.dispatch_calls: list[ProviderDispatchRequest] = []
        self.reconcile_calls: list[ProviderReconcileQuery] = []

    def dispatch(self, request: ProviderDispatchRequest) -> ProviderResult:
        self.dispatch_calls.append(request)
        return self.dispatch_result

    def reconcile(self, query: ProviderReconcileQuery) -> ProviderResult:
        self.reconcile_calls.append(query)
        return self.reconcile_result


class SequencedProvider:
    def __init__(self, results: tuple[ProviderResult, ...]) -> None:
        self._results = list(results)
        self.dispatch_calls: list[ProviderDispatchRequest] = []

    def dispatch(self, request: ProviderDispatchRequest) -> ProviderResult:
        self.dispatch_calls.append(request)
        return self._results.pop(0)

    def reconcile(self, query: ProviderReconcileQuery) -> ProviderResult:
        raise AssertionError(f"unexpected reconciliation: {query}")


class ReentrantLeaseProbeProvider:
    def __init__(self, completed: ProviderCompleted) -> None:
        self.completed = completed
        self.command: GenerateVlmEvidenceCommand | None = None
        self.request: GenerateVlmEvidenceRequest | None = None
        self.concurrent_result: object | None = None
        self.reconcile_calls = 0

    def dispatch(self, request: ProviderDispatchRequest) -> ProviderResult:
        assert self.command is not None and self.request is not None
        self.concurrent_result = self.command.execute(self.request)
        return self.completed

    def reconcile(self, query: ProviderReconcileQuery) -> ProviderResult:
        self.reconcile_calls += 1
        raise AssertionError(f"active dispatch lease must suppress reconcile: {query}")


class BarrierReserveNextStore:
    def __init__(self, delegate: PostgresRuntimeStore) -> None:
        self.delegate = delegate
        self.entered = threading.Event()
        self.barrier = threading.Barrier(2)

    def __getattr__(self, name: str) -> object:
        return getattr(self.delegate, name)

    def reserve_next_generation_attempt(self, *args: object, **kwargs: object) -> object:
        self.entered.set()
        self.barrier.wait(timeout=5)
        return self.delegate.reserve_next_generation_attempt(*args, **kwargs)  # type: ignore[arg-type]


class CreatedThenInterruptedProvider:
    def __init__(self, completed: ProviderCompleted) -> None:
        self.completed = completed
        self.reconcile_calls: list[ProviderReconcileQuery] = []

    def dispatch(self, request: ProviderDispatchRequest) -> ProviderResult:
        assert request.on_provider_request_id is not None
        request.on_provider_request_id("provider-request-created-before-crash")
        raise TimeoutError("worker crashed after response.created")

    def reconcile(self, query: ProviderReconcileQuery) -> ProviderResult:
        self.reconcile_calls.append(query)
        return self.completed


class CallbackCasFailureProvider:
    def __init__(self, completed: ProviderCompleted) -> None:
        self.completed = completed
        self.dispatch_calls: list[ProviderDispatchRequest] = []
        self.reconcile_calls: list[ProviderReconcileQuery] = []

    def dispatch(self, request: ProviderDispatchRequest) -> ProviderResult:
        self.dispatch_calls.append(request)
        assert request.on_provider_request_id is not None
        try:
            request.on_provider_request_id("provider-request-callback-fallback")
        except RuntimeError:
            return ProviderIndeterminate(
                "PROVIDER_REQUEST_ID_PERSIST_FAILED",
                "provider-request-callback-fallback",
            )
        raise AssertionError("the first provider request-id CAS must fail")

    def reconcile(self, query: ProviderReconcileQuery) -> ProviderResult:
        self.reconcile_calls.append(query)
        return self.completed


class FailFirstProviderRequestIdStore:
    def __init__(self, delegate: PostgresRuntimeStore) -> None:
        self.delegate = delegate
        self.failed = False

    def __getattr__(self, name: str) -> object:
        return getattr(self.delegate, name)

    def record_generation_provider_request_id(
        self,
        attempt_id: object,
        *,
        expected_version: int,
        provider_request_id: str,
        dispatch_lease_token: str,
    ) -> object:
        del attempt_id, expected_version, provider_request_id, dispatch_lease_token
        if not self.failed:
            self.failed = True
            raise RuntimeError("simulated response.created CAS failure")
        raise AssertionError("dispatch must not be retried after callback failure")


def _request(
    store: PostgresRuntimeStore,
    job: Job,
) -> GenerateVlmEvidenceRequest:
    proxy = b"exact-proxy-video"
    proxy_blob = store.put_immutable_blob(
        job,
        content=proxy,
        content_hash=_digest(proxy),
        media_type="video/mp4",
    )
    source_hash = "sha256:" + "a" * 64
    time_base = TimeBase(1, 1_000)
    frame_index = _frame_pts_set(
        source_id="source-001",
        source_sha256=source_hash,
        clock_id="video-clock-0",
        time_base=time_base,
        origin_tick=1_000,
        end_tick=1_100,
        ticks=(1_000, 1_010, 1_050, 1_090, 1_100),
    )
    manifest = WindowManifest(
        source_id="source-001",
        source_clock_id="video-clock-0",
        source_sha256=source_hash,
        stream_index=0,
        source_time_base=time_base,
        source_range=TickRange(1_000, 1_100),
        core_range=TickRange(1_000, 1_100),
        frame_pts_index_set=frame_index,
        proxy_blob_ref=WindowProxyBlobRef(
            str(proxy_blob.object_id),
            proxy_blob.content_hash,
            proxy_blob.byte_length,
            proxy_blob.media_type,
        ),
        preprocess_policy_sha256="sha256:" + "b" * 64,
        window_sampling_policy_sha256="sha256:" + "c" * 64,
        timeline_map=ProxyTimelineMap.translation(
            time_base=time_base,
            proxy_range=TickRange(0, 100),
            source_start_pts=1_000,
            max_source_error_pts=1,
        ),
        frame_samples=(
            WindowFrameSample(1_010, 10, "sha256:" + "d" * 64),
            WindowFrameSample(1_050, 50, "sha256:" + "e" * 64),
            WindowFrameSample(1_090, 90, "sha256:" + "f" * 64),
        ),
    )
    manifest_set = WindowManifestSet(
        source_id=manifest.source_id,
        source_clock_id=manifest.source_clock_id,
        source_sha256=manifest.source_sha256,
        stream_index=manifest.stream_index,
        source_time_base=manifest.source_time_base,
        declared_source_range=manifest.core_range,
        manifests=(manifest,),
    )
    return GenerateVlmEvidenceRequest(
        job=job,
        idempotency_key="vlm-window-001",
        artifact_scope=canonical_recipe_scope(job),
        artifact_revision=1,
        manifest=manifest,
        manifest_set=manifest_set,
        proxy_blob=proxy_blob,
        prompt_template="Identify only visible semantic changes.",
        prompt_version="semantic-evidence-v1",
        response_schema_json=json.dumps(
            {"schema_version": 3, "type": "object"},
            separators=(",", ":"),
        ),
        request_parameters_json='{"temperature":"0"}',
        model_id="fake-vlm-v1",
        provider_id="fake-provider",
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
            3,
            (0, 0),
        ),
        episode_index=0,
    )


def _raw_success(request: GenerateVlmEvidenceRequest) -> bytes:
    support = {
        "confidence": "0.91",
        "proxy_interval": {
            "start_pts": 40,
            "end_pts": 60,
            "uncertainty_pts": 2,
        },
        "supporting_frame_ids": [request.manifest.frame_samples[1].frame_id],
    }
    return json.dumps(
        {
            "schema_version": 3,
            "window_summary": {
                "summary": "画面发生变化。",
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
                    "visual_description": "A person visible in the frame.",
                    "support": support,
                }
            ],
            "facts": [
                {
                    "local_fact_id": "fact_1",
                    "fact_kind": "visible_change",
                    "subject_ref": "entity_1",
                    "object_ref": None,
                    "summary": "画面发生变化。",
                    "support": support,
                }
            ],
            "events": [],
            "candidate_hypotheses": [],
        },
        separators=(",", ":"),
    ).encode()


def test_success_is_persisted_once_and_replay_never_calls_provider() -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    request = _request(store, Job("vlm-success", "test"))
    provider = FakeProvider(ProviderCompleted(_raw_success(request), "provider-request-1"))
    command = GenerateVlmEvidenceCommand(store, provider)

    first = command.execute(request)
    replay = command.execute(request)

    assert first.outcome.state == "succeeded"
    assert first.semantic_pack is not None
    assert replay.outcome.state == "succeeded"
    assert replay.semantic_pack == first.semantic_pack
    assert len(provider.dispatch_calls) == 1
    assert not provider.reconcile_calls
    assert provider.dispatch_calls[0].proxy_content == b"exact-proxy-video"
    semantic_pack_artifact = next(
        item for item in first.artifacts if item.artifact_type == "vlm_semantic_pack"
    )
    assert semantic_pack_artifact.payload_json == json.dumps(
        first.semantic_pack.to_mapping(),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    assert first.semantic_pack.facts[0].summary == "画面发生变化。"
    assert first.semantic_pack.candidate_hypotheses == ()
    assert first.semantic_pack.facts[0].support.source_interval.to_mapping()[
        "semantic_precision"
    ] == "coarse_only"

    with psycopg.connect(DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM runtime.artifacts WHERE artifact_type LIKE 'vlm_%'"
            )
            assert cursor.fetchone()[0] == 3
            cursor.execute(
                "SELECT state FROM runtime.jobs WHERE job_key = 'vlm-success'"
            )
            state = cursor.fetchone()[0]
            assert (state.decode() if isinstance(state, bytes) else state) == "running"


def test_postgres_resolves_exact_child_references_and_batch_finalizer() -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    job = Job("vlm-independent-proof", "test")
    request = replace(
        _request(store, job),
        episode_index=0,
        source_manifest_sha256="sha256:" + "8" * 64,
        source_provenance_sha256="sha256:" + "9" * 64,
    )
    provider = FakeProvider(ProviderCompleted(_raw_success(request), "provider-proof-1"))
    child_result = GenerateVlmEvidenceCommand(store, provider).execute(request)

    persisted = store.read_committed_vlm_generation_child(job, request.idempotency_key)
    exact_reference = store.read_committed_vlm_input_reference(
        job, request.idempotency_key
    )

    assert child_result.attempt is not None
    assert persisted.request_hash == request.request_hash
    assert persisted.attempt_id == child_result.attempt.attempt_id
    assert persisted.window_manifest_sha256 == request.manifest.canonical_hash
    assert persisted.window_manifest_set_sha256 == request.manifest_set.canonical_hash
    assert persisted.source_manifest_sha256 == request.source_manifest_sha256
    assert persisted.source_provenance_sha256 == request.source_provenance_sha256
    assert exact_reference.request_record.content_hash == persisted.reference.content_hash
    assert exact_reference.request_payload == persisted.request_payload
    assert exact_reference.semantic_pack.artifact_type == "vlm_semantic_pack"
    assert child_result.outcome.receipt_id is not None
    assert child_result.outcome.artifact_set_id is not None
    batch = FinalizeVlmBatchCommand(store).execute(
        FinalizeVlmBatchRequest(
            job,
            VLM_BATCH_IDEMPOTENCY_PREFIX + "independent-proof",
            canonical_recipe_scope(job),
            1,
            1,
            persisted.source_manifest_sha256,
            persisted.source_provenance_sha256,
            (
                VlmBatchChildOutcome(
                    0,
                    request.idempotency_key,
                    request.manifest.canonical_hash,
                    persisted.source_manifest_sha256,
                    persisted.source_provenance_sha256,
                    request.request_hash,
                    "succeeded",
                    child_result.outcome.receipt_id,
                    child_result.outcome.artifact_set_id,
                ),
            ),
        )
    )
    assert batch.outcome.state == "succeeded"
    assert batch.artifact is not None


def test_postgres_reader_rejects_missing_and_duplicate_relabelled_child() -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    job = Job("vlm-independent-proof-negative", "test")
    with pytest.raises(StoreValidationError, match="unavailable"):
        store.read_committed_vlm_generation_child(job, "missing-child")

    request = replace(
        _request(store, job),
        episode_index=0,
        source_manifest_sha256="sha256:" + "8" * 64,
        source_provenance_sha256="sha256:" + "9" * 64,
    )
    result = GenerateVlmEvidenceCommand(
        store,
        FakeProvider(ProviderCompleted(_raw_success(request), "provider-proof-negative")),
    ).execute(request)
    assert result.outcome.receipt_id is not None
    assert result.outcome.artifact_set_id is not None
    assert request.source_manifest_sha256 is not None
    assert request.source_provenance_sha256 is not None
    first = VlmBatchChildOutcome(
        0,
        request.idempotency_key,
        request.manifest.canonical_hash,
        request.source_manifest_sha256,
        request.source_provenance_sha256,
        request.request_hash,
        "succeeded",
        result.outcome.receipt_id,
        result.outcome.artifact_set_id,
    )
    duplicate = replace(first, episode_index=1)
    batch_key = VLM_BATCH_IDEMPOTENCY_PREFIX + "duplicate-proof"
    batch_request = FinalizeVlmBatchRequest(
        job,
        batch_key,
        canonical_recipe_scope(job),
        1,
        2,
        first.source_manifest_sha256,
        first.source_provenance_sha256,
        (first, duplicate),
    )

    with pytest.raises(ValueError, match="exact persisted Kernel outcome|duplicate"):
        FinalizeVlmBatchCommand(store).execute(batch_request)
    assert store.read_outcome(job, batch_key) is None


def test_indeterminate_dispatch_reconciles_same_attempt_without_redispatch() -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    request = _request(store, Job("vlm-reconcile", "test"))
    provider = FakeProvider(
        ProviderIndeterminate("TIMEOUT", "provider-request-2"),
        ProviderCompleted(_raw_success(request), "provider-request-2"),
    )
    command = GenerateVlmEvidenceCommand(store, provider)

    first = command.execute(request)
    assert first.outcome.state == "running"
    assert first.attempt is not None and first.attempt.state == "indeterminate"
    with psycopg.connect(DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM runtime.command_receipts")
            assert cursor.fetchone()[0] == 0

    recovered = command.execute(request)
    assert recovered.outcome.state == "succeeded"
    assert len(provider.dispatch_calls) == 1
    assert len(provider.reconcile_calls) == 1
    assert recovered.attempt is not None
    assert recovered.attempt.attempt_id == first.attempt.attempt_id


def test_response_created_callback_persists_request_id_before_stream_crash() -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    request = _request(store, Job("vlm-created-crash", "test"))
    provider = CreatedThenInterruptedProvider(
        ProviderCompleted(
            _raw_success(request),
            "provider-request-created-before-crash",
        )
    )
    command = GenerateVlmEvidenceCommand(store, provider)

    interrupted = command.execute(request)

    assert interrupted.outcome.state == "running"
    assert interrupted.attempt is not None
    assert interrupted.attempt.state == "indeterminate"
    assert (
        interrupted.attempt.provider_request_id
        == "provider-request-created-before-crash"
    )
    recovered = command.execute(request)
    assert recovered.outcome.state == "succeeded"
    assert len(provider.reconcile_calls) == 1
    assert (
        provider.reconcile_calls[0].provider_request_id
        == "provider-request-created-before-crash"
    )


def test_callback_failure_fallback_persists_id_and_replay_only_reconciles() -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    request = _request(store, Job("vlm-created-cas-fallback", "test"))
    wrapped_store = FailFirstProviderRequestIdStore(store)
    provider = CallbackCasFailureProvider(
        ProviderCompleted(
            _raw_success(request),
            "provider-request-callback-fallback",
        )
    )
    command = GenerateVlmEvidenceCommand(wrapped_store, provider)  # type: ignore[arg-type]

    interrupted = command.execute(request)

    assert interrupted.outcome.state == "running"
    assert interrupted.attempt is not None
    assert interrupted.attempt.state == "indeterminate"
    assert (
        interrupted.attempt.provider_request_id
        == "provider-request-callback-fallback"
    )
    recovered = command.execute(request)
    assert recovered.outcome.state == "succeeded"
    assert len(provider.dispatch_calls) == 1
    assert len(provider.reconcile_calls) == 1
    assert (
        provider.reconcile_calls[0].provider_request_id
        == "provider-request-callback-fallback"
    )


def test_invalid_response_is_denied_without_semantic_pack_artifact() -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    request = _request(store, Job("vlm-invalid", "test"))
    raw = b'{"schema_version":2,"observations":[]}'
    provider = FakeProvider(ProviderCompleted(raw, "provider-request-invalid"))

    result = GenerateVlmEvidenceCommand(store, provider).execute(request)

    assert result.outcome.state == "denied"
    assert result.attempt is not None and result.attempt.state == "failed"
    with psycopg.connect(DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM runtime.artifacts WHERE artifact_type = 'vlm_semantic_pack'"
            )
            assert cursor.fetchone()[0] == 0
            cursor.execute(
                "SELECT count(*) FROM storage.blob_objects WHERE content_hash = %s",
                (_digest(raw),),
            )
            assert cursor.fetchone()[0] == 1


def test_event_fact_support_invariant_denial_persists_terminal_receipt() -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    request = _request(store, Job("vlm-support-mismatch", "test"))
    payload = json.loads(_raw_success(request))
    event_support = {
        "confidence": "0.91",
        "proxy_interval": {
            "start_pts": 70,
            "end_pts": 95,
            "uncertainty_pts": 0,
        },
        "supporting_frame_ids": [request.manifest.frame_samples[2].frame_id],
    }
    payload["events"] = [
        {
            "local_event_id": "event_1",
            "event_kind": "reveal",
            "summary": "另一个时段发生变化。",
            "participant_refs": ["entity_1"],
            "fact_refs": ["fact_1"],
            "cause_event_refs": [],
            "effect_event_refs": [],
            "open_question": None,
            "temporal_mode": "present",
            "support": event_support,
        }
    ]
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()

    result = GenerateVlmEvidenceCommand(
        store,
        FakeProvider(ProviderCompleted(raw, "provider-support-mismatch")),
    ).execute(request)

    assert result.outcome.state == "denied"
    assert result.outcome.receipt_id is not None
    assert result.attempt is not None and result.attempt.state == "failed"
    assert result.attempt.failure_code == "SEMANTIC_PACK_INVARIANT_VIOLATION"
    with psycopg.connect(DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT outcome, failure_code
                  FROM runtime.command_receipts
                 WHERE receipt_id = %s
                """,
                (result.outcome.receipt_id,),
            )
            assert cursor.fetchone() == (
                "denied",
                "SEMANTIC_PACK_INVARIANT_VIOLATION",
            )


def test_continuity_invariant_denial_persists_terminal_receipt() -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    request = _request(store, Job("vlm-continuity-mismatch", "test"))
    payload = json.loads(_raw_success(request))
    payload["continuity"]["entry_state_fact_refs"] = ["fact_1"]
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()

    result = GenerateVlmEvidenceCommand(
        store,
        FakeProvider(ProviderCompleted(raw, "provider-continuity-mismatch")),
    ).execute(request)

    assert result.outcome.state == "denied"
    assert result.outcome.receipt_id is not None
    assert result.attempt is not None and result.attempt.state == "failed"
    assert result.attempt.failure_code == "SEMANTIC_PACK_INVARIANT_VIOLATION"


def test_provider_terminal_failure_closes_attempt_and_command_without_artifacts() -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    request = _request(store, Job("vlm-provider-failed", "test"))
    provider = FakeProvider(
        ProviderFailed(
            "PROVIDER_DENIED",
            '{"retryable":false}',
            "provider-request-failed",
        )
    )

    result = GenerateVlmEvidenceCommand(store, provider).execute(request)

    assert result.outcome.state == "failed"
    assert result.attempt is not None and result.attempt.state == "failed"
    assert result.attempt.provider_request_id == "provider-request-failed"
    with psycopg.connect(DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM runtime.artifact_sets")
            assert cursor.fetchone()[0] == 0


def test_generic_rejection_api_cannot_terminalize_generation_command() -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    outcome = store.claim_command(
        CommandClaim(
            Job("vlm-generic-rejection-blocked", "test"),
            "vlm-generic-rejection",
            "GenerateVlmEvidenceCommand",
            "sha256:" + "1" * 64,
            execution_kind="generation",
        )
    )

    with pytest.raises(CommandStateError, match="explicit generation API"):
        store.commit_command_rejection(
            CommandRejection(outcome.command_slot_id, "FORBIDDEN", "{}")
        )


def test_retryable_503_then_429_then_success_commits_three_attempt_chain() -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    request = _request(store, Job("vlm-retry-success", "test"))
    provider = SequencedProvider(
        (
            ProviderFailed(
                "PROVIDER_503",
                '{"status":503}',
                "provider-retry-1",
                ProviderFailureDisposition.RETRYABLE,
            ),
            ProviderFailed(
                "PROVIDER_429",
                '{"status":429}',
                "provider-retry-2",
                ProviderFailureDisposition.RETRYABLE,
            ),
            ProviderCompleted(_raw_success(request), "provider-retry-3"),
        )
    )
    command = GenerateVlmEvidenceCommand(store, provider)

    first = command.execute(request)
    second = command.execute(request)
    final = command.execute(request)

    assert first.outcome.state == second.outcome.state == "running"
    assert final.outcome.state == "succeeded"
    assert [item.attempt_ordinal for item in store.read_generation_attempt_chain(
        request.job, final.outcome.command_slot_id
    )] == [1, 2, 3]
    assert len({call.provider_idempotency_key for call in provider.dispatch_calls}) == 3
    with psycopg.connect(DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM runtime.generation_receipt_attempts WHERE receipt_id = %s",
                (final.outcome.receipt_id,),
            )
            assert cursor.fetchone()[0] == 3


def test_retryable_budget_exhaustion_commits_one_complete_failure_receipt() -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    request = _request(store, Job("vlm-retry-exhausted", "test"))
    provider = SequencedProvider(
        tuple(
            ProviderFailed(
                f"PROVIDER_RETRY_{ordinal}",
                json.dumps({"ordinal": ordinal}, separators=(",", ":")),
                f"provider-exhausted-{ordinal}",
                ProviderFailureDisposition.RETRYABLE,
            )
            for ordinal in range(1, 4)
        )
    )
    command = GenerateVlmEvidenceCommand(store, provider)

    assert command.execute(request).outcome.state == "running"
    assert command.execute(request).outcome.state == "running"
    final = command.execute(request)

    assert final.outcome.state == "failed"
    assert final.outcome.failure_code == "RETRY_BUDGET_EXHAUSTED"
    assert final.outcome.receipt_id is not None
    detail = json.loads(final.outcome.failure_detail_json or "{}")
    assert [item["attempt_ordinal"] for item in detail["attempts"]] == [1, 2, 3]
    with psycopg.connect(DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM runtime.command_receipts WHERE command_slot_id = %s",
                (final.outcome.command_slot_id,),
            )
            assert cursor.fetchone()[0] == 1
            cursor.execute(
                "SELECT count(*) FROM runtime.generation_receipt_attempts WHERE receipt_id = %s",
                (final.outcome.receipt_id,),
            )
            assert cursor.fetchone()[0] == 3


def test_retry_backoff_is_persisted_and_suppresses_early_redispatch() -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    request = replace(
        _request(store, Job("vlm-retry-backoff", "test")),
        retry_policy=GenerationRetryPolicy(
            GENERATION_RETRY_STRATEGY_VERSION,
            3,
            (60, 120),
        ),
    )
    provider = SequencedProvider(
        (
            ProviderFailed(
                "PROVIDER_503",
                '{"status":503}',
                "provider-backoff-1",
                ProviderFailureDisposition.RETRYABLE,
            ),
        )
    )
    command = GenerateVlmEvidenceCommand(store, provider)

    first = command.execute(request)
    early_replay = command.execute(request)

    assert first.attempt is not None and first.attempt.attempt_ordinal == 2
    assert first.attempt.retry_delay_is_active()
    assert first.attempt.retry_backoff_seconds == 60
    assert early_replay.attempt is not None
    assert early_replay.attempt.attempt_id == first.attempt.attempt_id
    assert early_replay.attempt.state == "reserved"
    assert len(provider.dispatch_calls) == 1


@pytest.mark.parametrize(
    "disposition",
    (
        ProviderFailureDisposition.NONRETRYABLE,
        ProviderFailureDisposition.REPAIRABLE,
    ),
)
def test_nonretryable_and_repairable_failures_stop_after_one_attempt(
    disposition: ProviderFailureDisposition,
) -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    request = _request(store, Job(f"vlm-stop-{disposition.value}", "test"))
    provider = SequencedProvider(
        (
            ProviderFailed(
                "PROVIDER_TERMINAL",
                '{"terminal":true}',
                "provider-terminal",
                disposition,
            ),
        )
    )

    result = GenerateVlmEvidenceCommand(store, provider).execute(request)

    assert result.outcome.state == "failed"
    assert len(provider.dispatch_calls) == 1
    assert len(store.read_generation_attempt_chain(
        request.job, result.outcome.command_slot_id
    )) == 1


def test_active_dispatch_lease_suppresses_reentrant_reconcile_and_version_change() -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    request = _request(store, Job("vlm-active-dispatch-lease", "test"))
    provider = ReentrantLeaseProbeProvider(
        ProviderCompleted(_raw_success(request), "provider-active-lease")
    )
    command = GenerateVlmEvidenceCommand(store, provider)
    provider.command = command
    provider.request = request

    result = command.execute(request)

    assert result.outcome.state == "succeeded"
    assert provider.reconcile_calls == 0
    concurrent = provider.concurrent_result
    assert concurrent is not None
    assert concurrent.outcome.state == "running"  # type: ignore[attr-defined]
    assert concurrent.attempt.version == 1  # type: ignore[attr-defined]
    assert concurrent.attempt.state == "dispatched"  # type: ignore[attr-defined]


def test_concurrent_recovery_reserves_only_one_next_ordinal() -> None:
    assert DSN is not None
    durable_store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    wrapped_store = BarrierReserveNextStore(durable_store)
    request = _request(durable_store, Job("vlm-reserve-next-race", "test"))
    provider = SequencedProvider(
        (
            ProviderFailed(
                "PROVIDER_503",
                '{"status":503}',
                "provider-race-1",
                ProviderFailureDisposition.RETRYABLE,
            ),
        )
    )
    command = GenerateVlmEvidenceCommand(wrapped_store, provider)  # type: ignore[arg-type]
    results: list[object] = []

    first = threading.Thread(target=lambda: results.append(command.execute(request)))
    first.start()
    assert wrapped_store.entered.wait(timeout=5)
    second = threading.Thread(target=lambda: results.append(command.execute(request)))
    second.start()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive() and not second.is_alive()
    assert len(results) == 2
    attempt_ids = {result.attempt.attempt_id for result in results}  # type: ignore[attr-defined]
    assert len(attempt_ids) == 1
    chain = durable_store.read_generation_attempt_chain(
        request.job, results[0].outcome.command_slot_id  # type: ignore[attr-defined]
    )
    assert [attempt.attempt_ordinal for attempt in chain] == [1, 2]
