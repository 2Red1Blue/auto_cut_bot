"""Shared read-only reconstruction of exact semantic predecessor requests.

Semantic stages use this owner; rebuilding a request never executes a predecessor
or grants Admission. Kernel committed readers remain the authority for content.
"""

from __future__ import annotations

import hashlib
import re
from typing import Protocol

from autocut_kernel.contracts.compiler.canonical import canonical_json_hash
from autocut_kernel.pipeline.build_narrative_graph_command import NarrativeGraphStore
from autocut_kernel.pipeline.build_narrative_graph_request import BuildNarrativeGraphRequest
from autocut_kernel.pipeline.compile_story_portfolio_request import CompileStoryPortfolioRequest
from autocut_kernel.semantic_chain.stage1_command_policy import Stage1CommandPolicy
from autocut_kernel.semantic_chain.story_design_command_policy import Stage2CommandPolicy
from autocut_kernel.store import (
    ArtifactScope,
    CommandOutcome,
    CommittedArtifactMemberReference,
    CommittedSemanticInputsRequest,
    Job,
    SemanticInputUnavailableError,
)

from auto_cut_bot.pipeline.source_prep import (
    PersistedPreparedSources,
    SourcePrepStore,
    read_persisted_prepared_sources_bundle,
)

from .errors import PipelineRunValidationError
from .models import validate_run_id
from .source_prep_stage import (
    require_committed_source_operation,
    source_prep_kernel_idempotency_key,
)
from .vlm_stage import vlm_batch_kernel_idempotency_key

_VLM_SELECTION_STRATEGY_VERSION = "all-committed-episodes-sequential-v1"
_SOURCE_ARTIFACT_REVISION = 1


class Stage1NarrativePipelineStore(SourcePrepStore, NarrativeGraphStore, Protocol):
    """Public committed read seams shared by semantic Runtime adapters."""

    def read_committed_vlm_semantic_pack_set_reference(
        self, job: Job, idempotency_key: str,
    ) -> CommittedArtifactMemberReference: ...


def stage1_narrative_kernel_idempotency_key(
    *, run_id: str, source_bundle: PersistedPreparedSources, execution_profile_hash: str,
) -> str:
    validate_run_id(run_id)
    if type(source_bundle) is not PersistedPreparedSources:  # noqa: E721
        raise PipelineRunValidationError("Stage 1 identity requires exact persisted source provenance")
    return "stage1-narrative:" + hashlib.sha256(
        (
            f"{run_id}\0{execution_profile_hash}\0{source_bundle.artifact_reference.content_hash}"
            f"\0{source_bundle.canonical_hash}\0{_VLM_SELECTION_STRATEGY_VERSION}"
        ).encode("utf-8")
    ).hexdigest()


