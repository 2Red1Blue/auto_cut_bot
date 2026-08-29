"""Durable adapter from committed source preparation to Kernel VLM commands."""

# pyright: reportMissingTypeStubs=false

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from typing import Protocol

from autocut_kernel.context_pack import (
    ContextSelectionPolicy,
    OwnerEpisodeMapSet,
    WindowContextPack,
)
from autocut_kernel.pipeline import (
    FinalizeVlmBatchCommand,
    FinalizeVlmBatchRequest,
    FinalizeVlmBatchResult,
    GenerateVlmEvidenceCommand,
    GenerateVlmEvidenceRequest,
    GenerateVlmEvidenceResult,
    GenerationStore,
    VlmBatchChildOutcome,
    VlmBatchFinalizerStore,
)
from autocut_kernel.registry.installed_local_run import LocalRunResource
from autocut_kernel.store import (
    VLM_BATCH_IDEMPOTENCY_PREFIX,
    ArtifactScope,
    CommandOutcome,
    Job,
)
from autocut_kernel.store.models import VLM_BATCH_FINALIZER_STRATEGY_VERSION, canonical_recipe_scope
from autocut_kernel.store.vlm_v4 import VLM_BATCH_FINALIZER_STRATEGY_VERSION_V4
from autocut_kernel.vlm import VlmProviderPort
from autocut_kernel.vlm.semantic_contracts import VLM_PARSER_V4

from auto_cut_bot.pipeline.context_prepare import (
    ContextPrepareStore,
    PrepareWindowContextRequest,
    find_committed_window_context_packs,
    read_committed_window_context_packs,
)
from auto_cut_bot.pipeline.media_preflight.installed_policy import validate_installed_media_policy
from auto_cut_bot.pipeline.source_prep import (
    PersistedPreparedSources,
    SourcePrepStore,
    read_persisted_prepared_sources_bundle,
)
from auto_cut_bot.pipeline.vlm import (
    DOUBAO_VLM_LEGACY_STAGE_STRATEGY_VERSION,
    DOUBAO_VLM_PARALLEL_STAGE_STRATEGY_VERSION,
    DOUBAO_VLM_STAGE_STRATEGY_VERSION,
    DoubaoVlmRequestPolicy,
    build_doubao_vlm_request,
)
from auto_cut_bot.pipeline.vlm.contextual_video_prompt import (
    VLM_CONTEXTUAL_CLOSED_VOCABULARY_CORE_VIDEO_PROMPT_VERSION,
    VLM_CONTEXTUAL_COMPACT_CANONICAL_CORE_VIDEO_PROMPT_VERSION,
    VLM_CONTEXTUAL_CORE_VIDEO_PROMPT_VERSION,
    VLM_CONTEXTUAL_ENUM_DISAMBIGUATED_FACT_ANCHORED_EVENT_CORE_VIDEO_PROMPT_VERSION,
    VLM_CONTEXTUAL_FACT_ANCHORED_EVENT_CORE_VIDEO_PROMPT_VERSION,
    VLM_CONTEXTUAL_RECIPROCAL_CAUSAL_CORE_VIDEO_PROMPT_VERSION,
    VLM_CONTEXTUAL_REQUIRED_EMPTY_ARRAY_CORE_VIDEO_PROMPT_VERSION,
    VLM_CONTEXTUAL_STABLE_CORE_VIDEO_PROMPT_VERSION,
    VLM_CONTEXTUAL_STRICT_WIRE_CORE_VIDEO_PROMPT_VERSION,
    VLM_CONTEXTUAL_TIMELINE_CORE_VIDEO_PROMPT_VERSION,
    VLM_CONTEXTUAL_VALIDATED_RECIPROCAL_CAUSAL_CORE_VIDEO_PROMPT_VERSION,
    VLM_CONTEXTUAL_VIDEO_PROMPT_VERSION,
)
from auto_cut_bot.pipeline.vlm.policy_binding import (
    validate_installed_source_sampling,
    validate_installed_vlm_policy,
)
from auto_cut_bot.pipeline.vlm.request_factory import (
    DOUBAO_VLM_ENUM_DISAMBIGUATED_FACT_ANCHORED_EVENT_VIDEO_STAGE_STRATEGY_VERSION,
    DOUBAO_VLM_FACT_ANCHORED_EVENT_VIDEO_STAGE_STRATEGY_VERSION,
    DOUBAO_VLM_REQUIRED_EMPTY_ARRAY_VIDEO_STAGE_STRATEGY_VERSION,
    DOUBAO_VLM_STABLE_VIDEO_STAGE_STRATEGY_VERSION,
    DOUBAO_VLM_STRICT_WIRE_VIDEO_STAGE_STRATEGY_VERSION,
    DOUBAO_VLM_VALIDATED_VIDEO_STAGE_STRATEGY_VERSION,
    DOUBAO_VLM_VIDEO_STAGE_STRATEGY_VERSION,
)

