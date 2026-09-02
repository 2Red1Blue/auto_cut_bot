from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from autocut_kernel.context_pack import ContextSelectionPolicy, video_only_window_context_pack
from autocut_kernel.pipeline import (
    PrepareTimedMediaEvidenceRequest,
    PrepareTimedMediaEvidenceResult,
)
from autocut_kernel.registry import AuthorityRegistrySnapshot, TimedSpeechProfileKey
from autocut_kernel.registry.installed_runtime import InstalledLocalRunProfileResolver
from autocut_kernel.registry.timed_speech import StoreAnchoredTimedSpeechProfileResolver
from autocut_kernel.store import CommandOutcome, PostgresRuntimeStore, SemanticInputIntegrityError
from autocut_kernel.store.models import canonical_payload_hash
from autocut_kernel.vlm import ProviderCompleted, ProviderDispatchRequest, ProviderReconcileQuery

from auto_cut_bot.pipeline.media_preflight import (
    BoundedSubprocessRunner,
    CommandOutput,
    LocalMediaPreflightPolicy,
    LocalMediaPreflightPort,
    LocalMediaPreflightRequest,
    LocalMediaPreflightResult,
)
from auto_cut_bot.pipeline.runtime import (
    MediaPreflightRecomputeRequest,
    PipelineCommand,
    PipelineExecutionProfile,
    PipelineRunRequest,
    PipelineRunValidationError,
    PipelineStageContext,
    PipelineStageResult,
    PostgresPipelineRunStore,
)
from auto_cut_bot.pipeline.runtime.media_preflight_stage import (
    MEDIA_PREFLIGHT_EPISODE_MAX_CONCURRENCY,
    MediaPreflightPipelineStage,
    _execute_independent_requests,
    media_preflight_vlm_batch_kernel_idempotency_key,
)
from auto_cut_bot.pipeline.runtime.semantic_authority import (
    load_installed_semantic_run_authority,
)
from auto_cut_bot.pipeline.runtime.source_prep_stage import SourcePrepPipelineStage
from auto_cut_bot.pipeline.runtime.vlm_stage import (
    VlmPipelineStage,
    vlm_batch_kernel_idempotency_key,
)
from auto_cut_bot.pipeline.source_prep import (
    AuthorizedSeriesSourceRoot,
    SourceOperationPolicy,
)
from auto_cut_bot.pipeline.source_prep.models import SeriesSource
from auto_cut_bot.pipeline.source_prep.probe import (
    DECODED_AUDIO_BOUNDARY_GENERATION_POLICY_SHA256,
    IDENTITY_FRAME_GENERATION_POLICY_SHA256,
    FFprobeSourceMediaPort,
)
from tests.pipeline.installed_profile_fixture import synthetic_installed_resource
from tests.pipeline.media_preflight_acceptance_fixture import seed_media_preflight_authority
from tests.pipeline.runtime_profile_fixture import execution_profile, media_preflight_policy
from tests.pipeline.test_local_media_preflight import _SpeechPort
from tests.pipeline.test_pipeline_vlm_stage import _bundle

try:
    import psycopg
except ModuleNotFoundError:
    psycopg = None

DSN = os.environ.get("AUTOCUT_TEST_POSTGRES_DSN")
VERIFY_POSTGRES_DSN = "postgresql://ac_user:ac_password_2026@127.0.0.1:5433/ac_autocut_verify"
MIGRATIONS = Path("packages/autocut-kernel/migrations")
AUTHORITY_SNAPSHOT = AuthorityRegistrySnapshot(
    "sha256:" + "a" * 64,
    TimedSpeechProfileKey("sensevoice_word_guard_v1", "1"),
)


@pytest.mark.asyncio
async def test_media_episode_scheduler_runs_all_children_with_bounded_concurrency() -> None:
    active = 0
    peak = 0
    lock = threading.Lock()

    def execute(value: int) -> tuple[int, str]:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.02)
            return value, "failed" if value == 1 else "succeeded"
        finally:
            with lock:
                active -= 1

    results = await _execute_independent_requests(
        tuple(range(8)),
        execute,
        max_concurrency=MEDIA_PREFLIGHT_EPISODE_MAX_CONCURRENCY,
    )

    assert results == tuple(
        (value, "failed" if value == 1 else "succeeded") for value in range(8)
    )
    assert peak == MEDIA_PREFLIGHT_EPISODE_MAX_CONCURRENCY


