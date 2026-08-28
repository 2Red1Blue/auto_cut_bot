"""Explicit, auditable reuse of one committed whole-series SourcePrep result.

This command never opens the origin source directory and never copies proxy
bytes.  Its only authority is an exact successful origin Receipt plus a target
policy equal to the policy frozen in that source manifest.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from autocut_kernel.media.types import canonical_sha256
from autocut_kernel.source_manifest import SourceManifestDecodeError, SourceOperationPolicy
from autocut_kernel.store import (
    SOURCE_REUSE_COMMAND_NAME,
    ArtifactMember,
    ArtifactScope,
    CommandClaim,
    CommandOutcome,
    CommandRejection,
    CommandSuccess,
    Job,
    PersistedWholeSeriesSourceManifest,
    SourceReuseBinding,
)
from autocut_kernel.store.models import (
    artifact_set_hash,
    canonical_payload_hash,
    canonical_recipe_scope,
)

from .command import (
    PersistedPreparedSources,
    PreparedSeriesSources,
    SourcePrepStore,
    read_persisted_prepared_sources_bundle,
)


class SourceReuseStore(SourcePrepStore, Protocol):
    def commit_source_reuse_success(
        self, success: CommandSuccess, *, binding: SourceReuseBinding
    ) -> CommandOutcome: ...


@dataclass(frozen=True, slots=True)
class BindWholeSeriesSourcesRequest:
    job: Job
    idempotency_key: str
    artifact_scope: ArtifactScope
    artifact_revision: int
    origin_job: Job
    origin_outcome: CommandOutcome
    target_policy: SourceOperationPolicy

    def __post_init__(self) -> None:
        if type(self.job) is not Job or type(self.origin_job) is not Job:  # noqa: E721
            raise ValueError("source reuse requires exact target and origin Jobs")
        if self.job == self.origin_job:
            raise ValueError("source reuse requires distinct origin and target Jobs")
        if not self.idempotency_key.strip():
            raise ValueError("source reuse idempotency key must be non-empty")
        if self.artifact_scope != canonical_recipe_scope(self.job):
            raise ValueError("source reuse artifact scope must be the canonical target Job scope")
        if type(self.artifact_revision) is not int or self.artifact_revision < 1:  # noqa: E721
            raise ValueError("source reuse artifact revision must be positive")
        if type(self.origin_outcome) is not CommandOutcome:  # noqa: E721
            raise ValueError("source reuse origin outcome is invalid")
        if type(self.target_policy) is not SourceOperationPolicy:  # noqa: E721
            raise ValueError("source reuse target policy is invalid")

    @property
    def request_hash(self) -> str:
        return canonical_sha256(
            {
                "artifact_revision": self.artifact_revision,
                "artifact_scope": {
                    "key": self.artifact_scope.key,
                    "kind": self.artifact_scope.kind,
                    "namespace": self.artifact_scope.namespace,
                },
                "command": SOURCE_REUSE_COMMAND_NAME,
                "job": {"job_key": self.job.job_key, "profile": self.job.profile},
                "origin_job": {
                    "job_key": self.origin_job.job_key,
                    "profile": self.origin_job.profile,
                },
                "origin_outcome": {
                    "artifact_set_id": (
                        str(self.origin_outcome.artifact_set_id)
                        if self.origin_outcome.artifact_set_id is not None
                        else None
                    ),
                    "command_slot_id": str(self.origin_outcome.command_slot_id),
                    "receipt_id": (
                        str(self.origin_outcome.receipt_id)
                        if self.origin_outcome.receipt_id is not None
                        else None
                    ),
                    "state": self.origin_outcome.state,
                },
                "target_policy_sha256": self.target_policy.policy_sha256,
            }
        )


@dataclass(frozen=True, slots=True)
class BindWholeSeriesSourcesResult:
    outcome: CommandOutcome
    sources: PersistedPreparedSources | None = None


class BindWholeSeriesSourcesCommand:
    """Project immutable source evidence to a target Job without source I/O."""

    def __init__(self, store: SourceReuseStore) -> None:
        self._store = store

    def execute(self, request: BindWholeSeriesSourcesRequest) -> BindWholeSeriesSourcesResult:
        existing = self._store.read_outcome(request.job, request.idempotency_key)
        if existing is not None and existing.state not in ("pending", "running"):
            return self._replay(request, existing)
        claimed = self._store.claim_command(
            CommandClaim(
                request.job,
                request.idempotency_key,
                SOURCE_REUSE_COMMAND_NAME,
                request.request_hash,
                execution_kind="deterministic",
            )
        )
        if claimed.state not in ("pending", "running"):
            return self._replay(request, claimed)
        try:
            origin = read_persisted_prepared_sources_bundle(
                self._store,
                job=request.origin_job,
                outcome=request.origin_outcome,
                artifact_scope=canonical_recipe_scope(request.origin_job),
                artifact_revision=request.artifact_revision,
            )
            if origin.prepared.census.policy != request.target_policy:
                return BindWholeSeriesSourcesResult(
                    self._store.commit_command_rejection(
                        CommandRejection(
                            claimed.command_slot_id,
                            "SOURCE_REUSE_POLICY_MISMATCH",
                            _denial_detail("SOURCE_REUSE_POLICY_MISMATCH"),
                            "denied",
                        )
                    )
                )
            binding = SourceReuseBinding(
                _persisted_origin(origin), request.job, request.target_policy
            )
            artifacts = (
                _source_artifact(request, origin.prepared),
                binding.artifact(request.artifact_revision),
            )
            outcome = self._store.commit_source_reuse_success(
                CommandSuccess(claimed.command_slot_id, artifact_set_hash(artifacts), artifacts),
                binding=binding,
            )
            if outcome.state != "succeeded":
                return BindWholeSeriesSourcesResult(outcome)
            return self._replay(request, outcome)
        except (SourceManifestDecodeError, ValueError):
            return BindWholeSeriesSourcesResult(
                self._store.commit_command_rejection(
                    CommandRejection(
                        claimed.command_slot_id,
                        "SOURCE_REUSE_ORIGIN_INVALID",
                        _denial_detail("SOURCE_REUSE_ORIGIN_INVALID"),
                        "denied",
                    )
                )
            )

    def _replay(
        self,
        request: BindWholeSeriesSourcesRequest,
        outcome: CommandOutcome,
    ) -> BindWholeSeriesSourcesResult:
        if outcome.state != "succeeded":
            return BindWholeSeriesSourcesResult(outcome)
        sources = read_persisted_prepared_sources_bundle(
            self._store,
            job=request.job,
            outcome=outcome,
            artifact_scope=request.artifact_scope,
            artifact_revision=request.artifact_revision,
        )
        if sources.prepared.census.policy != request.target_policy:
            raise SourceManifestDecodeError("reused source policy does not match the target policy")
        return BindWholeSeriesSourcesResult(outcome, sources)


def _persisted_origin(value: PersistedPreparedSources) -> PersistedWholeSeriesSourceManifest:
    payload_json = json.dumps(
        value.prepared.to_mapping(), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    return PersistedWholeSeriesSourceManifest(
        value.artifact_reference,
        payload_json,
        tuple(episode.proxy_blob for episode in value.prepared.episodes),
        value.kernel_job_id,
        value.receipt_id,
        value.artifact_set_id,
        value.command_slot_id,
        value.source_job,
    )


def _source_artifact(
    request: BindWholeSeriesSourcesRequest,
    prepared: PreparedSeriesSources,
) -> ArtifactMember:
    payload_json = json.dumps(
        prepared.to_mapping(), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    return ArtifactMember(
        "whole_series_source_manifest",
        "whole_series_source_manifest",
        request.artifact_revision,
        request.artifact_scope,
        canonical_payload_hash(payload_json),
        payload_json,
    )


def _denial_detail(code: str) -> str:
    return json.dumps(
        {"classification": "denied", "diagnostic_code": code, "stage": SOURCE_REUSE_COMMAND_NAME},
        separators=(",", ":"),
        sort_keys=True,
    )


__all__ = [
    "BindWholeSeriesSourcesCommand",
    "BindWholeSeriesSourcesRequest",
    "BindWholeSeriesSourcesResult",
    "SourceReuseStore",
]
