"""Bounded durable worker for leased pipeline outbox entries."""

from __future__ import annotations

import asyncio
from uuid import uuid4

from .errors import PipelineRunValidationError, ResumeNotAllowedError, StaleRunVersionError
from .models import OutboxLease, PipelineRunSnapshot
from .ports import PipelineRunService, PipelineSchedulerPort, PipelineWorkerStore
from .stages import PipelineStageReconciler, PipelineStageRunner


class DurablePipelineWorker:
    """Execute a bounded set of outbox leases without in-memory success authority."""

    def __init__(
        self,
        *,
        worker_id: str,
        service: PipelineRunService,
        scheduler: PipelineSchedulerPort,
        store: PipelineWorkerStore,
        runner: PipelineStageRunner,
        reconciler: PipelineStageReconciler,
        concurrency: int = 2,
        max_batch_size: int = 16,
        outbox_heartbeat_seconds: float = 20.0,
    ) -> None:
        if type(worker_id) is not str or not worker_id.strip():  # noqa: E721
            raise PipelineRunValidationError("worker_id must be non-empty")
        if type(concurrency) is not int or concurrency < 1:  # noqa: E721
            raise PipelineRunValidationError("worker concurrency must be positive")
        if type(max_batch_size) is not int or max_batch_size < 1:  # noqa: E721
            raise PipelineRunValidationError("worker max_batch_size must be positive")
        if outbox_heartbeat_seconds <= 0:
            raise PipelineRunValidationError("outbox_heartbeat_seconds must be positive")
        self._worker_id = worker_id
        self._service = service
        self._scheduler = scheduler
        self._store = store
        self._runner = runner
        self._reconciler = reconciler
        self._concurrency = concurrency
        self._max_batch_size = max_batch_size
        self._outbox_heartbeat_seconds = outbox_heartbeat_seconds

    async def startup_reconstruct(self) -> tuple[str, ...]:
        return await self._service.reconstruct()

    async def run_once(self) -> int:
        claim_lock = asyncio.Lock()
        seen_run_ids: set[str] = set()
        claimed_count = 0

        async def execute_slot() -> None:
            nonlocal claimed_count
            while True:
                async with claim_lock:
                    if claimed_count >= self._max_batch_size:
                        return
                    lease = await self._scheduler.claim_next(
                        lease_id=f"{self._worker_id}:{uuid4().hex}"
                    )
                    if lease is None:
                        return
                    if lease.run_id in seen_run_ids:
                        await self._scheduler.requeue(lease)
                        return
                    seen_run_ids.add(lease.run_id)
                    claimed_count += 1
                # A lease is claimed only by a free execution slot. Its heartbeat
                # starts before this slot can claim any additional durable work.
                await self._process_lease(lease)

        slot_count = min(self._concurrency, self._max_batch_size)
        await asyncio.gather(*(execute_slot() for _index in range(slot_count)))
        return claimed_count

    async def _process_lease(self, lease: OutboxLease) -> None:
        current = [lease]
        stop = asyncio.Event()
        heartbeat_error: list[BaseException] = []

        async def heartbeat() -> None:
            while True:
                try:
                    await asyncio.wait_for(
                        stop.wait(),
                        timeout=self._outbox_heartbeat_seconds,
                    )
                    return
                except TimeoutError:
                    try:
                        current[0] = await self._scheduler.renew(current[0])
                    except BaseException as error:
                        heartbeat_error.append(error)
                        return

        heartbeat_task = asyncio.create_task(heartbeat())
        try:
            terminal = await self._process_run(lease.run_id)
        finally:
            stop.set()
            await heartbeat_task
        if heartbeat_error:
            raise PipelineRunValidationError(
                "pipeline outbox lease heartbeat was lost"
            ) from heartbeat_error[0]
        if terminal:
            await self._scheduler.acknowledge(current[0])
        else:
            await self._scheduler.requeue(current[0])

    async def run_until_idle(self, *, max_batches: int = 100) -> int:
        if type(max_batches) is not int or max_batches < 1:  # noqa: E721
            raise PipelineRunValidationError("max_batches must be positive")
        processed = 0
        for _index in range(max_batches):
            count = await self.run_once()
            processed += count
            if count == 0:
                break
        return processed

    async def _process_run(self, run_id: str) -> bool:
        snapshot = await self._store.read_run(run_id)
        if snapshot is None or snapshot.status in ("succeeded", "denied", "failed"):
            return True
        running = next((item for item in snapshot.commands if item.status == "running"), None)
        if running is not None:
            try:
                await self._store.expire_running_lease(
                    snapshot.run_id,
                    expected_version=running.version,
                    lease_id=running.lease_id or "",
                )
            except ResumeNotAllowedError:
                return False
            except (StaleRunVersionError, PipelineRunValidationError):
                return False
            snapshot = await self._require_snapshot(snapshot.run_id)
        if any(item.status == "indeterminate" for item in snapshot.commands):
            await self._reconciler.reconcile(snapshot)
        elif any(item.status == "pending" for item in snapshot.commands):
            await self._runner.claim_and_execute(
                snapshot,
                lease_id=f"{self._worker_id}:{uuid4().hex}",
            )
        current = await self._require_snapshot(snapshot.run_id)
        return current.status in ("succeeded", "denied", "failed")

    async def _require_snapshot(self, run_id: str) -> PipelineRunSnapshot:
        snapshot = await self._store.read_run(run_id)
        if snapshot is None:
            raise PipelineRunValidationError("leased pipeline run vanished")
        return snapshot


__all__ = ("DurablePipelineWorker",)
