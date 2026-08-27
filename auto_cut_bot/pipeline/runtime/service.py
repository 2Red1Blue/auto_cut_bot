"""Durable, reconstructible pipeline run application service."""

from __future__ import annotations

from uuid import uuid4

from .errors import PipelineRunNotFoundError, PipelineRunValidationError, SourceDeniedError
from .models import (
    PipelineExecutionProfile,
    PipelineRunRequest,
    PipelineRunSnapshot,
    RunClaim,
    validate_idempotency_key,
    validate_run_id,
)
from .ports import PipelineRunStore, PipelineSchedulerPort, SourceAuthorizationPort


class DurablePipelineRunService:
    """Persist intent first, then enqueue an opaque run identity.

    Neither the scheduler call nor an in-memory task is treated as completion
    authority. A restarted composition can replay reconstructible rows through
    :meth:`reconstruct`.
    """

    def __init__(
        self,
        store: PipelineRunStore,
        scheduler: PipelineSchedulerPort,
        source_authority: SourceAuthorizationPort,
        *,
        execution_profile: PipelineExecutionProfile | None = None,
    ) -> None:
        self._store = store
        self._scheduler = scheduler
        self._source_authority = source_authority
        self._execution_profile = (
            PipelineExecutionProfile.legacy_unresolved()
            if execution_profile is None
            else execution_profile
        )
        if type(self._execution_profile) is not PipelineExecutionProfile:  # noqa: E721
            raise TypeError("execution_profile must be a PipelineExecutionProfile")

    async def submit(self, request: PipelineRunRequest, idempotency_key: str) -> RunClaim:
        if type(request) is not PipelineRunRequest:  # noqa: E721
            raise TypeError("submit accepts only PipelineRunRequest")
        validate_idempotency_key(idempotency_key)
        if not self._execution_profile.has_executable_plan:
            raise PipelineRunValidationError(
                "new pipeline runs require a frozen media-preflight or semantic-only execution profile"
            )
        if not self._source_authority.allows(request):
            raise SourceDeniedError("source is outside the configured authority")
        claim = await self._store.claim_run(
            run_id=f"pipeline_run_{uuid4().hex}",
            idempotency_key=idempotency_key,
            request=request,
            request_hash=request.request_hash,
            execution_profile=self._execution_profile,
        )
        if not claim.snapshot.execution_profile.has_executable_plan:
            raise PipelineRunValidationError(
                "persisted pipeline run has no frozen media-preflight or semantic-only execution profile"
            )
        if claim.snapshot.request != request or claim.snapshot.request_hash != request.request_hash:
            raise PipelineRunValidationError(
                "claimed run does not bind the submitted canonical request"
            )
        # The durable scheduler is idempotent. Re-enqueue every replay so a
        # prior claim-success/enqueue-failure window is repairable by retrying
        # the same HTTP idempotency key.
        await self._scheduler.enqueue(claim.snapshot.run_id)
        return claim

    async def status(self, run_id: str) -> PipelineRunSnapshot:
        validate_run_id(run_id)
        snapshot = await self._store.read_run(run_id)
        if snapshot is None:
            raise PipelineRunNotFoundError(run_id)
        return snapshot

    async def resume(self, run_id: str, *, expected_version: int) -> PipelineRunSnapshot:
        validate_run_id(run_id)
        if type(expected_version) is not int or expected_version < 0:  # noqa: E721
            raise ValueError("expected_version must be a non-negative integer")
        persisted = await self._store.read_run(run_id)
        if persisted is None:
            raise PipelineRunNotFoundError(run_id)
        if not persisted.execution_profile.has_executable_plan:
            raise PipelineRunValidationError(
                "persisted pipeline run has no frozen media-preflight or semantic-only execution profile"
            )
        snapshot = await self._store.claim_resume(run_id, expected_version=expected_version)
        if (
            snapshot.run_id != run_id
            or snapshot.version != expected_version + 1
            or snapshot.status not in ("accepted", "running")
            or not any(
                command.status in ("pending", "indeterminate")
                for command in snapshot.commands
            )
        ):
            raise PipelineRunValidationError("resume claim returned an invalid CAS projection")
        await self._scheduler.enqueue(snapshot.run_id)
        return snapshot

    async def reconstruct(self) -> tuple[str, ...]:
        snapshots = await self._store.list_reconstructible_runs()
        run_ids: list[str] = []
        for snapshot in snapshots:
            if snapshot.status not in ("accepted", "running") or not any(
                command.status in ("pending", "running", "indeterminate")
                for command in snapshot.commands
            ):
                continue
            if not snapshot.execution_profile.has_executable_plan:
                raise PipelineRunValidationError(
                    "reconstructible run has no frozen media-preflight or semantic-only execution profile"
                )
            await self._scheduler.enqueue(snapshot.run_id)
            run_ids.append(snapshot.run_id)
        return tuple(run_ids)