@pytest.mark.asyncio
async def test_media_episode_scheduler_settles_siblings_before_raising() -> None:
    settled: list[int] = []
    lock = threading.Lock()

    def execute(value: int) -> int:
        try:
            time.sleep(0.005 * (5 - value))
            if value == 1:
                raise RuntimeError("unexpected adapter bug")
            return value
        finally:
            with lock:
                settled.append(value)

    with pytest.raises(RuntimeError, match="unexpected adapter bug"):
        await _execute_independent_requests(
            tuple(range(5)),
            execute,
            max_concurrency=2,
        )

    assert sorted(settled) == list(range(5))


def _unvalidated_media_request(episode_index: int) -> PrepareTimedMediaEvidenceRequest:
    """Build a type-exact sentinel used only before any Kernel validation seam."""

    request = object.__new__(PrepareTimedMediaEvidenceRequest)
    object.__setattr__(request, "episode_index", episode_index)
    return request


def _stage_context(
    run_id: str,
    profile: PipelineExecutionProfile,
    recompute: MediaPreflightRecomputeRequest | None = None,
) -> PipelineStageContext:
    return PipelineStageContext(
        run_id,
        PipelineRunRequest("test", source_reference="authorized-source"),
        PipelineCommand(
            "media-command",
            "media_preflight",
            "running",
            lease_id="media-test-lease",
        ),
        profile,
        recompute,
    )


@pytest.mark.asyncio
async def test_media_stage_settles_all_children_but_never_finalizes_mixed_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, _blobs = _bundle(3)
    requests = tuple(_unvalidated_media_request(index) for index in range(3))
    calls: list[int] = []
    finalizer_calls = 0

    class FakeCommand:
        def __init__(self, *_args: object) -> None:
            pass

        def execute(self, request: PrepareTimedMediaEvidenceRequest):
            calls.append(request.episode_index)
            state = "failed" if request.episode_index == 1 else "succeeded"
            return PrepareTimedMediaEvidenceResult(
                CommandOutcome(
                    uuid4(),
                    state,
                    receipt_id=uuid4(),
                    artifact_set_id=uuid4() if state == "succeeded" else None,
                    job_id=uuid4(),
                )
            )

    class ForbiddenFinalizer:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            nonlocal finalizer_calls
            finalizer_calls += 1

    resolver = SimpleNamespace(
        snapshot=AUTHORITY_SNAPSHOT,
        resource=SimpleNamespace(
            local_run=SimpleNamespace(
                native_timed_speech=SimpleNamespace(native_port_identity_sha256="sha256:" + "f" * 64)
            )
        ),
        resolve=lambda _store: None,
    )
    stage = object.__new__(MediaPreflightPipelineStage)
    stage._store = object()  # pyright: ignore[reportPrivateUsage]
    stage._port = object()  # pyright: ignore[reportPrivateUsage]
    stage._authority_profile_resolver = resolver  # pyright: ignore[reportPrivateUsage]
    stage._episode_max_concurrency = 2  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(
        "auto_cut_bot.pipeline.runtime.media_preflight_stage.PrepareTimedMediaEvidenceCommand",
        FakeCommand,
    )
    monkeypatch.setattr(
        "auto_cut_bot.pipeline.runtime.media_preflight_stage.FinalizeTimedMediaEvidenceBatchCommand",
        ForbiddenFinalizer,
    )

    result = await stage._execute_batch(  # pyright: ignore[reportPrivateUsage]
        _stage_context(bundle.source_job.job_key, execution_profile()),
        bundle,
        requests,
        _fixture_policy(),
    )

    assert result.outcome.state == "failed"
    assert sorted(calls) == [0, 1, 2]
    assert finalizer_calls == 0