from .context_prepare_stage import context_prepare_kernel_idempotency_key
from .errors import PipelineRunValidationError
from .models import PipelineStageContext, PipelineStageResult, validate_run_id
from .source_prep_stage import (
    require_committed_source_operation,
    source_prep_kernel_idempotency_key,
)

VLM_LEGACY_EPISODE_SELECTION_STRATEGY_VERSION = "all-committed-episodes-sequential-v1"
VLM_PARALLEL_EPISODE_SELECTION_STRATEGY_VERSION = "all-committed-episodes-bounded-parallel-v2"
VLM_EPISODE_SELECTION_STRATEGY_VERSION = "probe-first-then-bounded-parallel-v3"
VLM_EPISODE_MAX_CONCURRENCY = 10
_ARTIFACT_REVISION = 1


def _requires_window_context_pack(policy: DoubaoVlmRequestPolicy) -> bool:
    """Only V7-V14 have a ContextPack input contract; older runs stay replayable."""

    return policy.prompt_version in {
        VLM_CONTEXTUAL_VIDEO_PROMPT_VERSION,
        VLM_CONTEXTUAL_CORE_VIDEO_PROMPT_VERSION,
        VLM_CONTEXTUAL_TIMELINE_CORE_VIDEO_PROMPT_VERSION,
        VLM_CONTEXTUAL_CLOSED_VOCABULARY_CORE_VIDEO_PROMPT_VERSION,
        VLM_CONTEXTUAL_COMPACT_CANONICAL_CORE_VIDEO_PROMPT_VERSION,
        VLM_CONTEXTUAL_RECIPROCAL_CAUSAL_CORE_VIDEO_PROMPT_VERSION,
        VLM_CONTEXTUAL_VALIDATED_RECIPROCAL_CAUSAL_CORE_VIDEO_PROMPT_VERSION,
        VLM_CONTEXTUAL_STABLE_CORE_VIDEO_PROMPT_VERSION,
        VLM_CONTEXTUAL_REQUIRED_EMPTY_ARRAY_CORE_VIDEO_PROMPT_VERSION,
        VLM_CONTEXTUAL_STRICT_WIRE_CORE_VIDEO_PROMPT_VERSION,
        VLM_CONTEXTUAL_FACT_ANCHORED_EVENT_CORE_VIDEO_PROMPT_VERSION,
        VLM_CONTEXTUAL_ENUM_DISAMBIGUATED_FACT_ANCHORED_EVENT_CORE_VIDEO_PROMPT_VERSION,
    }


def _episode_selection_strategy(policy: DoubaoVlmRequestPolicy) -> tuple[str, int]:
    """Derive scheduling from the persisted policy rather than process config."""
    if policy.stage_strategy_version == DOUBAO_VLM_LEGACY_STAGE_STRATEGY_VERSION:
        return VLM_LEGACY_EPISODE_SELECTION_STRATEGY_VERSION, 1
    if policy.stage_strategy_version == DOUBAO_VLM_PARALLEL_STAGE_STRATEGY_VERSION:
        return VLM_PARALLEL_EPISODE_SELECTION_STRATEGY_VERSION, VLM_EPISODE_MAX_CONCURRENCY
    if policy.stage_strategy_version in {DOUBAO_VLM_STAGE_STRATEGY_VERSION, DOUBAO_VLM_VIDEO_STAGE_STRATEGY_VERSION}:
        return VLM_EPISODE_SELECTION_STRATEGY_VERSION, VLM_EPISODE_MAX_CONCURRENCY
    if policy.stage_strategy_version in {
        DOUBAO_VLM_VALIDATED_VIDEO_STAGE_STRATEGY_VERSION,
        DOUBAO_VLM_STABLE_VIDEO_STAGE_STRATEGY_VERSION,
        DOUBAO_VLM_REQUIRED_EMPTY_ARRAY_VIDEO_STAGE_STRATEGY_VERSION,
        DOUBAO_VLM_STRICT_WIRE_VIDEO_STAGE_STRATEGY_VERSION,
        DOUBAO_VLM_FACT_ANCHORED_EVENT_VIDEO_STAGE_STRATEGY_VERSION,
        DOUBAO_VLM_ENUM_DISAMBIGUATED_FACT_ANCHORED_EVENT_VIDEO_STAGE_STRATEGY_VERSION,
    }:
        return VLM_EPISODE_SELECTION_STRATEGY_VERSION, 3
    raise PipelineRunValidationError("VLM profile has no registered episode selection strategy")


