from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import ANY
from uuid import uuid4

import pytest
from autocut_kernel.store import (
    ArtifactScope,
    CommittedArtifactMemberReference,
    SemanticInputIntegrityError,
    SemanticInputUnavailableError,
)
from autocut_kernel.vlm import VlmCandidateKind

from auto_cut_bot.pipeline.runtime.errors import (
    PipelineRunNotFoundError,
    PipelineRunValidationError,
)
from auto_cut_bot.pipeline.runtime.highlight_projection import PipelineHighlightReadService
from auto_cut_bot.pipeline.runtime.models import PipelineCommand, PipelineRunRequest

RUN_ID = "pipeline_run_" + "a" * 32


class _RunStore:
    def __init__(self, snapshot: object | None) -> None:
        self.snapshot = snapshot
        self.calls: list[str] = []

    async def read_run(self, run_id: str) -> object | None:
        self.calls.append(run_id)
        return self.snapshot


class _SemanticStore:
    def __init__(self, *, outcome: object | None = None, committed: object | None = None) -> None:
        self.outcome = outcome
        self.committed = committed
        self.outcome_calls: list[tuple[object, str]] = []
        self.batch_calls: list[tuple[object, str]] = []
        self.semantic_requests: list[object] = []
        self.batch_error: Exception | None = None
        self.semantic_error: Exception | None = None

    def read_outcome(self, job: object, idempotency_key: str) -> object | None:
        self.outcome_calls.append((job, idempotency_key))
        return self.outcome

    def read_committed_vlm_semantic_pack_set_reference(
        self, job: object, idempotency_key: str
    ) -> CommittedArtifactMemberReference:
        self.batch_calls.append((job, idempotency_key))
        if self.batch_error is not None:
            raise self.batch_error
        return _member("vlm_semantic_pack_set", "vlm_semantic_pack_set", 0)

    def read_committed_semantic_inputs(self, request: object) -> object:
        self.semantic_requests.append(request)
        if self.semantic_error is not None:
            raise self.semantic_error
        assert self.committed is not None
        return self.committed


def _command(stage: str, status: str) -> PipelineCommand:
    receipt_id = uuid4() if status in {"succeeded", "denied", "failed"} else None
    blocking_command_id = "source-command" if status == "blocked" else None
    return PipelineCommand(
        f"{stage}-command",
        stage,
        status,  # type: ignore[arg-type]
        receipt_id,
        lease_id="lease" if status == "running" else None,
        blocking_command_id=blocking_command_id,
    )


def _snapshot(*commands: PipelineCommand, profile_hash: str = "profile-sha") -> object:
    return SimpleNamespace(
        commands=commands,
        request=PipelineRunRequest("test", source_root="/authorized/source"),
        execution_profile=SimpleNamespace(is_legacy_unresolved=False),
        execution_profile_hash=profile_hash,
    )


def _member(artifact_type: str, logical_id: str, ordinal: int) -> CommittedArtifactMemberReference:
    return CommittedArtifactMemberReference(
        receipt_id=uuid4(),
        artifact_set_id=uuid4(),
        member_ordinal=ordinal,
        scope=ArtifactScope("pipeline", "job", RUN_ID),
        artifact_type=artifact_type,
        logical_id=logical_id,
        revision=1,
        content_hash="sha256:" + "a" * 64,
    )


def _source_bundle() -> object:
    reference = SimpleNamespace(
        scope=ArtifactScope("pipeline", "job", RUN_ID),
        artifact_type="whole_series_source_manifest",
        logical_id="whole_series_source_manifest",
        revision=1,
        content_hash="sha256:" + "b" * 64,
    )
    return SimpleNamespace(
        receipt_id=uuid4(),
        artifact_set_id=uuid4(),
        artifact_reference=reference,
    )