@pytest.mark.asyncio
async def test_selected_media_success_is_inspection_only_and_skips_finalizer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, _blobs = _bundle(2)
    selected_request = _unvalidated_media_request(1)
    finalizer_calls = 0

    class FakeCommand:
        def __init__(self, *_args: object) -> None:
            pass

        def execute(self, _request: PrepareTimedMediaEvidenceRequest):
            return PrepareTimedMediaEvidenceResult(
                CommandOutcome(
                    uuid4(),
                    "succeeded",
                    receipt_id=uuid4(),
                    artifact_set_id=uuid4(),
                    job_id=uuid4(),
                )
            )

    class ForbiddenFinalizer:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            nonlocal finalizer_calls
            finalizer_calls += 1

    resolver = SimpleNamespace(
        snapshot=AUTHORITY_SNAPSHOT,
        resource=SimpleNamespace(
            local_run=SimpleNamespace(
                native_timed_speech=SimpleNamespace(native_port_identity_sha256="sha256:" + "f" * 64)
            )
        ),
        resolve=lambda _store: None,
    )
    stage = object.__new__(MediaPreflightPipelineStage)
    stage._store = object()  # pyright: ignore[reportPrivateUsage]
    stage._port = object()  # pyright: ignore[reportPrivateUsage]
    stage._authority_profile_resolver = resolver  # pyright: ignore[reportPrivateUsage]
    stage._episode_max_concurrency = 2  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(
        "auto_cut_bot.pipeline.runtime.media_preflight_stage.PrepareTimedMediaEvidenceCommand",
        FakeCommand,
    )
    monkeypatch.setattr(
        "auto_cut_bot.pipeline.runtime.media_preflight_stage.FinalizeTimedMediaEvidenceBatchCommand",
        ForbiddenFinalizer,
    )
    recompute = MediaPreflightRecomputeRequest(
        "pipeline_run_" + "b" * 32,
        1,
        (2,),
    )

    result = await stage._execute_batch(  # pyright: ignore[reportPrivateUsage]
        _stage_context(bundle.source_job.job_key, execution_profile(), recompute),
        bundle,
        (selected_request,),
        _fixture_policy(),
    )

    assert result.outcome.state == "succeeded"
    assert finalizer_calls == 0


def test_contextual_media_preflight_reproduces_exact_vlm_batch_identity() -> None:
    bundle, _blobs = _bundle()
    policy = load_installed_semantic_run_authority().vlm_policy
    pack = video_only_window_context_pack(
        ContextSelectionPolicy(),
        "EXTERNAL_CONTEXT_NOT_CONFIGURED",
    )
    expected = vlm_batch_kernel_idempotency_key(
        run_id=bundle.source_job.job_key,
        source_bundle=bundle,
        policy=policy,
        execution_profile_hash="sha256:" + "a" * 64,
        context_packs=(pack,),
    )

    actual = media_preflight_vlm_batch_kernel_idempotency_key(
        run_id=bundle.source_job.job_key,
        source_bundle=bundle,
        policy=policy,
        execution_profile_hash="sha256:" + "a" * 64,
        context_packs=(pack,),
    )

    assert actual == expected


def test_contextual_media_preflight_refuses_missing_committed_context_pack() -> None:
    bundle, _blobs = _bundle()
    policy = load_installed_semantic_run_authority().vlm_policy

    with pytest.raises(PipelineRunValidationError, match="one exact context pack"):
        media_preflight_vlm_batch_kernel_idempotency_key(
            run_id=bundle.source_job.job_key,
            source_bundle=bundle,
            policy=policy,
            execution_profile_hash="sha256:" + "a" * 64,
            context_packs=None,
        )


def test_legacy_media_preflight_keeps_non_context_vlm_batch_identity() -> None:
    bundle, _blobs = _bundle()
    policy = execution_profile().to_doubao_policy()
    expected = vlm_batch_kernel_idempotency_key(
        run_id=bundle.source_job.job_key,
        source_bundle=bundle,
        policy=policy,
        execution_profile_hash="sha256:" + "a" * 64,
    )

    actual = media_preflight_vlm_batch_kernel_idempotency_key(
        run_id=bundle.source_job.job_key,
        source_bundle=bundle,
        policy=policy,
        execution_profile_hash="sha256:" + "a" * 64,
        context_packs=None,
    )

    assert actual == expected


def _fixture_policy(**changes: object) -> LocalMediaPreflightPolicy:
    changes.setdefault("timed_speech_service_sha256", "sha256:" + "2" * 64)
    changes.setdefault("timed_speech_provider_id", "funasr-http-v1")
    changes.setdefault("timed_speech_provider_version", "1.0.0")
    changes.setdefault("asr_model_sha256", "sha256:" + "3" * 64)
    changes.setdefault("vad_model_sha256", "sha256:" + "4" * 64)
    return media_preflight_policy(**changes)


