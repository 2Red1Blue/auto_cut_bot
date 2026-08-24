"""Narrow injected ports for the durable pipeline run service."""

from __future__ import annotations

from typing import Protocol

from .models import (
    OutboxLease,
    PipelineCommand,
    PipelineRunRequest,
    PipelineRunSnapshot,
    PipelineStageContext,
    PipelineStageResult,
    RunClaim,
)


class PipelineRunStore(Protocol):
    """Semantic persistence boundary implemented by the runtime composition."""

    async def claim_run(
        self,
        *,
        run_id: str,
        idempotency_key: str,
        request: PipelineRunRequest,
        request_hash: str,
    ) -> RunClaim: ...

    async def read_run(self, run_id: str) -> PipelineRunSnapshot | None: ...

    async def claim_resume(
        self,
        run_id: str,
        *,
        expected_version: int,
    ) -> PipelineRunSnapshot: ...

    async def list_reconstructible_runs(self) -> tuple[PipelineRunSnapshot, ...]: ...


class PipelineSchedulerPort(Protocol):
    """Durable idempotent queue boundary.

    Repeated ``enqueue(run_id)`` calls must collapse to the same durable work
    identity. Enqueue never implies stage or run success.
    """

    async def enqueue(self, run_id: str) -> None: ...

    async def claim_next(self, *, lease_id: str) -> OutboxLease | None: ...

    async def renew(self, lease: OutboxLease) -> OutboxLease: ...

    async def acknowledge(self, lease: OutboxLease) -> None: ...

    async def requeue(self, lease: OutboxLease) -> None: ...


class SourceAuthorizationPort(Protocol):
    """Authorize a root or opaque source reference before persistence."""

    def allows(self, request: PipelineRunRequest) -> bool: ...


class PipelineRunService(Protocol):
    """The only pipeline dependency visible to aiohttp handlers."""

    async def submit(self, request: PipelineRunRequest, idempotency_key: str) -> RunClaim: ...

    async def status(self, run_id: str) -> PipelineRunSnapshot: ...

    async def resume(self, run_id: str, *, expected_version: int) -> PipelineRunSnapshot: ...

    async def reconstruct(self) -> tuple[str, ...]: ...


class PipelineStagePort(Protocol):
    """One registered business stage; handlers and run DTOs never import it."""

    async def execute(self, context: PipelineStageContext) -> PipelineStageResult: ...


class PipelineStageReconcilePort(Protocol):
    """Read-only recovery of one external call with an uncertain outcome."""

    async def reconcile(
        self,
        context: PipelineStageContext,
    ) -> PipelineStageResult | None: ...


class PipelineCommandClaimStore(Protocol):
    """Atomically lease a command and CAS-project its stage result."""

    async def claim_next_pending(
        self,
        run_id: str,
        *,
        expected_version: int,
        lease_id: str,
    ) -> PipelineCommand | None: ...

    async def expire_running_lease(
        self,
        run_id: str,
        *,
        expected_version: int,
        lease_id: str,
    ) -> PipelineCommand: ...

    async def renew_running_lease(
        self,
        run_id: str,
        *,
        command_id: str,
        expected_version: int,
        lease_id: str,
    ) -> PipelineCommand: ...

    async def record_result(
        self,
        run_id: str,
        *,
        result: PipelineStageResult,
        expected_version: int,
        lease_id: str,
    ) -> None: ...

    async def read_indeterminate(
        self,
        run_id: str,
        *,
        expected_version: int,
    ) -> PipelineCommand | None: ...

    async def record_reconciled_result(
        self,
        run_id: str,
        *,
        result: PipelineStageResult,
        expected_version: int,
    ) -> None: ...


class PipelineSnapshotStore(Protocol):
    async def read_run(self, run_id: str) -> PipelineRunSnapshot | None: ...


class PipelineWorkerStore(PipelineCommandClaimStore, PipelineSnapshotStore, Protocol):
    pass