def _candidate(candidate_id: str, kind: VlmCandidateKind, start: int) -> object:
    source_time_base = SimpleNamespace(numerator=1, denominator=90_000)
    proxy_time_base = SimpleNamespace(numerator=1, denominator=1_000)
    interval = SimpleNamespace(
        coarse_range=SimpleNamespace(start_pts=start, end_pts=start + 10),
        source_time_base=source_time_base,
        mapping_error_bound_source_pts=2,
        provider_uncertainty_proxy_pts=3,
        proxy_time_base=proxy_time_base,
    )
    support = SimpleNamespace(confidence=Decimal("0.91"), source_interval=interval)
    measurement = SimpleNamespace(
        measurement_kind=SimpleNamespace(value="visual_salience"),
        value=Decimal("0.88"),
        confidence=Decimal("0.87"),
        fact_refs=("forbidden-fact-ref",),
        event_refs=("forbidden-event-ref",),
    )
    return SimpleNamespace(
        candidate_id=candidate_id,
        local_candidate_id="forbidden-local-id",
        candidate_kind=kind,
        reason="A concise reason.",
        anchor_summary="Anchor.",
        payoff_or_open_question="Payoff.",
        dialogue_excerpt=None,
        tags=(SimpleNamespace(value="action"),),
        narrative_functions=(SimpleNamespace(value="reveal"),),
        editing_modes=(SimpleNamespace(value="action"),),
        measurements=(measurement,),
        support=support,
        supporting_event_refs=("forbidden-event-ref",),
        context_event_refs=(),
        payoff_event_refs=("forbidden-event-ref",),
        anchor_event_ref="forbidden-event-ref",
    )


def _committed() -> object:
    first = SimpleNamespace(
        source_window=SimpleNamespace(canonical_order_key=(1, 0, 20, 30, "window-b"), episode_index=1),
        semantic_pack=SimpleNamespace(
            semantic_pack=SimpleNamespace(
                candidate_hypotheses=(
                    _candidate("candidate-b", VlmCandidateKind.HIGHLIGHT, 20),
                    _candidate("candidate-hook", VlmCandidateKind.HOOK, 20),
                    _candidate("candidate-a", VlmCandidateKind.HIGHLIGHT, 20),
                )
            )
        ),
    )
    second = SimpleNamespace(
        source_window=SimpleNamespace(canonical_order_key=(0, 0, 5, 10, "window-a"), episode_index=0),
        semantic_pack=SimpleNamespace(
            semantic_pack=SimpleNamespace(
                candidate_hypotheses=(
                    _candidate("candidate-c", VlmCandidateKind.HIGHLIGHT, 5),
                )
            )
        ),
    )
    return SimpleNamespace(inputs=(first, second))


def _patch_source_bundle(monkeypatch: pytest.MonkeyPatch, bundle: object) -> list[dict[str, object]]:
    from auto_cut_bot.pipeline.runtime import highlight_projection

    calls: list[dict[str, object]] = []

    def read_bundle(*args: object, **kwargs: object) -> object:
        del args
        calls.append(dict(kwargs))
        return bundle

    monkeypatch.setattr(highlight_projection, "read_persisted_prepared_sources_bundle", read_bundle)
    return calls


@pytest.mark.asyncio
async def test_projects_exact_committed_highlights_with_profile_bound_batch_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from auto_cut_bot.pipeline.runtime import highlight_projection

    bundle = _source_bundle()
    source_calls = _patch_source_bundle(monkeypatch, bundle)
    batch_inputs: list[dict[str, object]] = []

    def batch_key(**kwargs: object) -> str:
        batch_inputs.append(kwargs)
        return "vlm-batch:exact-profile-bound-key"

    monkeypatch.setattr(highlight_projection, "vlm_batch_kernel_idempotency_key", batch_key)
    store = _SemanticStore(outcome=SimpleNamespace(state="succeeded"), committed=_committed())
    service = PipelineHighlightReadService(
        _RunStore(_snapshot(_command("source_prep", "succeeded"), _command("vlm", "succeeded"), profile_hash="profile-hash")),
        store,  # type: ignore[arg-type]
    )

    result = await service.get(RUN_ID)

    assert result.to_mapping() == {
        "status": "ready",
        "items": [
            {
                "episode_index": 0,
                "candidate_id": "candidate-c",
                "reason": "A concise reason.",
                "anchor_summary": "Anchor.",
                "payoff_summary": "Payoff.",
                "dialogue_excerpt": None,
                "tags": ["action"],
                "narrative_functions": ["reveal"],
                "editing_modes": ["action"],
                "measurements": [{"kind": "visual_salience", "value": "0.88", "confidence": "0.87"}],
                "support_confidence": "0.91",
                "semantic_window": {
                    "start_tick": 5,
                    "end_tick": 15,
                    "source_time_base": {"numerator": 1, "denominator": 90_000},
                    "mapping_error_bound_source_ticks": 2,
                    "provider_uncertainty_proxy_ticks": 3,
                    "provider_uncertainty_proxy_time_base": {"numerator": 1, "denominator": 1_000},
                    "precision": "coarse_only",
                },
            },
            ANY,
            ANY,
        ],
    }
    assert [item["candidate_id"] for item in result.to_mapping()["items"]] == [
        "candidate-c",
        "candidate-a",
        "candidate-b",
    ]
    assert source_calls[0]["artifact_scope"] == ArtifactScope("pipeline", "job", RUN_ID)
    assert batch_inputs == [
        {"run_id": RUN_ID, "source_bundle": bundle, "execution_profile_hash": "profile-hash"}
    ]
    assert store.batch_calls[0][1] == "vlm-batch:exact-profile-bound-key"
    request = store.semantic_requests[0]
    assert request.source_manifest.receipt_id == bundle.receipt_id
    assert request.source_manifest.artifact_set_id == bundle.artifact_set_id
    assert request.source_manifest.member_ordinal == 0
    rendered = json.dumps(result.to_mapping(), sort_keys=True)
    for forbidden in (
        "source_id",
        "source_sha256",
        "receipt_id",
        "artifact_set_id",
        "object_id",
        "raw_response",
        "fact_refs",
        "event_refs",
        "local_candidate_id",
        "proxy_interval",
        "core_owner_window",
    ):
        assert forbidden not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize("stage,status", [("source_prep", value) for value in ("pending", "running", "indeterminate")] + [("vlm", value) for value in ("pending", "running", "indeterminate")])