class _SourceResolver:
    def __init__(self, source_root: AuthorizedSeriesSourceRoot) -> None:
        self.source_root = source_root

    def resolve(self, context: PipelineStageContext) -> AuthorizedSeriesSourceRoot:
        assert context.request.source_root == str(self.source_root.root)
        return self.source_root


class _VisibleSemanticPackProvider:
    def __init__(self) -> None:
        self.dispatch_calls = 0

    def dispatch(self, request: ProviderDispatchRequest) -> ProviderCompleted:
        self.dispatch_calls += 1
        payload = json.loads(request.request_payload)
        prompt = payload["prompt"]
        context = json.loads(prompt.split("窗口证据：", 1)[1])
        anchor = context["allowed_frame_anchors"][0]
        proxy_range = context["proxy_range"]
        start = int(anchor["proxy_pts"])
        end = min(start + 1, int(proxy_range["end_pts_exclusive"]))
        if end <= start:
            start -= 1
        support = {
            "confidence": "0.91",
            "proxy_interval": {
                "start_pts": start,
                "end_pts": end,
                "uncertainty_pts": 0,
            },
            "supporting_frame_ids": [anchor["frame_id"]],
        }
        response = {
            "schema_version": 3,
            "window_summary": {
                "summary": "画面中可见测试图案。",
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
                    "display_label": "测试图案",
                    "visual_description": "画面中的测试图案",
                    "support": support,
                }
            ],
            "facts": [
                {
                    "local_fact_id": "fact_1",
                    "fact_kind": "visible_presence",
                    "subject_ref": "entity_1",
                    "object_ref": None,
                    "summary": "测试图案可见。",
                    "support": support,
                }
            ],
            "events": [
                {
                    "local_event_id": "event_1",
                    "event_kind": "reveal",
                    "summary": "测试图案出现。",
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
                    "reason": "测试图案形成明确视觉事件。",
                    "anchor_summary": "测试图案出现。",
                    "payoff_or_open_question": "测试图案完整出现。",
                    "dialogue_excerpt": None,
                    "editing_modes": ["action"],
                    "narrative_functions": ["reveal", "payoff"],
                    "tags": ["action", "reveal"],
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
        }
        return ProviderCompleted(
            json.dumps(response, separators=(",", ":")).encode(),
            "provider-request-real-replay",
        )

    def reconcile(self, query: ProviderReconcileQuery) -> ProviderCompleted:
        raise AssertionError(f"unexpected VLM reconciliation: {query}")


class _CountingRunner:
    def __init__(self) -> None:
        self._delegate = BoundedSubprocessRunner()
        self.visual_calls = 0

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: int,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
    ) -> CommandOutput:
        if "rawvideo" in argv:
            self.visual_calls += 1
        return self._delegate.run(
            argv,
            timeout_seconds=timeout_seconds,
            max_stdout_bytes=max_stdout_bytes,
            max_stderr_bytes=max_stderr_bytes,
        )


class _MustNotDetectPort:
    def __init__(self, changed_environment: LocalMediaPreflightPolicy) -> None:
        self.changed_environment = changed_environment
        self.asr_calls = 0
        self.visual_calls = 0

    def prepare(
        self, request: LocalMediaPreflightRequest, *,
        kernel_max_source_bytes: int, service_max_request_bytes: int,
    ) -> LocalMediaPreflightResult:
        del request, kernel_max_source_bytes, service_max_request_bytes
        self.asr_calls += 1
        self.visual_calls += 1
        raise AssertionError("persisted media evidence must replay without detector calls")