class VlmPipelineStore(
    SourcePrepStore,
    ContextPrepareStore,
    GenerationStore,
    VlmBatchFinalizerStore,
    Protocol,
):
    """The shared Kernel Store capabilities required by this adapter."""


def vlm_kernel_idempotency_key(
    *,
    run_id: str,
    episode_index: int,
    source_bundle: PersistedPreparedSources,
    policy: DoubaoVlmRequestPolicy,
    execution_profile_hash: str,
    context_pack: WindowContextPack | None = None,
) -> str:
    validate_run_id(run_id)
    if type(episode_index) is not int or episode_index < 0:  # noqa: E721
        raise PipelineRunValidationError("VLM episode index must be non-negative")
    if type(source_bundle) is not PersistedPreparedSources:  # noqa: E721
        raise PipelineRunValidationError(
            "VLM idempotency requires exact persisted source provenance"
        )
    payload = {
        "episode_index": episode_index,
        "execution_profile_sha256": execution_profile_hash,
        "policy_sha256": policy.canonical_hash,
        "run_id": run_id,
        "selection_strategy_version": _episode_selection_strategy(policy)[0],
        "source_manifest_sha256": source_bundle.artifact_reference.content_hash,
        "source_provenance_sha256": source_bundle.canonical_hash,
    }
    if context_pack is not None:
        payload["context_pack_sha256"] = context_pack.canonical_hash
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "vlm:" + hashlib.sha256(encoded).hexdigest()


