from __future__ import annotations

import hashlib
import json
import os
from decimal import Decimal
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
    GenerateVlmEvidenceCommand,
    GenerateVlmEvidenceRequest,
    adapt_vlm_observations,
)
from autocut_kernel.semantic_chain import SemanticChainBuilder, SemanticProfile
from autocut_kernel.store import Job, PostgresRuntimeStore
from autocut_kernel.store.models import canonical_recipe_scope
from autocut_kernel.vlm import (
    ProviderCompleted,
    ProviderFailed,
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
            {"schema_version": 1, "type": "object"},
            separators=(",", ":"),
        ),
        request_parameters_json='{"temperature":"0"}',
        model_id="fake-vlm-v1",
        provider_id="fake-provider",
        parse_policy=VlmParsePolicy(Decimal("0.80"), 4_096, 4, 128, 256),
    )


def _raw_success(request: GenerateVlmEvidenceRequest) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "observations": [
                {
                    "confidence": "0.91",
                    "kind": "change",
                    "proxy_interval": {
                        "start_pts": 40,
                        "end_pts": 60,
                        "uncertainty_pts": 2,
                    },
                    "summary": "画面发生变化。",
                    "supporting_frame_ids": [request.manifest.frame_samples[1].frame_id],
                }
            ],
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
    assert first.observation_set is not None
    assert replay.outcome.state == "succeeded"
    assert replay.observation_set == first.observation_set
    assert len(provider.dispatch_calls) == 1
    assert not provider.reconcile_calls
    assert provider.dispatch_calls[0].proxy_content == b"exact-proxy-video"
    observation_artifact = next(
        item for item in first.artifacts if item.artifact_type == "vlm_observation_set"
    )
    adapted = adapt_vlm_observations(
        profile=SemanticProfile.TEST,
        manifest=request.manifest,
        request_identity=request.request_identity,
        observation_set=first.observation_set,
        observation_artifact=observation_artifact,
    )
    chain = SemanticChainBuilder().build(adapted.semantic_input)
    assert len(chain.narrative.nodes) == 1
    assert adapted.candidate_catalog.entries[0].summary == "画面发生变化。"
    assert (
        adapted.candidate_catalog.entries[0]
        .coarse_interval.to_mapping()["semantic_precision"]
        == "coarse_only"
    )

    with psycopg.connect(DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM runtime.artifacts WHERE artifact_type LIKE 'vlm_%'"
            )
            assert cursor.fetchone()[0] == 3
            cursor.execute(
                "SELECT state FROM runtime.jobs WHERE job_key = 'vlm-success'"
            )
            assert cursor.fetchone()[0] == "running"


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


def test_invalid_response_is_denied_without_observation_artifact() -> None:
    assert DSN is not None
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    request = _request(store, Job("vlm-invalid", "test"))
    raw = b'{"schema_version":1,"observations":[]}'
    provider = FakeProvider(ProviderCompleted(raw, "provider-request-invalid"))

    result = GenerateVlmEvidenceCommand(store, provider).execute(request)

    assert result.outcome.state == "denied"
    assert result.attempt is not None and result.attempt.state == "failed"
    with psycopg.connect(DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM runtime.artifacts WHERE artifact_type = 'vlm_observation_set'"
            )
            assert cursor.fetchone()[0] == 0
            cursor.execute(
                "SELECT count(*) FROM storage.blob_objects WHERE content_hash = %s",
                (_digest(raw),),
            )
            assert cursor.fetchone()[0] == 1


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
