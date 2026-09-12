"""Closed operator entry for one rich observation from committed SourcePrep."""
from __future__ import annotations

from typing import Protocol, cast

from autocut_kernel.pipeline.observation_generation import (
    GenerateObservationCommand,
    ObservationGenerationRequest,
    ObservationGenerationResult,
)
from autocut_kernel.store import ArtifactScope, CommandOutcome, Job
from autocut_kernel.store.models import JobProfile
from autocut_kernel.vlm import GenerationRetryPolicy

from auto_cut_bot.pipeline.source_prep import (
    PersistedPreparedSources,
    SourcePrepStore,
    read_persisted_prepared_sources_bundle,
)
from auto_cut_bot.pipeline.vlm.observation_factory import (
    ObservationRuntimePolicy,
    build_observation_request,
)

from .errors import PipelineRunValidationError
from .source_prep_stage import source_prep_kernel_idempotency_key


class ObservationRequestFactory(Protocol):
    """Build the immutable Kernel request from the exact committed episode only."""
    def build(self, *, source_bundle: PersistedPreparedSources, episode_index: int) -> ObservationGenerationRequest: ...


class SourcePrepObservationRequestFactory:
    def __init__(self, *, profile: JobProfile, policy: ObservationRuntimePolicy,
                 retry_policy: GenerationRetryPolicy) -> None:
        self._profile, self._policy, self._retry = profile, policy, retry_policy

    def build(self, *, source_bundle: PersistedPreparedSources,
              episode_index: int) -> ObservationGenerationRequest:
        job = Job(source_bundle.source_job.job_key, cast(JobProfile, self._profile))
        return build_observation_request(source_bundle=source_bundle, episode_index=episode_index,
            job=job, idempotency_key=f"vlm-observation-v1:{source_bundle.canonical_hash}:{episode_index}",
            policy=self._policy, retry_policy=self._retry)


class ObservationEntryService:
    def __init__(self, store: SourcePrepStore, command: GenerateObservationCommand, request_factory: ObservationRequestFactory, *, source_profile: JobProfile) -> None:
        self._store, self._command, self._factory, self._source_profile = store, command, request_factory, source_profile

    def _request(self, source_run_id: str, episode_index: int) -> ObservationGenerationRequest:
        if type(episode_index) is not int or episode_index < 0:  # noqa: E721
            raise PipelineRunValidationError("episode_index must be non-negative")
        job = Job(source_run_id, cast(JobProfile, self._source_profile))
        outcome = self._store.read_outcome(job, source_prep_kernel_idempotency_key(source_run_id))
        if outcome is None or outcome.state != "succeeded":
            raise PipelineRunValidationError("a committed SourcePrep outcome is required")
        bundle = read_persisted_prepared_sources_bundle(self._store, job=job, outcome=outcome,
            artifact_scope=ArtifactScope("pipeline", "job", source_run_id), artifact_revision=1)
        if episode_index >= len(bundle.prepared.episodes):
            raise PipelineRunValidationError("episode_index is outside committed SourcePrep")
        return self._factory.build(source_bundle=bundle, episode_index=episode_index)

    def dry_run(self, source_run_id: str, episode_index: int) -> ObservationGenerationRequest:
        return self._request(source_run_id, episode_index)

    def execute(self, source_run_id: str, episode_index: int) -> ObservationGenerationResult:
        return self._command.execute(self._request(source_run_id, episode_index))

    def status(self, source_run_id: str, episode_index: int) -> CommandOutcome | None:
        """Read only; unknown attempts are deliberately not reconciled here."""
        request = self._request(source_run_id, episode_index)
        return self._store.read_outcome(request.job, request.idempotency_key)


__all__ = ["ObservationEntryService", "ObservationRequestFactory", "SourcePrepObservationRequestFactory"]
