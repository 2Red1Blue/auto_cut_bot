"""Bounded durable worker for leased pipeline outbox entries."""

from __future__ import annotations

import asyncio
from uuid import uuid4

from autocut_kernel.store import RuntimeStoreError
from psycopg import InterfaceError, OperationalError

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

    async def run_once(self, *, stop_event: asyncio.Event | None = None) -> int:
        if stop_event is not None and type(stop_event) is not asyncio.Event:  # noqa: E721
            raise PipelineRunValidationError("stop_event must be an asyncio.Event")
        claim_lock = asyncio.Lock()
        seen_run_ids: set[str] = set()
        claimed_count = 0

        async def execute_slot() -> None:
            nonlocal claimed_count
            while True:
                async with claim_lock:
                    if stop_event is not None and stop_event.is_set():
                        return
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
                async with claim_lock:
                    if stop_event is not None and stop_event.is_set():
                        return

        slot_count = min(self._concurrency, self._max_batch_size)
        results = await asyncio.gather(
            *(execute_slot() for _index in range(slot_count)),
            return_exceptions=True,
        )
        errors = tuple(result for result in results if isinstance(result, BaseException))
        cancelled = next(
            (error for error in errors if isinstance(error, asyncio.CancelledError)),
            None,
        )
        if cancelled is not None:
            raise cancelled
        fatal = next(
            (error for error in errors if not _is_recoverable_worker_error(error)),
            None,
        )
        if fatal is not None:
            raise fatal
        if errors:
            raise errors[0]
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
        processing_error: Exception | None = None
        terminal = False
        try:
            terminal = await self._process_run(lease.run_id)
        except Exception as error:
            processing_error = error
        finally:
            stop.set()
            await heartbeat_task
        if heartbeat_error:
            raise PipelineRunValidationError(
                "pipeline outbox lease heartbeat was lost"
            ) from heartbeat_error[0]
        if processing_error is not None:
            if not _is_recoverable_worker_error(processing_error):
                raise processing_error
            try:
                # requeue() is the scheduler's owned-lease CAS. If ownership or
                # storage is unavailable, do not invent a replacement transition;
                # leave the durable lease to expire and be reclaimed.
                await self._scheduler.requeue(current[0])
            except Exception:
                pass
            return
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

    async def run_forever(
        self,
        stop_event: asyncio.Event,
        *,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        """Poll bounded batches until the application-owned stop event is set."""
        if type(stop_event) is not asyncio.Event:  # noqa: E721
            raise PipelineRunValidationError("stop_event must be an asyncio.Event")
        if (
            isinstance(poll_interval_seconds, bool)
            or type(poll_interval_seconds) not in (int, float)
            or poll_interval_seconds <= 0
        ):
            raise PipelineRunValidationError("poll_interval_seconds must be positive")
        backoff_seconds = float(poll_interval_seconds)
        while not stop_event.is_set():
            try:
                await self.run_once(stop_event=stop_event)
            except Exception as error:
                if not _is_recoverable_worker_error(error):
                    raise
                await _wait_for_stop(
                    stop_event,
                    backoff_seconds,
                )
                backoff_seconds = min(
                    backoff_seconds * 2,
                    max(30.0, float(poll_interval_seconds)),
                )
                continue
            backoff_seconds = float(poll_interval_seconds)
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=float(poll_interval_seconds),
                )
            except TimeoutError:
                pass

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


def _is_recoverable_worker_error(error: BaseException) -> bool:
    transient_errors = (OperationalError, InterfaceError, ConnectionError, TimeoutError)
    if isinstance(error, transient_errors):
        return True
    if not isinstance(error, (PipelineRunValidationError, RuntimeStoreError)):
        return False

    pending: list[tuple[BaseException, int]] = [(error, 0)]
    seen: set[int] = set()
    while pending:
        current, depth = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if depth > 0 and isinstance(current, transient_errors):
            return True
        if depth >= 4:
            continue
        if current.__cause__ is not None:
            pending.append((current.__cause__, depth + 1))
        if current.__context__ is not None:
            pending.append((current.__context__, depth + 1))
    return False


async def _wait_for_stop(stop_event: asyncio.Event, delay_seconds: float) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=delay_seconds)
    except TimeoutError:
        pass


__all__ = ("DurablePipelineWorker",)