def _make_media(path: Path) -> None:
    completed = subprocess.run(
        [
            shutil.which("ffmpeg") or "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=96x64:rate=10:duration=1.2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=1000:sample_rate=48000:duration=1.2",
            "-c:v",
            "mpeg4",
            "-q:v",
            "5",
            "-c:a",
            "aac",
            str(path),
        ],
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")


def _frozen_policy(source: Path) -> LocalMediaPreflightPolicy:
    content = source.read_bytes()
    probe = FFprobeSourceMediaPort().probe(
        source,
        SeriesSource(
            source.name,
            "fixture-source",
            "sha256:" + hashlib.sha256(content).hexdigest(),
            len(content),
        ),
    )
    base = _fixture_policy(
        analysis_fps_numerator=10,
        max_analysis_frames=20,
        max_stderr_bytes=64 * 1024,
        max_stdout_bytes=32 * 18 * 20,
    )
    generation_policies = {
        "frame": IDENTITY_FRAME_GENERATION_POLICY_SHA256,
        "audio": DECODED_AUDIO_BOUNDARY_GENERATION_POLICY_SHA256,
    }
    producer_ids = {
        "frame": "identity-source-window-v2",
        "audio": "ffprobe-decoded-audio-boundaries-v2",
    }
    base = replace(
        base,
        calibrations=tuple(
            replace(
                item,
                generation_policy_sha256=generation_policies.get(
                    item.producer_kind,
                    item.generation_policy_sha256,
                ),
                producer_id=producer_ids.get(item.producer_kind, item.producer_id),
                timing_error_bound_microseconds=(
                    100_000
                    if item.producer_kind in ("asr", "subtitle", "vad")
                    else item.timing_error_bound_microseconds
                ),
            )
            for item in base.calibrations
        ),
    )
    measurement_port = LocalMediaPreflightPort(speech_port=_SpeechPort())
    measured = measurement_port.measure_detector_identity_sha256s(base)
    measured["frame"] = probe.frame_detector_sha256
    measured["audio"] = probe.audio_detector_sha256
    measured["asr"] = base.timed_speech_detector_sha256("asr")
    measured["vad"] = base.timed_speech_detector_sha256("vad")
    return replace(
        base,
        calibrations=tuple(
            replace(item, detector_sha256=measured[item.producer_kind])
            for item in base.calibrations
        ),
    )


def _artifact_identity(command_name: str) -> tuple[str, str]:
    assert DSN is not None
    assert psycopg is not None
    with psycopg.connect(DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT receipt_id, result_artifact_set_id
                  FROM runtime.command_receipts AS receipt
                  JOIN runtime.command_slots AS command
                    ON command.command_slot_id = receipt.command_slot_id
                 WHERE command.command_name = %s
                """,
                (command_name,),
            )
            rows = cursor.fetchall()
    assert len(rows) == 1
    return str(rows[0][0]), str(rows[0][1])


def _forge_semantic_pack_with_recomputed_member_and_set_hash() -> None:
    assert DSN is not None
    assert psycopg is not None
    with psycopg.connect(DSN, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT artifact.artifact_id, artifact.artifact_set_id,
                       artifact.payload_json
                  FROM runtime.artifacts AS artifact
                 WHERE artifact.artifact_type = 'vlm_semantic_pack'
                """
            )
            rows = cursor.fetchall()
            assert len(rows) == 1
            artifact_id, artifact_set_id, payload = rows[0]
            forged = dict(payload)
            forged["window_summary"]["summary"] = "伪造但内部闭合的摘要。"
            forged["facts"][0]["summary"] = "伪造但内部闭合的摘要。"
            payload_json = json.dumps(
                forged,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            content_hash = canonical_payload_hash(payload_json)
            cursor.execute("ALTER TABLE runtime.artifacts DISABLE TRIGGER USER")
            cursor.execute("ALTER TABLE runtime.artifact_sets DISABLE TRIGGER USER")
            try:
                cursor.execute(
                    """
                    UPDATE runtime.artifacts
                       SET payload_json = %s::jsonb, content_hash = %s
                     WHERE artifact_id = %s
                    """,
                    (payload_json, content_hash, artifact_id),
                )
                cursor.execute(
                    """
                    SELECT artifact.artifact_type, artifact.logical_id,
                           artifact.revision, artifact.namespace,
                           artifact.scope_kind, artifact.scope_key,
                           artifact.content_hash, artifact.payload_json
                      FROM runtime.artifact_set_members AS member
                      JOIN runtime.artifacts AS artifact
                        ON artifact.artifact_id = member.artifact_id
                     WHERE member.artifact_set_id = %s
                     ORDER BY member.ordinal
                    """,
                    (artifact_set_id,),
                )
                canonical_members = [
                    {
                        "artifact_type": row[0],
                        "content_hash": row[6],
                        "logical_id": row[1],
                        "payload_json": row[7],
                        "revision": row[2],
                        "scope": {
                            "key": row[5],
                            "kind": row[4],
                            "namespace": row[3],
                        },
                    }
                    for row in cursor.fetchall()
                ]
                encoded = json.dumps(
                    canonical_members,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode()
                set_hash = "sha256:" + hashlib.sha256(encoded).hexdigest()
                cursor.execute(
                    "UPDATE runtime.artifact_sets SET set_hash = %s WHERE artifact_set_id = %s",
                    (set_hash, artifact_set_id),
                )
            finally:
                cursor.execute("ALTER TABLE runtime.artifacts ENABLE TRIGGER USER")
                cursor.execute("ALTER TABLE runtime.artifact_sets ENABLE TRIGGER USER")


@pytest.mark.skipif(
    psycopg is None
    or DSN != VERIFY_POSTGRES_DSN
    or shutil.which("ffmpeg") is None
    or shutil.which("ffprobe") is None,
    reason="disposable PostgreSQL, ffmpeg and ffprobe are required",
)
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario",
    ("valid", "forged-pack", "missing-render-grant", "missing-semantic-grant"),
)
async def test_postgres_restart_reconcile_replays_original_receipt_without_detectors(
    tmp_path: Path,
    scenario: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real PG/media replay; synthetic raw calibration, VLM and Stage1-3 scheduling.

    Calibration acceptance is produced by the real independent validator and
    bootstrapped through the installed admin path. No real model is invoked and
    the synthetic Stage1-3 control-plane receipts are not semantic acceptance.
    """
    assert DSN is not None
    assert psycopg is not None
    with psycopg.connect(DSN, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA IF EXISTS runtime CASCADE")
            cursor.execute("DROP SCHEMA IF EXISTS storage CASCADE")
            for migration in sorted(MIGRATIONS.glob("*.sql")):
                cursor.execute(migration.read_text(encoding="utf-8"))

    source_root = tmp_path / "source"
    source_root.mkdir()
    source = source_root / "episode.mp4"
    _make_media(source)
    frozen_policy = _frozen_policy(source)
    request = PipelineRunRequest("test", source_root=str(source_root.resolve()))

    def factory():
        assert psycopg is not None
        return psycopg.connect(DSN)

    run_store = PostgresPipelineRunStore(factory)
    materialization_root = tmp_path / "verified-media-staging"
    kernel_store = PostgresRuntimeStore(
        factory,
        materialization_staging_root=materialization_root,
    )
    resolver, frozen_policy = seed_media_preflight_authority(
        kernel_store, tmp_path, monkeypatch, frozen_policy,
    )
    profile = execution_profile(
        media_policy=frozen_policy,
        stage1_policy=resolver.resource.narrative.command_policy,
        stage2_policy=resolver.resource.local_run.stage2_command_policy,
        stage3_policy=resolver.resource.local_run.stage3_command_policy,
    )
    run_id = f"pipeline_run_{uuid4().hex}"
    claimed = await run_store.claim_run(
        run_id=run_id,
        idempotency_key="real-media-restart-replay",
        request=request,
        request_hash=request.request_hash,
        execution_profile=profile,
    )
    assert tuple(command.stage for command in claimed.snapshot.commands) == (
        "source_prep", "vlm", "stage1_narrative", "stage2_portfolio",
        "stage3_blueprint", "media_preflight",
    )

    async def claim_context() -> PipelineStageContext:
        snapshot = await run_store.read_run(run_id)
        assert snapshot is not None
        command = await run_store.claim_next_pending(
            run_id,
            expected_version=0,
            lease_id=f"lease-{snapshot.commands[0].stage}-{uuid4().hex}",
        )
        assert command is not None
        return PipelineStageContext(run_id, request, command, snapshot.execution_profile)

    async def project(context: PipelineStageContext, result: PipelineStageResult) -> None:
        assert result.outcome == "succeeded" and result.receipt_id is not None
        assert context.command.lease_id is not None
        await run_store.record_result(
            run_id,
            result=result,
            expected_version=context.command.version,
            lease_id=context.command.lease_id,
        )

    source_context = await claim_context()
    source_result = await SourcePrepPipelineStage(
        kernel_store,
        _SourceResolver(
            AuthorizedSeriesSourceRoot(
                source_root.resolve(),
                SourceOperationPolicy(
                    "fixture-authority",
                    "fixture-series",
                    1,
                    (
                        ("semantic_analysis",)
                        if scenario == "missing-render-grant"
                        else (
                            ("render_source",)
                            if scenario == "missing-semantic-grant"
                            else ("semantic_analysis", "render_source")
                        )
                    ),
                ),
            )
        ),
    ).execute(source_context)
    await project(source_context, source_result)

    provider = _VisibleSemanticPackProvider()
    if scenario != "missing-semantic-grant":
        vlm_context = await claim_context()
        vlm_result = await VlmPipelineStage(
            kernel_store, provider, installed_profile=resolver.resource,
        ).execute(vlm_context)
        await project(vlm_context, vlm_result)
        assert provider.dispatch_calls == 1
        narrative_context = await claim_context()
        assert narrative_context.command.stage == "stage1_narrative"
        # This remote test covers only media replay/control-plane sequencing.
        # No Kernel Stage 1 members, Admission or production authority are minted.
        await project(
            narrative_context,
            PipelineStageResult(narrative_context.command.command_id, "succeeded", uuid4()),
        )
        portfolio_context = await claim_context()
        assert portfolio_context.command.stage == "stage2_portfolio"
        # This remote test covers only media replay/control-plane sequencing.
        # No Kernel Stage 2 portfolio members or production authority are minted.
        await project(
            portfolio_context,
            PipelineStageResult(portfolio_context.command.command_id, "succeeded", uuid4()),
        )
        editorial_context = await claim_context()
        assert editorial_context.command.stage == "stage3_blueprint"
        # Scheduler-only progression: no Kernel Stage3 ArtifactSet is invented.
        await project(
            editorial_context,
            PipelineStageResult(editorial_context.command.command_id, "succeeded", uuid4()),
        )

    if scenario == "forged-pack":
        _forge_semantic_pack_with_recomputed_member_and_set_hash()

    if scenario == "missing-semantic-grant":
        snapshot = await run_store.read_run(run_id)
        assert snapshot is not None
        media_context = PipelineStageContext(
            run_id,
            request,
            snapshot.commands[5],
            snapshot.execution_profile,
        )
    else:
        media_context = await claim_context()
    assert media_context.command.stage == "media_preflight"
    speech = _SpeechPort()
    runner = _CountingRunner()
    if scenario in ("missing-render-grant", "missing-semantic-grant"):

        def unexpected_vlm_read(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("source authorization must precede every VLM read")

        monkeypatch.setattr(
            kernel_store,
            "read_committed_vlm_semantic_pack_set_reference",
            unexpected_vlm_read,
        )
        monkeypatch.setattr(
            kernel_store,
            "read_committed_semantic_inputs",
            unexpected_vlm_read,
        )
    first_stage = MediaPreflightPipelineStage(
        kernel_store,
        LocalMediaPreflightPort(speech_port=speech, runner=runner),
        resolver,
    )
    if scenario in ("missing-render-grant", "missing-semantic-grant"):
        missing_purpose = (
            "render_source" if scenario == "missing-render-grant" else "semantic_analysis"
        )
        with pytest.raises(PipelineRunValidationError, match=missing_purpose):
            await first_stage.execute(media_context)
        with pytest.raises(PipelineRunValidationError, match=missing_purpose):
            await first_stage.reconcile(media_context)
        assert len(speech.requests) == 0
        assert runner.visual_calls == 0
        return
    if scenario == "forged-pack":
        with pytest.raises(SemanticInputIntegrityError, match="member/blob/owner"):
            await first_stage.execute(media_context)
        assert len(speech.requests) == 0
        assert runner.visual_calls == 0
        return
    first = await first_stage.execute(media_context)
    assert first.outcome == "succeeded" and first.receipt_id is not None
    assert len(speech.requests) == 1
    assert runner.visual_calls == 1
    child_identity = _artifact_identity("PrepareTimedMediaEvidence@2.1.3")
    batch_identity = _artifact_identity("FinalizeTimedMediaEvidenceBatch@2.1.3")
    assert batch_identity[0] == str(first.receipt_id)

    restarted_run_store = PostgresPipelineRunStore(factory)
    restarted_kernel_store = PostgresRuntimeStore(
        factory,
        materialization_staging_root=materialization_root,
    )
    restarted_snapshot = await restarted_run_store.read_run(claimed.snapshot.run_id)
    assert restarted_snapshot is not None
    persisted_command = restarted_snapshot.commands[5]
    assert persisted_command.stage == "media_preflight"
    assert persisted_command.status == "running"
    changed_environment = _fixture_policy(asr_model_revision="changed-after-restart")
    assert changed_environment.canonical_hash != frozen_policy.canonical_hash
    must_not_detect = _MustNotDetectPort(changed_environment)
    restarted_stage = MediaPreflightPipelineStage(
        restarted_kernel_store,
        must_not_detect,  # type: ignore[arg-type]
        InstalledLocalRunProfileResolver(resolver.resource),
    )
    replay = await restarted_stage.reconcile(
        PipelineStageContext(
            run_id,
            request,
            persisted_command,
            restarted_snapshot.execution_profile,
        )
    )

    assert replay is not None and replay.outcome == "succeeded"
    assert replay.receipt_id == first.receipt_id
    assert _artifact_identity("PrepareTimedMediaEvidence@2.1.3") == child_identity
    assert _artifact_identity("FinalizeTimedMediaEvidenceBatch@2.1.3") == batch_identity
    assert must_not_detect.asr_calls == 0
    assert must_not_detect.visual_calls == 0
    assert (
        restarted_snapshot.execution_profile.to_media_preflight_policy().canonical_hash
        == frozen_policy.canonical_hash
    )


def test_media_preflight_stage_constructor_requires_an_explicit_resolver() -> None:
    resolver = InstalledLocalRunProfileResolver(synthetic_installed_resource())

    stage = MediaPreflightPipelineStage(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        resolver,
    )

    assert isinstance(stage, MediaPreflightPipelineStage)
    for invalid in (object(), StoreAnchoredTimedSpeechProfileResolver(AUTHORITY_SNAPSHOT)):
        with pytest.raises(PipelineRunValidationError, match="installed"):
            MediaPreflightPipelineStage(
                object(),  # type: ignore[arg-type]
                object(),  # type: ignore[arg-type]
                invalid,  # type: ignore[arg-type]
            )
    for invalid_concurrency in (0, -1, True):
        with pytest.raises(PipelineRunValidationError, match="episode_max_concurrency"):
            MediaPreflightPipelineStage(
                object(),  # type: ignore[arg-type]
                object(),  # type: ignore[arg-type]
                resolver,
                episode_max_concurrency=invalid_concurrency,
            )


def test_calibration_seed_raw_derives_exact_bound_without_accepted_fixture() -> None:
    from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes
    from autocut_kernel.registry.authority_profiles import (
        decode_shadow_calibration_profile_source,
        decode_stage1_narrative_profile_source,
    )

    from auto_cut_bot.pipeline.media_preflight.shadow_calibration_service_profile import (
        build_funasr_shadow_service_profile,
    )
    from tests.pipeline.media_preflight_acceptance_fixture import (
        SyntheticCalibrationRawPort,
        calibration_measurement_request,
        calibration_seed_values,
    )

    base = _fixture_policy()
    policy = replace(base, calibrations=tuple(
        replace(item, detector_sha256=base.timed_speech_detector_sha256(item.producer_kind))
        if item.producer_kind in ("asr", "vad") else item for item in base.calibrations
    ))
    narrative_raw, shadow_raw, contexts = calibration_seed_values(policy)
    narrative = decode_stage1_narrative_profile_source(canonical_json_bytes(narrative_raw))
    shadow = decode_shadow_calibration_profile_source(
        canonical_json_bytes(shadow_raw), narrative=narrative,
        expected_profile_contract_sha256=shadow_raw["profile_contract_sha256"],
    )
    assert build_funasr_shadow_service_profile(
        profile=shadow, narrative=narrative,
        expected_profile_contract_sha256=shadow.profile_contract_sha256,
    )
    request = calibration_measurement_request(shadow, "sha256:" + "a" * 64, contexts)
    assert len(request.corpus_members) == 2
    for member in request.corpus_members:
        result = SyntheticCalibrationRawPort().measure(request, member)
        assert result.projection.summary.asr.accepted_bound_tick == 4800
        assert result.projection.summary.vad.accepted_bound_tick == 4800
        assert member.raw_context.source_byte_limits.service_max_request_bytes == (
            shadow.native_timed_speech.max_request_bytes
        )


def test_media_preflight_context_rejects_historical_v3_profile() -> None:
    mapping = execution_profile(media_policy=_fixture_policy()).to_mapping()
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
    v3 = PipelineExecutionProfile.from_mapping(mapping)

    with pytest.raises(PipelineRunValidationError, match="physical/story stages require execution profile v9"):
        PipelineStageContext(
            "pipeline_run_" + "b" * 32,
            PipelineRunRequest("test", source_root="/authorized/source"),
            PipelineCommand("media-command", "media_preflight", "pending"),
            v3,
        )
