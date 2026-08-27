"""Simulated host relocation with real PostgreSQL readback, never a provider call."""

from __future__ import annotations

import builtins
import os
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from autocut_kernel.store import (
    ArtifactScope,
    BlobIntegrityError,
    BlobUnavailableError,
    Job,
    PostgresRuntimeStore,
)

from auto_cut_bot.pipeline.runtime import (
    PipelineExecutionProfile,
    PipelineRunRequest,
    PipelineStageContext,
    PipelineStageResult,
    PostgresPipelineRunStore,
)
from auto_cut_bot.pipeline.runtime.source_prep_stage import source_prep_kernel_idempotency_key
from auto_cut_bot.pipeline.runtime.vlm_stage import VlmPipelineStage
from auto_cut_bot.pipeline.source_prep import (
    PrepareWholeSeriesSourcesCommand,
    PrepareWholeSeriesSourcesRequest,
)
from auto_cut_bot.pipeline.vlm.prompt import VLM_COMPACT_PROMPT_VERSION
from tests.pipeline.test_pipeline_vlm_stage import RUN_ID, _profile
from tests.pipeline.test_whole_series_source_prep_command import (
    SyntheticLayoutProbe,
    SyntheticSampleBuilder,
    _source_root,
)

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("AUTOCUT_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="requires an explicit disposable PostgreSQL database")
WINDOWS_ROOT = r"Z:\unavailable-portable-resume-host\series"
SOURCE_BYTES = b"synthetic source bytes; media probe and frames are synthetic"


class _NoProvider:
    def __init__(self) -> None:
        self.calls = 0

    def dispatch(self, _request):
        self.calls += 1
        raise AssertionError("portable recovery tests must never dispatch a provider")

    def reconcile(self, _query):
        self.calls += 1
        raise AssertionError("portable recovery tests must never query a provider")


@dataclass
class _PreparedRun:
    source_root: Path
    context: PipelineStageContext
    source_slot: UUID


@pytest.fixture
def prepared_run(tmp_path: Path) -> _PreparedRun:
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

    root = tmp_path / "source-preparation-host"
    root.mkdir()
    (root / "episode.mp4").write_bytes(SOURCE_BYTES)
    base = _profile()
    profile = PipelineExecutionProfile.from_semantic_policies(
        replace(base.to_doubao_policy(), adapter_strategy_version="doubao-ark-files-responses-stream-v5",
                thinking_type="disabled"),
        retry_policy=base.to_generation_retry_policy(),
    )
    run_store = PostgresPipelineRunStore(lambda: psycopg.connect(DSN))
    # The persisted original locator belongs to another host. Synthetic local
    # preparation seeds real immutable evidence; this is not HTTP authorization.
    request = PipelineRunRequest("test", source_root=WINDOWS_ROOT)
    run_store._claim_run_sync(RUN_ID, "portable-original", request, request.request_hash, profile)
    job = Job(RUN_ID, "test")
    kernel = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    source = PrepareWholeSeriesSourcesCommand(
        kernel, builder=SyntheticSampleBuilder(SyntheticLayoutProbe()),
    ).execute(PrepareWholeSeriesSourcesRequest(
        job, source_prep_kernel_idempotency_key(RUN_ID),
        ArtifactScope("pipeline", "job", RUN_ID), 1, _source_root(root),
    ))
    assert source.outcome.state == "succeeded" and source.outcome.receipt_id is not None
    control = run_store._claim_next_pending_sync(RUN_ID, 0, "source-prep")
    assert control is not None and control.stage == "source_prep"
    run_store._record_result_sync(
        RUN_ID, PipelineStageResult(control.command_id, "succeeded", source.outcome.receipt_id),
        control.version, "source-prep",
    )
    snapshot = run_store._read_run_sync(RUN_ID)
    assert snapshot is not None
    context = PipelineStageContext(RUN_ID, snapshot.request, snapshot.commands[1], snapshot.execution_profile)
    return _PreparedRun(root, context, source.outcome.command_slot_id)


def _source_history(prepared: _PreparedRun):
    assert DSN is not None
    with psycopg.connect(DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT to_jsonb(job)::text, to_jsonb(slot)::text, to_jsonb(receipt)::text,
                to_jsonb(artifact_set)::text,
                (SELECT jsonb_agg(to_jsonb(artifact) ORDER BY artifact_id)::text
                   FROM runtime.artifacts artifact WHERE artifact.artifact_set_id = artifact_set.artifact_set_id)
                FROM runtime.command_slots slot
                JOIN runtime.jobs job USING (job_id)
                JOIN runtime.command_receipts receipt USING (command_slot_id)
                JOIN runtime.artifact_sets artifact_set USING (command_slot_id)
                WHERE slot.command_slot_id = %s""",
            (prepared.source_slot,),
        )
        row = cursor.fetchone()
        assert row is not None
        return row


def _forbid_source_io(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    def guard(delegate):
        def checked(path, *args, **kwargs):
            value = os.fspath(path) if isinstance(path, (str, bytes, os.PathLike)) else ""
            if isinstance(value, str) and (value.startswith(str(root)) or value.startswith(WINDOWS_ROOT)):
                raise AssertionError("recovery touched the original host filesystem")
            return delegate(path, *args, **kwargs)
        return checked

    for owner, name in ((Path, "stat"), (Path, "exists"), (Path, "open"),
                        (builtins, "open"), (os, "stat"), (os, "open")):
        monkeypatch.setattr(owner, name, guard(getattr(owner, name)))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("recovery attempted a media probe or subprocess")

    monkeypatch.setattr(SyntheticLayoutProbe, "probe", forbidden)
    monkeypatch.setattr(SyntheticSampleBuilder, "build", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


def test_portable_requests_use_exact_persisted_blobs_without_original_host_io(
    prepared_run: _PreparedRun, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = prepared_run.context
    provider = _NoProvider()
    before = _source_history(prepared_run)
    first_store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    baseline = VlmPipelineStage(first_store, provider)._requests(replace(
        original, request=PipelineRunRequest("test", source_root=str(prepared_run.source_root)),
    ))
    assert baseline is not None
    with monkeypatch.context() as guarded:
        _forbid_source_io(guarded, prepared_run.source_root)
        restarted = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
        restored = VlmPipelineStage(restarted, provider)._requests(original)
        assert restored == baseline
        bundle, _policy, requests = restored
        assert requests[0].request_hash == baseline[2][0].request_hash
        assert requests[0].request_payload == baseline[2][0].request_payload
        assert restarted.read_immutable_blob(bundle.source_job, requests[0].proxy_blob) == SOURCE_BYTES
    assert provider.calls == 0
    assert _source_history(prepared_run) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["claim", "bytes"])
async def test_missing_persisted_blob_access_fails_before_provider(
    prepared_run: _PreparedRun, monkeypatch: pytest.MonkeyPatch, missing: str,
) -> None:
    provider = _NoProvider()
    store = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    prepared = VlmPipelineStage(store, provider)._requests(prepared_run.context)
    assert prepared is not None
    target = prepared[2][0].proxy_blob.object_id
    before = _source_history(prepared_run)
    misses = []

    class MissingBlobCursor(psycopg.Cursor):
        """Fault injection only: actual DB rows/constraints remain untouched."""

        def execute(self, query, params=None, **kwargs):
            selector = "JOIN storage.blob_claims" if missing == "claim" else "SELECT content_bytes"
            self.hide_target = selector in str(query) and target in (params or ())
            return super().execute(query, params, **kwargs)

        def fetchone(self):
            if self.hide_target:
                misses.append(missing)
                return None
            return super().fetchone()

    faulted = PostgresRuntimeStore(lambda: psycopg.connect(DSN, cursor_factory=MissingBlobCursor))
    expected = BlobIntegrityError if missing == "claim" else BlobUnavailableError
    message = "not claimed by the attempt Job" if missing == "claim" else "immutable blob bytes are unavailable"
    with monkeypatch.context() as guarded:
        _forbid_source_io(guarded, prepared_run.source_root)
        with pytest.raises(expected, match=message):
            await VlmPipelineStage(faulted, provider).execute(prepared_run.context)
    assert misses == [missing]
    assert provider.calls == 0
    assert _source_history(prepared_run) == before


@pytest.mark.parametrize("changed_field", ["thinking", "prompt"])
def test_replay_never_relabels_old_run_with_current_semantic_policy(
    prepared_run: _PreparedRun, changed_field: str,
) -> None:
    original = prepared_run.context
    policy = original.execution_profile.to_doubao_policy()
    changed = replace(policy, thinking_type="enabled") if changed_field == "thinking" else replace(
        policy, prompt_version=VLM_COMPACT_PROMPT_VERSION,
    )
    current_profile = PipelineExecutionProfile.from_semantic_policies(
        changed, retry_policy=original.execution_profile.to_generation_retry_policy(),
    )
    provider = _NoProvider()
    kernel = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    stage = VlmPipelineStage(kernel, provider)
    baseline = stage._requests(original)
    proposed = stage._requests(replace(original, execution_profile=current_profile))
    assert baseline is not None and proposed is not None
    assert proposed[2][0].request_hash != baseline[2][0].request_hash
    assert proposed[2][0].idempotency_key != baseline[2][0].idempotency_key
    before = _source_history(prepared_run)
    restarted = PostgresPipelineRunStore(lambda: psycopg.connect(DSN))
    replay = restarted._claim_run_sync(
        "pipeline_run_" + uuid4().hex, "portable-original", original.request,
        original.request.request_hash, current_profile,
    )
    assert replay.replayed and replay.snapshot.run_id == original.run_id
    assert replay.snapshot.request == original.request
    assert replay.snapshot.commands[1] == original.command
    assert replay.snapshot.execution_profile == original.execution_profile
    restored = replace(original, execution_profile=replay.snapshot.execution_profile)
    assert stage._requests(restored) == baseline
    assert provider.calls == 0
    assert _source_history(prepared_run) == before