def vlm_batch_kernel_idempotency_key(
    *,
    run_id: str,
    source_bundle: PersistedPreparedSources,
    policy: DoubaoVlmRequestPolicy,
    execution_profile_hash: str,
    context_packs: tuple[WindowContextPack, ...] | None = None,
) -> str:
    """Return the sole durable identity of one complete VLM semantic batch."""

    validate_run_id(run_id)
    if type(source_bundle) is not PersistedPreparedSources:  # noqa: E721
        raise PipelineRunValidationError(
            "VLM batch identity requires exact persisted source provenance"
        )
    payload: dict[str, object] = {
            "execution_profile_sha256": execution_profile_hash,
            "run_id": run_id,
            "selection_strategy_version": _episode_selection_strategy(policy)[0],
            "source_manifest_sha256": source_bundle.artifact_reference.content_hash,
            "source_provenance_sha256": source_bundle.canonical_hash,
        }
    if context_packs is not None:
        payload["context_pack_hashes"] = [item.canonical_hash for item in context_packs]
    encoded = json.dumps(
        payload,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return VLM_BATCH_IDEMPOTENCY_PREFIX + hashlib.sha256(encoded).hexdigest()


class VlmPipelineStage:
    """Probe one committed episode, then project bounded parallel batches.

    Per-episode Receipts are never presented as aggregate completion evidence.
    The final control-plane result is owned by ``FinalizeVlmBatchCommand``.
    """

    def __init__(
        self,
        store: VlmPipelineStore,
        provider: VlmProviderPort,
        *,
        command: GenerateVlmEvidenceCommand | None = None,
        finalizer: FinalizeVlmBatchCommand | None = None,
        installed_profile: LocalRunResource | None = None,
        context_owner_maps: OwnerEpisodeMapSet | None = None,
        context_selection_policy: ContextSelectionPolicy | None = None,
        stop_after_probe: bool = False,
    ) -> None:
        if not callable(getattr(provider, "dispatch", None)) or not callable(
            getattr(provider, "reconcile", None)
        ):
            raise PipelineRunValidationError("VLM provider is required")
        self._store = store
        self._command = command or GenerateVlmEvidenceCommand(store, provider)
        self._finalizer = finalizer or FinalizeVlmBatchCommand(store)
        # None is an internal unit-test/adapter seam, never standard HTTP composition.
        if installed_profile is not None and type(installed_profile) is not LocalRunResource:  # noqa: E721
            raise PipelineRunValidationError("VLM requires an exact installed local-run resource")
        if type(stop_after_probe) is not bool:  # noqa: E721
            raise PipelineRunValidationError("VLM probe inspection control must be a bool")
        self._installed_profile = installed_profile
        if context_owner_maps is not None and type(context_owner_maps) is not OwnerEpisodeMapSet:  # noqa: E721
            raise PipelineRunValidationError("VLM context owner maps must be exact")
        if context_selection_policy is not None and type(context_selection_policy) is not ContextSelectionPolicy:  # noqa: E721
            raise PipelineRunValidationError("VLM context policy must be exact")
        if (context_owner_maps is None) != (context_selection_policy is None):
            raise PipelineRunValidationError("VLM context maps and policy must be supplied together")
        self._context_owner_maps = context_owner_maps
        self._context_selection_policy = context_selection_policy
        # This is an operational inspection hold only. It is deliberately not
        # part of the persisted execution profile: it cannot change the VLM
        # request, idempotency key, or semantic result being inspected.
        self._stop_after_probe = stop_after_probe

    @staticmethod
    def _job(context: PipelineStageContext) -> Job:
        if type(context) is not PipelineStageContext:  # noqa: E721
            raise PipelineRunValidationError("VLM adapter requires an exact stage context")
        if context.command.stage != "vlm":
            raise PipelineRunValidationError("VLM adapter received another stage")
        if context.execution_profile.is_legacy_unresolved:
            raise PipelineRunValidationError(
                "legacy-unresolved execution profile cannot execute VLM"
            )
        validate_run_id(context.run_id)
        return Job(context.run_id, context.request.profile)

    def _requests(
        self,
        context: PipelineStageContext,
    ) -> tuple[
        PersistedPreparedSources,
        DoubaoVlmRequestPolicy,
        tuple[GenerateVlmEvidenceRequest, ...],
        tuple[WindowContextPack, ...] | None,
    ] | None:
        job = self._job(context)
        policy = context.execution_profile.to_doubao_policy()
        retry_policy = context.execution_profile.to_generation_retry_policy()
        if self._installed_profile is not None:
            validate_installed_vlm_policy(self._installed_profile.narrative, policy, retry_policy)
            validate_installed_media_policy(
                self._installed_profile, context.execution_profile.to_media_preflight_policy(),
            )
            if context.execution_profile.to_materialization_limits().timed_speech_max_request_bytes != (
                self._installed_profile.local_run.native_timed_speech.max_request_bytes
            ):
                raise PipelineRunValidationError("persisted timed speech request limit differs from installed service")
        source_outcome = self._store.read_outcome(
            job,
            source_prep_kernel_idempotency_key(context.run_id),
        )
        if source_outcome is None or source_outcome.state in ("pending", "running"):
            return None
        if source_outcome.state in ("denied", "failed"):
            return None
        if source_outcome.state != "succeeded":
            raise PipelineRunValidationError("Kernel returned an unsupported source outcome")
        source_bundle = read_persisted_prepared_sources_bundle(
            self._store,
            job=job,
            outcome=source_outcome,
            artifact_scope=ArtifactScope("pipeline", "job", context.run_id),
            artifact_revision=_ARTIFACT_REVISION,
        )
        if type(source_bundle) is not PersistedPreparedSources:  # noqa: E721
            raise PipelineRunValidationError("VLM source reader lost exact persisted provenance")
        require_committed_source_operation(source_bundle, "semantic_analysis")
        if not source_bundle.prepared.episodes:
            raise PipelineRunValidationError(
                "VLM stage requires at least one committed source episode"
            )
        if self._installed_profile is not None:
            validate_installed_source_sampling(source_bundle)
        context_packs: tuple[WindowContextPack, ...] | None = None
        committed_context = (
            find_committed_window_context_packs(
                self._store,
                job=job,
                artifact_scope=ArtifactScope("pipeline", "job", context.run_id),
                artifact_revision=_ARTIFACT_REVISION,
                source_bundle=source_bundle,
            )
            if _requires_window_context_pack(policy)
            else None
        )
        if committed_context is not None:
            # An already committed PackSet is the sole historic input.  Ignore
            # whatever API configuration happens to be installed on this host.
            context_packs = committed_context.packs
        elif self._context_owner_maps is not None and self._context_selection_policy is not None:
            context_request = PrepareWindowContextRequest(
                job=job,
                idempotency_key=context_prepare_kernel_idempotency_key(
                    run_id=context.run_id,
                    source_bundle=source_bundle,
                    owner_maps=self._context_owner_maps,
                    policy=self._context_selection_policy,
                    execution_profile_hash=context.execution_profile_hash,
                ),
                artifact_scope=ArtifactScope("pipeline", "job", context.run_id),
                artifact_revision=_ARTIFACT_REVISION,
                source_bundle=source_bundle,
                owner_maps=self._context_owner_maps,
                selection_policy=self._context_selection_policy,
            )
            context_outcome = self._store.read_outcome(job, context_request.idempotency_key)
            if context_outcome is None or context_outcome.state in ("pending", "running"):
                return None
            if context_outcome.state != "succeeded":
                raise PipelineRunValidationError("context prepare did not produce a committed PackSet")
            context_packs = read_committed_window_context_packs(
                self._store, context_request, context_outcome
            )
        elif _requires_window_context_pack(policy):
            raise PipelineRunValidationError(
                "no committed WindowContextPack is available; context_prepare must complete first"
            )
        selection_strategy, _max_concurrency = _episode_selection_strategy(policy)
        if (
            self._stop_after_probe
            and selection_strategy != VLM_EPISODE_SELECTION_STRATEGY_VERSION
        ):
            raise PipelineRunValidationError(
                "probe inspection requires the registered single-episode probe strategy"
            )
        return source_bundle, policy, tuple(
            replace(
                build_doubao_vlm_request(
                    source_bundle=source_bundle,
                    episode_index=episode_index,
                    job=job,
                    artifact_revision=_ARTIFACT_REVISION,
                    idempotency_key=vlm_kernel_idempotency_key(
                        run_id=context.run_id,
                        episode_index=episode_index,
                        source_bundle=source_bundle,
                        policy=policy,
                        execution_profile_hash=context.execution_profile_hash,
                        context_pack=None if context_packs is None else context_packs[episode_index],
                    ),
                    policy=policy,
                    retry_policy=retry_policy,
                    context_pack=None if context_packs is None else context_packs[episode_index],
                ),
                episode_index=episode_index,
                source_manifest_sha256=source_bundle.artifact_reference.content_hash,
            )
            for episode_index in range(len(source_bundle.prepared.episodes))
        ), context_packs

    async def execute(self, context: PipelineStageContext) -> PipelineStageResult:
        prepared = await asyncio.to_thread(self._requests, context)
        if prepared is None:
            return PipelineStageResult(context.command.command_id, "indeterminate")
        result = await self._execute_batch(context, *prepared)
        if result is None:
            # Episode zero completed through the normal Kernel command and
            # provider path. Do not finalize or dispatch subsequent work until
            # the operator restarts without this inspection control.
            return PipelineStageResult(context.command.command_id, "indeterminate")
        return self._project(context, result.outcome)

    async def reconcile(self, context: PipelineStageContext) -> PipelineStageResult | None:
        prepared = await asyncio.to_thread(self._requests, context)
        if prepared is None:
            return None
        if self._probe_inspection_is_holding(context, prepared[2][0]):
            # The worker polls reconciliation. Once the real probe has a
            # terminal success, holding must be a pure store read rather than
            # repeatedly touching the provider command.
            return None
        result = await self._execute_batch(context, *prepared)
        if result is None:
            return None
        projected = self._project(context, result.outcome)
        if projected.outcome == "indeterminate":
            return None
        return projected

    async def _execute_batch(
        self,
        context: PipelineStageContext,
        source_bundle: PersistedPreparedSources,
        policy: DoubaoVlmRequestPolicy,
        requests: tuple[GenerateVlmEvidenceRequest, ...],
        context_packs: tuple[WindowContextPack, ...] | None,
    ) -> FinalizeVlmBatchResult | GenerateVlmEvidenceResult | None:
        children: list[VlmBatchChildOutcome] = []
        selection_strategy, max_concurrency = _episode_selection_strategy(policy)
        chunks = (
            (requests[:1],)
            + tuple(
                requests[start:start + max_concurrency]
                for start in range(1, len(requests), max_concurrency)
            )
            if selection_strategy == VLM_EPISODE_SELECTION_STRATEGY_VERSION
            else tuple(
                requests[start:start + max_concurrency]
                for start in range(0, len(requests), max_concurrency)
            )
        )
        for chunk_index, chunk in enumerate(chunks):
            results = await asyncio.gather(
                *(asyncio.to_thread(self._command.execute, request) for request in chunk),
            )
            unresolved: GenerateVlmEvidenceResult | None = None
            terminal: GenerateVlmEvidenceResult | None = None
            for request, result in zip(chunk, results, strict=True):
                episode_index = request.episode_index
                outcome = result.outcome
                if outcome.state in ("pending", "running"):
                    unresolved = unresolved or result
                    continue
                if outcome.state not in ("succeeded", "denied", "failed"):
                    raise PipelineRunValidationError("Kernel returned an unsupported VLM child outcome")
                if outcome.receipt_id is None:
                    raise PipelineRunValidationError("terminal Kernel VLM child lost its Receipt")
                if outcome.state in ("denied", "failed"):
                    terminal = terminal or result
                    continue
                children.append(
                    VlmBatchChildOutcome(
                        episode_index=episode_index,
                        idempotency_key=request.idempotency_key,
                        window_manifest_sha256=request.manifest.canonical_hash,
                        source_manifest_sha256=source_bundle.artifact_reference.content_hash,
                        source_provenance_sha256=source_bundle.canonical_hash,
                        request_hash=request.request_hash,
                        state=outcome.state,
                        receipt_id=outcome.receipt_id,
                        artifact_set_id=outcome.artifact_set_id,
                    )
                )
            # A chunk is intentionally the most work that can already have
            # escaped to Ark when one child fails.  Never dispatch a later
            # chunk after a terminal or indeterminate child outcome.
            if terminal is not None:
                return terminal
            if unresolved is not None:
                return unresolved
            if self._stop_after_probe and chunk_index == 0:
                # The registered v3 policy always makes this first chunk the
                # single episode-zero probe. Never reinterpret another policy
                # as safe to pause after an arbitrary multi-episode chunk.
                if (
                    selection_strategy != VLM_EPISODE_SELECTION_STRATEGY_VERSION
                    or len(chunk) != 1
                    or chunk[0].episode_index != 0
                ):
                    raise PipelineRunValidationError(
                        "probe inspection requires the registered single-episode probe strategy"
                    )
                return None
        finalizer_request = FinalizeVlmBatchRequest(
            job=Job(context.run_id, context.request.profile),
            idempotency_key=vlm_batch_kernel_idempotency_key(
                run_id=context.run_id,
                source_bundle=source_bundle,
                policy=policy,
                execution_profile_hash=context.execution_profile_hash,
                context_packs=context_packs,
            ),
            artifact_scope=canonical_recipe_scope(Job(context.run_id, context.request.profile)),
            artifact_revision=_ARTIFACT_REVISION,
            declared_episode_count=len(requests),
            source_manifest_sha256=source_bundle.artifact_reference.content_hash,
            source_provenance_sha256=source_bundle.canonical_hash,
            children=tuple(children),
            strategy_version=(
                VLM_BATCH_FINALIZER_STRATEGY_VERSION_V4
                if policy.parser_strategy_version == VLM_PARSER_V4
                else VLM_BATCH_FINALIZER_STRATEGY_VERSION
            ),
        )
        return await asyncio.to_thread(self._finalizer.execute, finalizer_request)

    def _probe_inspection_is_holding(
        self,
        context: PipelineStageContext,
        request: GenerateVlmEvidenceRequest,
    ) -> bool:
        if not self._stop_after_probe:
            return False
        outcome = self._store.read_outcome(self._job(context), request.idempotency_key)
        return outcome is not None and outcome.state == "succeeded"

    @staticmethod
    def _project(
        context: PipelineStageContext,
        outcome: CommandOutcome,
    ) -> PipelineStageResult:
        if type(outcome) is not CommandOutcome:  # noqa: E721
            raise PipelineRunValidationError("Kernel returned an invalid VLM outcome")
        if outcome.state in ("pending", "running"):
            return PipelineStageResult(context.command.command_id, "indeterminate")
        if outcome.state not in ("succeeded", "denied", "failed"):
            raise PipelineRunValidationError("Kernel returned an unsupported VLM outcome")
        if outcome.receipt_id is None:
            raise PipelineRunValidationError("terminal Kernel VLM outcome lost its Receipt")
        return PipelineStageResult(
            context.command.command_id,
            outcome.state,
            outcome.receipt_id,
        )


__all__ = (
    "VLM_EPISODE_SELECTION_STRATEGY_VERSION",
    "VLM_PARALLEL_EPISODE_SELECTION_STRATEGY_VERSION",
    "VLM_EPISODE_MAX_CONCURRENCY",
    "VLM_LEGACY_EPISODE_SELECTION_STRATEGY_VERSION",
    "VlmPipelineStage",
    "VlmPipelineStore",
    "vlm_batch_kernel_idempotency_key",
    "vlm_kernel_idempotency_key",
)
