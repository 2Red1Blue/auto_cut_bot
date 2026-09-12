"""Real disposable PostgreSQL acceptance for the Terra observation implementation."""

import json
import os
import runpy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from autocut_kernel.pipeline.observation_generation import GenerateObservationCommand
from autocut_kernel.store import ArtifactScope, CommandSuccess, Job, PostgresRuntimeStore
from autocut_kernel.vlm import GenerationRetryPolicy, ProviderCompleted, ProviderIndeterminate
from autocut_kernel.vlm.observation_contract import ObservationLimits

from auto_cut_bot.pipeline.source_prep import (
    IdentitySourceWindowBuilder,
    PrepareWholeSeriesSourcesCommand,
    PrepareWholeSeriesSourcesRequest,
    read_persisted_prepared_sources_bundle,
)
from auto_cut_bot.pipeline.vlm.observation_factory import (
    ObservationRuntimePolicy,
    build_observation_request,
)

DSN = os.environ.get("AUTOCUT_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="requires isolated PostgreSQL acceptance database")


class CountingProvider:
    def __init__(self, unknown_first=False):
        self.dispatches = 0
        self.reconciles = 0
        self.unknown_first = unknown_first

    def completed(self):
        return ProviderCompleted(json.dumps({
            "description": "女子放下信封后后退。",
            "moments": [],
            "interpretations": ["她似乎有所顾虑。"],
            "uncertainties": ["无法确定信封内容。"],
        }, ensure_ascii=False).encode(), "test-observation-response")

    def dispatch(self, request):
        self.dispatches += 1
        if self.unknown_first:
            request.on_provider_request_id("test-observation-response")
            return ProviderIndeterminate("TEST_CONNECTION_LOST", "test-observation-response")
        return self.completed()

    def reconcile(self, query):
        self.reconciles += 1
        assert query.provider_request_id == "test-observation-response"
        return self.completed()


@pytest.fixture
def observation_request(tmp_path, request):
    assert DSN
    with psycopg.connect(DSN, autocommit=True) as connection:
        if connection.info.dbname != "ac_autocut_verify":
            pytest.fail("Only disposable ac_autocut_verify may run schema-reset tests")
        with connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA IF EXISTS runtime CASCADE")
            cursor.execute("DROP SCHEMA IF EXISTS storage CASCADE")
            for name in (
                "0001_runtime_core.sql", "0002_runtime_core_constraints.sql",
                "0003_vlm_generation_and_run_finalization.sql", "0004_provider_media_objects.sql",
                "0006_ark_provider_recovery.sql", "0009_vlm_bounded_retry.sql",
                "0011_generation_retry_schedule.sql", "0018_command_execution_kind.sql",
            ):
                cursor.execute((Path("packages/autocut-kernel/migrations") / name).read_text())
    helpers = runpy.run_path("tests/pipeline/test_whole_series_source_prep_command.py")
    helpers["_make_nonzero_media"](tmp_path / "episode.mp4")
    episode_count = getattr(request, "param", 1)
    for index in range(1, episode_count):
        helpers["_make_nonzero_media"](tmp_path / f"episode-{index}.mp4")
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    job = Job("observation-acceptance", "test")
    scope = ArtifactScope("pipeline", "job", job.job_key)
    request = PrepareWholeSeriesSourcesRequest(
        job, "source", scope, 1,
        helpers["_source_root"](tmp_path.resolve(), expected_source_count=episode_count),
    )
    source = PrepareWholeSeriesSourcesCommand(
        store,
        builder=IdentitySourceWindowBuilder(probe_port=helpers["CountingProbe"](), sample_count=3),
    ).execute(request)
    assert source.outcome.state == "succeeded"
    bundle = read_persisted_prepared_sources_bundle(
        store, job=job, outcome=source.outcome, artifact_scope=scope, artifact_revision=1,
    )
    policy = ObservationRuntimePolicy(
        model_id="test-only-model", provider_id="test-only-provider",
        limits=ObservationLimits(524288, 131072, 65536, 256, 256, 256),
        max_output_tokens=32768, video_fps=1, task="描述画面，保留判断和未知。",
    )
    return store, build_observation_request(
        source_bundle=bundle, episode_index=episode_count - 1, job=job, idempotency_key="observe",
        policy=policy, retry_policy=GenerationRetryPolicy("generation-retry-v1", 1, ()),
    )


def test_success_and_fresh_store_replay_without_second_dispatch(observation_request):
    store, request = observation_request
    provider = CountingProvider()
    result = GenerateObservationCommand(store, provider).execute(request)
    assert result.outcome.state == "succeeded"
    replay = GenerateObservationCommand(
        PostgresRuntimeStore(lambda: psycopg.connect(DSN)), provider,
    ).execute(request)
    assert replay.outcome.receipt_id == result.outcome.receipt_id
    assert replay.report == result.report
    assert provider.dispatches == 1


def test_unknown_result_reconciles_same_provider_request(observation_request):
    store, request = observation_request
    provider = CountingProvider(unknown_first=True)
    command = GenerateObservationCommand(store, provider)
    pending = command.execute(request)
    assert pending.outcome.state == "running"
    recovered = command.execute(request)
    assert recovered.outcome.state == "succeeded"
    assert provider.dispatches == provider.reconciles == 1


def test_foreign_source_receipt_rejected_before_dispatch(observation_request):
    store, request = observation_request
    provider = CountingProvider()
    forged = replace(request, source_binding=replace(request.source_binding, source_receipt_id=uuid4()))
    with pytest.raises(ValueError):
        GenerateObservationCommand(store, provider).execute(forged)
    assert provider.dispatches == 0


def test_writer_rejects_caller_supplied_projection(observation_request):
    store, request = observation_request
    result = GenerateObservationCommand(store, CountingProvider()).execute(request)
    assert result.outcome.state == "succeeded"
    from autocut_kernel.store.models import artifact_set_hash
    forged_members = result.artifacts[:-1]
    forged = CommandSuccess(
        result.outcome.command_slot_id, artifact_set_hash(forged_members), forged_members,
    )
    with pytest.raises(ValueError):
        store.commit_observation_generation_success(request, result.attempt, forged)


def test_actual_observation_adapter_uses_database_file_cache_and_clean_wire(observation_request):
    from auto_cut_bot.pipeline.vlm.ark_file_cache import PostgresArkFileCache
    from auto_cut_bot.pipeline.vlm.doubao_ark_provider import (
        DOUBAO_ARK_PROVIDER_ID,
        DoubaoArkVlmProviderConfig,
    )
    from auto_cut_bot.pipeline.vlm.observation_provider import ObservationArkProvider

    store, original = observation_request
    request = replace(original, provider_id=DOUBAO_ARK_PROVIDER_ID)
    calls = {"uploads": 0, "responses": 0, "retrieves": 0}
    bodies = []

    class Files:
        def create(self, **kwargs):
            calls["uploads"] += 1
            return SimpleNamespace(id="test-file-id", status="processed")

        def retrieve(self, file_id):
            assert file_id == "test-file-id"
            calls["retrieves"] += 1
            return SimpleNamespace(id=file_id, status="processed")

        def wait_for_processing(self, file_id, **kwargs):
            return self.retrieve(file_id)

    client = SimpleNamespace(files=Files(), close=lambda: None)
    provider = ObservationArkProvider(
        DoubaoArkVlmProviderConfig(api_key="test-only", tenant_id="test", project_id="test"),
        file_cache=PostgresArkFileCache(lambda: psycopg.connect(DSN)),
    )
    provider._transport.create_client = lambda: client

    def dispatch(body, **kwargs):
        calls["responses"] += 1
        bodies.append(body)
        return replace(
            CountingProvider().completed(),
            provider_request_id=f"test-observation-response-{calls['responses']}",
        )

    provider._transport.dispatch = dispatch
    first = GenerateObservationCommand(store, provider).execute(request)
    assert first.outcome.state == "succeeded"
    GenerateObservationCommand(store, provider).execute(request)
    assert calls["uploads"] == calls["responses"] == 1
    second = GenerateObservationCommand(store, provider).execute(
        replace(request, idempotency_key="second-observation"),
    )
    assert second.outcome.state == "succeeded"
    assert calls["uploads"] == 1
    assert calls["responses"] == 2 and calls["retrieves"] >= 1
    for body in bodies:
        assert body["thinking"] == {"type": "disabled"}
        wire = json.dumps(body)
        assert "source_binding" not in wire and "alias_envelope" not in wire
        assert str(request.source_binding.source_receipt_id) not in wire
        assert "candidate_hypotheses" not in wire


def test_invalid_json_becomes_terminal_without_blind_regeneration(observation_request):
    store, request = observation_request

    class MalformedProvider(CountingProvider):
        def completed(self):
            return ProviderCompleted(b"{", "test-malformed-response")

    provider = MalformedProvider()
    command = GenerateObservationCommand(store, provider)
    result = command.execute(request)
    assert result.outcome.state == "failed"
    assert command.execute(request).outcome.receipt_id == result.outcome.receipt_id
    assert provider.dispatches == 1


def test_unknown_alias_preserves_committed_report_and_private_original_mapping(observation_request):
    from autocut_kernel.vlm.observation_aliases import ObservationAliasMap
    from autocut_kernel.vlm.observation_contract import observation_response_schema

    store, original = observation_request
    aliases = ObservationAliasMap(original.job.job_key, {"a": "private-existing-target"})
    request = replace(
        original, alias_map=aliases,
        response_schema_json=json.dumps(observation_response_schema(aliases)),
    )

    class UnknownSelectionProvider(CountingProvider):
        def completed(self):
            root = json.loads(super().completed().raw_response)
            root.update(selected_id="z", selection_reason="人物看不清楚")
            return ProviderCompleted(json.dumps(root).encode(), "test-unknown-selection")

    provider = UnknownSelectionProvider()
    result = GenerateObservationCommand(store, provider).execute(request)
    assert result.outcome.state == "succeeded"
    assert result.report.description == "女子放下信封后后退。"
    assert result.report.selection.status == "unresolved"
    assert result.report.selection.raw_selected_id == "z"
    assert result.report.selection.resolved_target is None
    saved = store.read_immutable_blob(request.job, result.attempt.request_payload)
    assert json.loads(saved)["alias_envelope"] == aliases.to_mapping()
    replay = GenerateObservationCommand(store, provider).execute(request)
    assert replay.report == result.report and provider.dispatches == 1


@pytest.mark.parametrize("observation_request", [2], indirect=True)
def test_later_episode_is_not_limited_by_per_episode_window_count(observation_request):
    store, request = observation_request
    assert request.source_binding.episode_index == 1
    assert len(request.manifest_set.manifests) == 1
    result = GenerateObservationCommand(store, CountingProvider()).execute(request)
    assert result.outcome.state == "succeeded"


def test_unknown_without_response_id_never_creates_another_generation(observation_request):
    store, request = observation_request

    class LostIdProvider(CountingProvider):
        def dispatch(self, request):
            self.dispatches += 1
            return ProviderIndeterminate("TEST_LOST_RESPONSE_ID")

        def reconcile(self, query):
            self.reconciles += 1
            assert query.provider_request_id is None
            return ProviderIndeterminate("PROVIDER_REQUEST_ID_UNKNOWN")

    provider = LostIdProvider()
    command = GenerateObservationCommand(store, provider)
    for _ in range(3):
        assert command.execute(request).outcome.state == "running"
    assert provider.dispatches == 1
    assert provider.reconciles == 2