def read_stage1_pipeline_request(
    store: Stage1NarrativePipelineStore, *, job: Job, run_id: str,
    execution_profile_hash: str, policy: Stage1CommandPolicy,
) -> BuildNarrativeGraphRequest | None:
    validate_run_id(run_id)
    if type(job) is not Job or job.job_key != run_id or type(policy) is not Stage1CommandPolicy:  # noqa: E721
        raise PipelineRunValidationError("semantic predecessor request requires an exact run Job and Stage 1 policy")
    source_outcome = store.read_outcome(job, source_prep_kernel_idempotency_key(run_id))
    if source_outcome is None or source_outcome.state in ("pending", "running"):
        return None
    if source_outcome.state in ("denied", "failed"):
        raise PipelineRunValidationError("Stage 1 cannot execute after a terminal source-preparation predecessor")
    if source_outcome.state != "succeeded":
        raise PipelineRunValidationError("source preparation outcome is unsupported")
    source_bundle = read_persisted_prepared_sources_bundle(
        store, job=job, outcome=source_outcome,
        artifact_scope=ArtifactScope("pipeline", "job", run_id),
        artifact_revision=_SOURCE_ARTIFACT_REVISION,
    )
    require_committed_source_operation(source_bundle, "semantic_analysis")
    vlm_key = vlm_batch_kernel_idempotency_key(
        run_id=run_id, source_bundle=source_bundle, execution_profile_hash=execution_profile_hash,
    )
    vlm_outcome = store.read_outcome(job, vlm_key)
    if vlm_outcome is None or vlm_outcome.state in ("pending", "running"):
        return None
    if vlm_outcome.state in ("denied", "failed"):
        raise PipelineRunValidationError("Stage 1 cannot execute after a terminal VLM predecessor")
    if vlm_outcome.state != "succeeded":
        raise PipelineRunValidationError("VLM aggregate outcome is unsupported")
    try:
        aggregate = store.read_committed_vlm_semantic_pack_set_reference(job, vlm_key)
    except SemanticInputUnavailableError as error:
        raise PipelineRunValidationError("Stage 1 requires one exact committed VLM SemanticPackSet") from error
    if type(aggregate) is not CommittedArtifactMemberReference:  # noqa: E721
        raise PipelineRunValidationError("VLM reader lost exact committed aggregate identity")
    source = CommittedArtifactMemberReference(
        source_bundle.receipt_id, source_bundle.artifact_set_id, 0,
        source_bundle.artifact_reference.scope, source_bundle.artifact_reference.artifact_type,
        source_bundle.artifact_reference.logical_id, source_bundle.artifact_reference.revision,
        source_bundle.artifact_reference.content_hash,
    )
    return policy.build_request(
        CommittedSemanticInputsRequest(job, source, aggregate),
        stage1_narrative_kernel_idempotency_key(
            run_id=run_id, source_bundle=source_bundle, execution_profile_hash=execution_profile_hash,
        ),
    )


def stage2_portfolio_kernel_idempotency_key(
    *, run_id: str, execution_profile_hash: str, stage1_idempotency_key: str,
) -> str:
    validate_run_id(run_id)
    if (type(execution_profile_hash) is not str  # noqa: E721
            or re.fullmatch(r"sha256:[0-9a-f]{64}", execution_profile_hash) is None
            or type(stage1_idempotency_key) is not str  # noqa: E721
            or re.fullmatch(r"stage1-narrative:[0-9a-f]{64}", stage1_idempotency_key) is None):
        raise PipelineRunValidationError("Stage 2 identity requires exact profile and Stage 1 request keys")
    digest = canonical_json_hash({"run_id": run_id, "execution_profile_hash": execution_profile_hash,
                                  "stage1_idempotency_key": stage1_idempotency_key})
    return "stage2-portfolio:" + digest.removeprefix("sha256:")


def read_stage2_pipeline_request(
    store: Stage1NarrativePipelineStore, *, job: Job, run_id: str,
    execution_profile_hash: str, stage1_policy: Stage1CommandPolicy, stage2_policy: Stage2CommandPolicy,
) -> CompileStoryPortfolioRequest | None:
    if type(stage2_policy) is not Stage2CommandPolicy:  # noqa: E721
        raise PipelineRunValidationError("semantic predecessor requires an exact Stage 2 policy")
    predecessor = read_stage1_pipeline_request(
        store, job=job, run_id=run_id, execution_profile_hash=execution_profile_hash, policy=stage1_policy,
    )
    if predecessor is None:
        return None
    outcome = store.read_outcome(job, predecessor.idempotency_key)
    if outcome is None:
        return None
    if type(outcome) is not CommandOutcome:  # noqa: E721
        raise PipelineRunValidationError("Stage 1 predecessor outcome is unsupported")
    if outcome.state in ("pending", "running"):
        return None
    if outcome.state in ("denied", "failed"):
        raise PipelineRunValidationError("Stage 2 cannot execute after a terminal Stage 1 predecessor")
    if outcome.state != "succeeded":
        raise PipelineRunValidationError("Stage 1 predecessor outcome is unsupported")
    # The constructor requires full succeeded identity; the downstream Kernel
    # Command independently reads actual committed content before generation.
    return stage2_policy.build_request(
        predecessor, outcome, stage2_portfolio_kernel_idempotency_key(
            run_id=run_id, execution_profile_hash=execution_profile_hash,
            stage1_idempotency_key=predecessor.idempotency_key,
        ),
    )
