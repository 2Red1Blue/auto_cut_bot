"""Read-only display projection for exact committed VLM highlight evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, TypeAlias

from autocut_kernel.store import (
    ArtifactScope,
    CommittedArtifactMemberReference,
    CommittedSemanticInputs,
    CommittedSemanticInputsRequest,
    Job,
    RuntimeStoreError,
)
from autocut_kernel.vlm import VlmCandidateHypothesis, VlmCandidateKind

from auto_cut_bot.pipeline.source_prep import (
    SourceManifestDecodeError,
    SourcePrepStore,
    read_persisted_prepared_sources_bundle,
)

from .errors import PipelineRunNotFoundError, PipelineRunValidationError
from .models import PipelineCommand, PipelineRunSnapshot, validate_run_id
from .ports import PipelineRunStore
from .source_prep_stage import source_prep_kernel_idempotency_key
from .vlm_stage import vlm_batch_kernel_idempotency_key

_ARTIFACT_REVISION = 1
_NOT_READY_STATUSES = frozenset({"pending", "running", "indeterminate"})
_FAILED_STATUSES = frozenset({"denied", "failed", "blocked"})


class PipelineHighlightProjectionStore(SourcePrepStore, Protocol):
    """The exact Store reads needed for the committed semantic projection."""

    def read_committed_vlm_semantic_pack_set_reference(
        self,
        job: Job,
        idempotency_key: str,
    ) -> CommittedArtifactMemberReference: ...

    def read_committed_semantic_inputs(
        self,
        request: CommittedSemanticInputsRequest,
    ) -> CommittedSemanticInputs: ...


@dataclass(frozen=True, slots=True)
class PipelineHighlightTimeBase:
    numerator: int
    denominator: int

    def to_mapping(self) -> dict[str, int]:
        return {"numerator": self.numerator, "denominator": self.denominator}


@dataclass(frozen=True, slots=True)
class PipelineHighlightSemanticWindow:
    """A coarse semantic interval, never a physical edit span."""

    start_tick: int
    end_tick: int
    source_time_base: PipelineHighlightTimeBase
    mapping_error_bound_source_ticks: int
    provider_uncertainty_proxy_ticks: int
    provider_uncertainty_proxy_time_base: PipelineHighlightTimeBase
    precision: Literal["coarse_only"] = "coarse_only"

    def to_mapping(self) -> dict[str, object]:
        return {
            "start_tick": self.start_tick,
            "end_tick": self.end_tick,
            "source_time_base": self.source_time_base.to_mapping(),
            "mapping_error_bound_source_ticks": self.mapping_error_bound_source_ticks,
            "provider_uncertainty_proxy_ticks": self.provider_uncertainty_proxy_ticks,
            "provider_uncertainty_proxy_time_base": (
                self.provider_uncertainty_proxy_time_base.to_mapping()
            ),
            "precision": self.precision,
        }


@dataclass(frozen=True, slots=True)
class PipelineHighlightMeasurement:
    kind: str
    value: str
    confidence: str

    def to_mapping(self) -> dict[str, str]:
        return {"kind": self.kind, "value": self.value, "confidence": self.confidence}


@dataclass(frozen=True, slots=True)
class PipelineHighlightDisplayItem:
    """Closed public display model; Kernel provenance never crosses this seam."""

    episode_index: int
    candidate_id: str
    reason: str
    anchor_summary: str
    payoff_summary: str
    dialogue_excerpt: str | None
    tags: tuple[str, ...]
    narrative_functions: tuple[str, ...]
    editing_modes: tuple[str, ...]
    measurements: tuple[PipelineHighlightMeasurement, ...]
    support_confidence: str
    semantic_window: PipelineHighlightSemanticWindow

    def to_mapping(self) -> dict[str, object]:
        return {
            "episode_index": self.episode_index,
            "candidate_id": self.candidate_id,
            "reason": self.reason,
            "anchor_summary": self.anchor_summary,
            "payoff_summary": self.payoff_summary,
            "dialogue_excerpt": self.dialogue_excerpt,
            "tags": list(self.tags),
            "narrative_functions": list(self.narrative_functions),
            "editing_modes": list(self.editing_modes),
            "measurements": [item.to_mapping() for item in self.measurements],
            "support_confidence": self.support_confidence,
            "semantic_window": self.semantic_window.to_mapping(),
        }


@dataclass(frozen=True, slots=True)
class PipelineHighlightsReady:
    status: Literal["ready"] = "ready"
    items: tuple[PipelineHighlightDisplayItem, ...] = ()

    def to_mapping(self) -> dict[str, object]:
        return {"status": self.status, "items": [item.to_mapping() for item in self.items]}


@dataclass(frozen=True, slots=True)
class PipelineHighlightsNotReady:
    status: Literal["not_ready"] = "not_ready"

    def to_mapping(self) -> dict[str, str]:
        return {"status": self.status}


PipelineHighlightsResult: TypeAlias = PipelineHighlightsReady | PipelineHighlightsNotReady


class PipelineHighlightReadService:
    """Project only exact committed VLM evidence for one durable Pipeline run."""

    def __init__(
        self,
        run_store: PipelineRunStore,
        store: PipelineHighlightProjectionStore,
    ) -> None:
        self._run_store = run_store
        self._store = store

    async def get(self, run_id: str) -> PipelineHighlightsResult:
        validate_run_id(run_id)
        snapshot = await self._run_store.read_run(run_id)
        if snapshot is None:
            raise PipelineRunNotFoundError(run_id)
        if snapshot.execution_profile.is_legacy_unresolved:
            raise PipelineRunValidationError(
                "committed highlight evidence requires a frozen VLM execution profile"
            )

        job = Job(run_id, snapshot.request.profile)
        source_command = _stage(snapshot, "source_prep")
        if _is_expected_unready(source_command):
            return PipelineHighlightsNotReady()
        _require_succeeded(source_command, "source preparation")

        try:
            source_outcome = self._store.read_outcome(
                job,
                source_prep_kernel_idempotency_key(run_id),
            )
        except RuntimeStoreError as error:
            raise PipelineRunValidationError(
                "committed highlight source closure is unavailable"
            ) from error
        if source_outcome is None or source_outcome.state != "succeeded":
            raise PipelineRunValidationError(
                "committed highlight source closure is unavailable"
            )
        try:
            source_bundle = read_persisted_prepared_sources_bundle(
                self._store,
                job=job,
                outcome=source_outcome,
                artifact_scope=ArtifactScope("pipeline", "job", run_id),
                artifact_revision=_ARTIFACT_REVISION,
            )
        except (RuntimeStoreError, SourceManifestDecodeError) as error:
            raise PipelineRunValidationError(
                "committed highlight source closure is unavailable"
            ) from error

        vlm_command = _stage(snapshot, "vlm")
        if _is_expected_unready(vlm_command):
            return PipelineHighlightsNotReady()
        _require_succeeded(vlm_command, "VLM")

        vlm_batch_key = vlm_batch_kernel_idempotency_key(
            run_id=run_id,
            source_bundle=source_bundle,
            policy=snapshot.execution_profile.to_doubao_policy(),
            execution_profile_hash=snapshot.execution_profile_hash,
        )
        try:
            vlm_semantic_pack_set = self._store.read_committed_vlm_semantic_pack_set_reference(
                job,
                vlm_batch_key,
            )
        except RuntimeStoreError as error:
            raise PipelineRunValidationError(
                "committed highlight semantic closure is unavailable"
            ) from error
        source_reference = source_bundle.artifact_reference
        try:
            committed = self._store.read_committed_semantic_inputs(
                CommittedSemanticInputsRequest(
                    job=job,
                    source_manifest=CommittedArtifactMemberReference(
                        receipt_id=source_bundle.receipt_id,
                        artifact_set_id=source_bundle.artifact_set_id,
                        member_ordinal=0,
                        scope=source_reference.scope,
                        artifact_type=source_reference.artifact_type,
                        logical_id=source_reference.logical_id,
                        revision=source_reference.revision,
                        content_hash=source_reference.content_hash,
                    ),
                    vlm_semantic_pack_set=vlm_semantic_pack_set,
                ),
            )
        except RuntimeStoreError as error:
            raise PipelineRunValidationError(
                "committed highlight semantic closure is unavailable"
            ) from error
        return PipelineHighlightsReady(items=_project_highlights(committed))


def _stage(snapshot: PipelineRunSnapshot, stage: str) -> PipelineCommand | None:
    commands = tuple(command for command in snapshot.commands if command.stage == stage)
    if len(commands) > 1:
        raise PipelineRunValidationError("pipeline highlight stage identity is ambiguous")
    return commands[0] if commands else None


def _is_expected_unready(command: PipelineCommand | None) -> bool:
    return command is None or command.status in _NOT_READY_STATUSES


def _require_succeeded(command: PipelineCommand | None, label: str) -> None:
    if command is None or command.status in _NOT_READY_STATUSES:
        raise PipelineRunValidationError("pipeline highlight readiness was not resolved")
    if command.status in _FAILED_STATUSES or command.status != "succeeded":
        raise PipelineRunValidationError(f"pipeline {label} command did not succeed")


def _project_highlights(
    committed: CommittedSemanticInputs,
) -> tuple[PipelineHighlightDisplayItem, ...]:
    rows: list[tuple[tuple[object, ...], str, PipelineHighlightDisplayItem]] = []
    for semantic_input in committed.inputs:
        for candidate in semantic_input.semantic_pack.semantic_pack.candidate_hypotheses:
            if candidate.candidate_kind is not VlmCandidateKind.HIGHLIGHT:
                continue
            rows.append(
                (
                    semantic_input.source_window.canonical_order_key,
                    candidate.candidate_id,
                    _display_item(semantic_input.source_window.episode_index, candidate),
                )
            )
    rows.sort(key=lambda row: (row[0], row[1]))
    return tuple(row[2] for row in rows)


def _display_item(
    episode_index: int,
    candidate: VlmCandidateHypothesis,
) -> PipelineHighlightDisplayItem:
    support = candidate.support
    interval = support.source_interval
    return PipelineHighlightDisplayItem(
        episode_index=episode_index,
        candidate_id=candidate.candidate_id,
        reason=candidate.reason,
        anchor_summary=candidate.anchor_summary,
        payoff_summary=candidate.payoff_or_open_question,
        dialogue_excerpt=candidate.dialogue_excerpt,
        tags=tuple(item.value for item in candidate.tags),
        narrative_functions=tuple(item.value for item in candidate.narrative_functions),
        editing_modes=tuple(item.value for item in candidate.editing_modes),
        measurements=tuple(
            PipelineHighlightMeasurement(
                kind=item.measurement_kind.value,
                value=format(item.value, "f"),
                confidence=format(item.confidence, "f"),
            )
            for item in candidate.measurements
        ),
        support_confidence=format(support.confidence, "f"),
        semantic_window=PipelineHighlightSemanticWindow(
            start_tick=interval.coarse_range.start_pts,
            end_tick=interval.coarse_range.end_pts,
            source_time_base=PipelineHighlightTimeBase(
                interval.source_time_base.numerator,
                interval.source_time_base.denominator,
            ),
            mapping_error_bound_source_ticks=interval.mapping_error_bound_source_pts,
            provider_uncertainty_proxy_ticks=interval.provider_uncertainty_proxy_pts,
            provider_uncertainty_proxy_time_base=PipelineHighlightTimeBase(
                interval.proxy_time_base.numerator,
                interval.proxy_time_base.denominator,
            ),
        ),
    )


__all__ = (
    "PipelineHighlightDisplayItem",
    "PipelineHighlightMeasurement",
    "PipelineHighlightProjectionStore",
    "PipelineHighlightReadService",
    "PipelineHighlightSemanticWindow",
    "PipelineHighlightTimeBase",
    "PipelineHighlightsNotReady",
    "PipelineHighlightsReady",
    "PipelineHighlightsResult",
)
