"""Application entry for one untrusted, shadow-only bootstrap observation.

This is deliberately outside the ordinary Pipeline stage chain.  Its public
input is just a prior shadow SourcePrep run identity and one episode index;
all Blob, Receipt, manifest, transport, and limit identities are reconstructed
from committed state or injected by composition.
"""

from __future__ import annotations

from typing import Protocol

from autocut_kernel.media.shadow_bootstrap_observation import (
    ShadowBootstrapObservationRequest,
    ShadowBootstrapObservationSource,
)
from autocut_kernel.media.types import TickRange
from autocut_kernel.pipeline.collect_shadow_bootstrap_observation_command import (
    CollectShadowBootstrapObservationCommand,
    CollectShadowBootstrapObservationRequest,
    ShadowBootstrapObservationPort,
    ShadowBootstrapObservationStore,
)
from autocut_kernel.store import ArtifactScope, CommandOutcome, Job
from autocut_kernel.store.models import MaterializationLimits

from auto_cut_bot.pipeline.source_prep import (
    PersistedPreparedSources,
    SourcePrepStore,
    read_persisted_prepared_sources_bundle,
)

from .errors import PipelineRunValidationError
from .models import validate_run_id
from .source_prep_stage import source_prep_kernel_idempotency_key

_SOURCE_ARTIFACT_REVISION = 1


class ShadowBootstrapEntryError(PipelineRunValidationError):
    """Base error for the closed bootstrap HTTP entry contract."""


class ShadowBootstrapSourceNotFoundError(ShadowBootstrapEntryError):
    """The requested shadow SourcePrep run has not recorded an outcome."""


class ShadowBootstrapSourceProfileError(ShadowBootstrapEntryError):
    """Committed source provenance does not belong to the shadow profile."""


class ShadowBootstrapSourceNotReadyError(ShadowBootstrapEntryError):
    """The source outcome cannot yet authorize a bootstrap dispatch."""


class ShadowBootstrapEpisodeOutOfRangeError(ShadowBootstrapEntryError):
    """The requested episode is absent from the committed source manifest."""


class ShadowBootstrapEntryStore(SourcePrepStore, ShadowBootstrapObservationStore, Protocol):
    """The only persistence seam required by the standalone entry service."""


class ShadowBootstrapObservationEntryService:
    """Rebuild and submit one immutable shadow-bootstrap observation request."""

    def __init__(
        self,
        store: ShadowBootstrapEntryStore,
        port: ShadowBootstrapObservationPort,
        *,
        materialization_limits: MaterializationLimits,
        max_response_bytes: int,
        command: CollectShadowBootstrapObservationCommand | None = None,
    ) -> None:
        if type(materialization_limits) is not MaterializationLimits:  # noqa: E721
            raise TypeError("materialization_limits must be exact")
        if type(max_response_bytes) is not int or max_response_bytes <= 0:  # noqa: E721
            raise ValueError("max_response_bytes must be a positive integer")
        if not callable(getattr(port, "observe", None)):
            raise TypeError("port must implement observe")
        if command is not None and not callable(getattr(command, "execute", None)):
            raise TypeError("command must implement execute")
        self._store = store
        self._limits = materialization_limits
        self._max_response_bytes = max_response_bytes
        self._command = command or CollectShadowBootstrapObservationCommand(store, port)

    def collect(self, source_run_id: str, episode_index: int) -> CommandOutcome:
        """Collect one episode observation without accepting client provenance.

        A running outcome is preserved only when the Kernel reports an unknown
        dispatch.  Every unavailable predecessor fails before command dispatch.
        """

        validate_run_id(source_run_id)
        source_job = Job(source_run_id, "shadow")
        if type(episode_index) is not int or episode_index < 0:  # noqa: E721
            raise ShadowBootstrapEpisodeOutOfRangeError("episode_index must be non-negative")
        source_outcome = self._store.read_outcome(
            source_job, source_prep_kernel_idempotency_key(source_run_id)
        )
        if source_outcome is None:
            raise ShadowBootstrapSourceNotFoundError("shadow SourcePrep outcome was not found")
        if source_outcome.state in ("pending", "running"):
            raise ShadowBootstrapSourceNotReadyError("shadow SourcePrep outcome is not terminal")
        if source_outcome.state != "succeeded":
            raise ShadowBootstrapSourceNotReadyError("shadow SourcePrep outcome is not succeeded")

        source_bundle = read_persisted_prepared_sources_bundle(
            self._store,
            job=source_job,
            outcome=source_outcome,
            artifact_scope=ArtifactScope("pipeline", "job", source_run_id),
            artifact_revision=_SOURCE_ARTIFACT_REVISION,
        )
        self._require_shadow_bundle(source_bundle, source_job)
        persisted = self._store.read_whole_series_source_manifest(
            source_job, source_bundle.artifact_set_id
        )
        if (
            persisted.source_job != source_job
            or persisted.reference != source_bundle.artifact_reference
            or persisted.receipt_id != source_bundle.receipt_id
            or persisted.artifact_set_id != source_bundle.artifact_set_id
            or persisted.command_slot_id != source_bundle.command_slot_id
        ):
            raise ShadowBootstrapSourceProfileError(
                "SourcePrep manifest provenance is not shadow-bound"
            )
        try:
            episode = source_bundle.prepared.episodes[episode_index]
        except IndexError as error:
            raise ShadowBootstrapEpisodeOutOfRangeError(
                "episode_index is outside the committed SourcePrep manifest"
            ) from error

        source = episode.media_probe.source
        clock = episode.media_probe.audio_sample_boundaries.context
        source_range = TickRange(clock.origin_tick, clock.end_tick)
        observation = ShadowBootstrapObservationRequest(
            ShadowBootstrapObservationSource(
                source.source_id,
                source.content_sha256,
                clock.clock_id,
                clock.time_base,
                source_range,
            ),
            source_range,
            self._limits.max_source_bytes,
            self._limits.timed_speech_max_request_bytes,
            self._max_response_bytes,
        )
        request = CollectShadowBootstrapObservationRequest(
            job=Job(source_run_id, "shadow"),
            source_job=source_job,
            episode_index=episode_index,
            source_blob=episode.proxy_blob,
            source_manifest_reference=source_bundle.artifact_reference,
            source_manifest_receipt_id=source_bundle.receipt_id,
            source_manifest_artifact_set_id=source_bundle.artifact_set_id,
            source_manifest_command_slot_id=source_bundle.command_slot_id,
            observation=observation,
            materialization_limits=self._limits,
        )
        return self._command.execute(request)

    @staticmethod
    def _require_shadow_bundle(
        source_bundle: PersistedPreparedSources,
        source_job: Job,
    ) -> None:
        if type(source_bundle) is not PersistedPreparedSources:  # noqa: E721
            raise ShadowBootstrapSourceProfileError("SourcePrep provenance is not exact")
        if source_bundle.source_job != source_job or source_bundle.source_job.profile != "shadow":
            raise ShadowBootstrapSourceProfileError("SourcePrep provenance is not shadow")


__all__ = (
    "ShadowBootstrapEntryError",
    "ShadowBootstrapEntryStore",
    "ShadowBootstrapEpisodeOutOfRangeError",
    "ShadowBootstrapObservationEntryService",
    "ShadowBootstrapSourceNotFoundError",
    "ShadowBootstrapSourceNotReadyError",
    "ShadowBootstrapSourceProfileError",
)
