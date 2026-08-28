"""Durable, reconstructible pipeline run application service."""

from __future__ import annotations

import hashlib
from uuid import uuid4

from .errors import (
    IdempotencyConflictError,
    PipelineRunNotFoundError,
    PipelineRunValidationError,
    SourceDeniedError,
    StaleRunVersionError,
)
from .models import (
    PipelineExecutionProfile,
    PipelineRunRequest,
    PipelineRunSnapshot,
    RunClaim,
    VlmFullStageRecomputeRequest,
    validate_idempotency_key,
    validate_run_id,
)
from .ports import PipelineRunStore, PipelineSchedulerPort, SourceAuthorizationPort
from .recompute import FullStageVlmRecomputeBinderPort


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
        full_stage_vlm_recompute_binder: FullStageVlmRecomputeBinderPort | None = None,
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
        if (
            full_stage_vlm_recompute_binder is not None
            and not callable(getattr(full_stage_vlm_recompute_binder, "bind", None))
        ):
            raise TypeError("full_stage_vlm_recompute_binder must implement bind")
        self._full_stage_vlm_recompute_binder = full_stage_vlm_recompute_binder

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

    async def recompute_full_vlm_stage(
        self,
        request: VlmFullStageRecomputeRequest,
        idempotency_key: str,
    ) -> RunClaim:
        """Create a new complete VLM Run from exact source evidence.

        It is intentionally not a resume: the base Run and all of its Receipt
        history stay immutable.  The target command keys include a new Run ID,
        so provider attempts and resulting semantic artifacts are distinct.
        """

        if type(request) is not VlmFullStageRecomputeRequest:  # noqa: E721
            raise TypeError("recompute accepts an exact VlmFullStageRecomputeRequest")
        validate_idempotency_key(idempotency_key)
        binder = self._full_stage_vlm_recompute_binder
        if binder is None:
            raise PipelineRunValidationError("full VLM recompute is not enabled for this runtime")
        base = await self.status(request.base_run_id)
        if base.version != request.expected_version:
            raise StaleRunVersionError(request.base_run_id)
        if base.status not in ("succeeded", "denied", "failed"):
            raise PipelineRunValidationError("base run must be terminal before VLM recompute")
        if base.execution_profile != self._execution_profile:
            raise PipelineRunValidationError(
                "installed execution profile differs from the base run; exact recompute is unsafe"
            )
        if not base.execution_profile.is_semantic_only:
            raise PipelineRunValidationError("first recompute slice supports semantic-only VLM runs")
        target_run_id = _recompute_run_id(idempotency_key, request.request_hash)
        # Binding happens before the target control-plane Run exists.  Thus a
        # concurrent worker can never observe a new Run before it owns exact
        # source claims.  The binder is idempotent for the deterministic target.
        await binder.bind(base=base, target_run_id=target_run_id)
        claim = await self._store.claim_run(
            run_id=target_run_id,
            idempotency_key=idempotency_key,
            request=base.request,
            request_hash=base.request.request_hash,
            execution_profile=self._execution_profile,
        )
        if claim.snapshot.run_id != target_run_id:
            raise IdempotencyConflictError("idempotency key already binds another recompute request")
        if claim.snapshot.execution_profile != self._execution_profile:
            raise PipelineRunValidationError("target run persisted another execution profile")
        await self._scheduler.enqueue(target_run_id)
        return claim

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


def _recompute_run_id(idempotency_key: str, request_hash: str) -> str:
    """Make binding and control-plane replay converge on one target Job."""

    encoded = ("full-vlm-recompute-v1\0" + idempotency_key + "\0" + request_hash).encode("utf-8")
    return "pipeline_run_" + hashlib.sha256(encoded).hexdigest()[:32]