async def test_only_expected_absent_or_nonterminal_states_are_not_ready(
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    status: str,
) -> None:
    bundle = _source_bundle()
    _patch_source_bundle(monkeypatch, bundle)
    commands = [_command("source_prep", "succeeded")]
    if stage == "source_prep":
        commands[0] = _command("source_prep", status)
    else:
        commands.append(_command("vlm", status))
    store = _SemanticStore(outcome=SimpleNamespace(state="succeeded"), committed=_committed())

    result = await PipelineHighlightReadService(
        _RunStore(_snapshot(*commands)), store  # type: ignore[arg-type]
    ).get(RUN_ID)

    assert result.to_mapping() == {"status": "not_ready"}
    assert not store.batch_calls
    assert not store.semantic_requests


@pytest.mark.asyncio
async def test_absent_vlm_is_not_ready_but_terminal_and_exact_read_failures_are_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from auto_cut_bot.pipeline.runtime import highlight_projection

    bundle = _source_bundle()
    _patch_source_bundle(monkeypatch, bundle)
    monkeypatch.setattr(
        highlight_projection,
        "vlm_batch_kernel_idempotency_key",
        lambda **_kwargs: "vlm-batch:exact-profile-bound-key",
    )
    source = _command("source_prep", "succeeded")
    store = _SemanticStore(outcome=SimpleNamespace(state="succeeded"), committed=_committed())
    service = PipelineHighlightReadService(_RunStore(_snapshot(source)), store)  # type: ignore[arg-type]
    assert (await service.get(RUN_ID)).to_mapping() == {"status": "not_ready"}

    denied = PipelineHighlightReadService(
        _RunStore(_snapshot(_command("source_prep", "denied"))), store  # type: ignore[arg-type]
    )
    with pytest.raises(PipelineRunValidationError):
        await denied.get(RUN_ID)

    store.batch_error = SemanticInputUnavailableError("missing exact pack set")
    failed_exact_read = PipelineHighlightReadService(
        _RunStore(_snapshot(source, _command("vlm", "succeeded"))), store  # type: ignore[arg-type]
    )
    with pytest.raises(PipelineRunValidationError):
        await failed_exact_read.get(RUN_ID)
    store.batch_error = None
    store.semantic_error = SemanticInputIntegrityError("tampered closure")
    with pytest.raises(PipelineRunValidationError):
        await failed_exact_read.get(RUN_ID)


@pytest.mark.asyncio
async def test_unknown_and_malformed_run_ids_fail_before_evidence_reads() -> None:
    run_store = _RunStore(None)
    store = _SemanticStore()
    service = PipelineHighlightReadService(run_store, store)  # type: ignore[arg-type]

    with pytest.raises(PipelineRunValidationError):
        await service.get("not-a-run")
    assert not run_store.calls
    with pytest.raises(PipelineRunNotFoundError):
        await service.get(RUN_ID)
    assert not store.outcome_calls
