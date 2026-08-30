"""Closed stage registration seam for runtime composition and workers."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext

from auto_cut_bot.pipeline.debug import PipelineStageDebugContext, PipelineStageDebugSink

from .errors import PipelineRunValidationError, PipelineStageIsolationError
from .models import (
    PipelineCommand,
    PipelineRunSnapshot,
    PipelineStageContext,
    PipelineStageResult,
)
from .ports import PipelineCommandClaimStore, PipelineStagePort, PipelineStageReconcilePort


class PipelineStageRegistry:
    """Immutable ordered stage ports supplied by the composition root."""

    def __init__(self, ports: tuple[tuple[str, PipelineStagePort], ...]) -> None:
        if not ports:
            raise PipelineRunValidationError("stage registry must not be empty")
        names: set[str] = set()
        normalized: list[tuple[str, PipelineStagePort]] = []
        for name, port in ports:
            if type(name) is not str or not name.strip():  # noqa: E721
                raise PipelineRunValidationError("stage name must be non-empty")
            if name in names:
                raise PipelineRunValidationError(f"duplicate stage name: {name}")
            if not callable(getattr(port, "execute", None)):
                raise PipelineRunValidationError(f"stage {name} does not implement execute")
            names.add(name)
            normalized.append((name, port))
        self._ports = tuple(normalized)

    @classmethod
    def from_ports(
        cls,
        *ports: tuple[str, PipelineStagePort],
    ) -> PipelineStageRegistry:
        return cls(ports)

    @property
    def stage_names(self) -> tuple[str, ...]:
        return tuple(name for name, _port in self._ports)

    def require(self, name: str) -> PipelineStagePort:
        for registered_name, port in self._ports:
            if registered_name == name:
                return port
        raise PipelineRunValidationError(f"unregistered pipeline stage: {name}")


class PipelineStageRunner:
    """Lease one ordered command and keep ownership alive during execution."""

    def __init__(
        self,
        registry: PipelineStageRegistry,
        command_store: PipelineCommandClaimStore,
        *,
        heartbeat_seconds: float = 30.0,
        debug_sink: PipelineStageDebugSink | None = None,
    ) -> None:
        if heartbeat_seconds <= 0:
            raise PipelineRunValidationError("heartbeat_seconds must be positive")
        self._registry = registry
        self._command_store = command_store
        self._heartbeat_seconds = heartbeat_seconds
        self._debug_sink = debug_sink

    async def claim_and_execute(
        self,
        snapshot: PipelineRunSnapshot,
        *,
        lease_id: str,
    ) -> PipelineStageResult | None:
        if type(snapshot) is not PipelineRunSnapshot:  # noqa: E721
            raise PipelineRunValidationError("runner requires a persisted run snapshot")
        if type(lease_id) is not str or not lease_id.strip():  # noqa: E721
            raise PipelineRunValidationError("lease_id must be a non-empty string")
        pending = next((item for item in snapshot.commands if item.status == "pending"), None)
        if pending is None:
            return None
        if pending.stage in ("stage1_narrative", "stage2_portfolio", "stage3_blueprint"):
            snapshot.execution_profile.build_stage1_command_policy()
        if pending.stage in ("stage2_portfolio", "stage3_blueprint"):
            snapshot.execution_profile.build_stage2_command_policy()
        if pending.stage == "stage3_blueprint":
            snapshot.execution_profile.build_stage3_command_policy()
        if pending.stage == "vlm" and snapshot.execution_profile.is_legacy_unresolved:
            raise PipelineRunValidationError(
                "legacy-unresolved execution profile cannot execute VLM"
            )
        if pending.stage == "vlm" and not snapshot.execution_profile.has_executable_plan:
            raise PipelineRunValidationError("execution profile cannot execute VLM")
        if (
            pending.stage == "media_preflight"
            and not snapshot.execution_profile.has_media_preflight_policy
        ):
            raise PipelineRunValidationError(
                "media-preflight cannot execute without its frozen policy"
            )
        command = await self._command_store.claim_next_pending(
            snapshot.run_id,
            expected_version=pending.version,
            lease_id=lease_id,
        )
        if command is None:
            return None
        if (
            type(command) is not PipelineCommand  # noqa: E721
            or command.status != "running"
            or command.lease_id != lease_id
            or command.version != pending.version + 1
        ):
            raise PipelineRunValidationError(
                "claimed command must bind the expected version and lease"
            )
        context = PipelineStageContext(
            snapshot.run_id,
            snapshot.request,
            command,
            snapshot.execution_profile,
        )
        stop = asyncio.Event()
        version = [command.version]
        heartbeat_error: list[BaseException] = []

        async def heartbeat() -> None:
            while True:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self._heartbeat_seconds)
                    return
                except TimeoutError:
                    try:
                        renewed = await self._command_store.renew_running_lease(
                            snapshot.run_id,
                            command_id=command.command_id,
                            expected_version=version[0],
                            lease_id=lease_id,
                        )
                        version[0] = renewed.version
                    except BaseException as error:  # ownership loss is authoritative
                        heartbeat_error.append(error)
                        return

        debug_context = PipelineStageDebugContext(
            context.run_id,
            command.stage,
            command.command_id,
            command.version,
            "execute",
        )
        stage_scope = (
            self._debug_sink.stage_scope(debug_context)
            if self._debug_sink is not None
            else nullcontext()
        )
        with stage_scope:
            if self._debug_sink is not None:
                self._debug_sink.capture_stage_input(
                    debug_context, value=_stage_input_snapshot(context)
                )
            heartbeat_task = asyncio.create_task(heartbeat())
            try:
                result = await self._registry.require(command.stage).execute(context)
            except Exception as error:
                if self._debug_sink is not None:
                    self._debug_sink.capture_stage_error(debug_context, error)
                result = PipelineStageResult(command.command_id, "indeterminate")
            finally:
                stop.set()
                await heartbeat_task
            if self._debug_sink is not None:
                self._debug_sink.capture_stage_output(
                    debug_context, value=_stage_output_snapshot(result)
                )
        if heartbeat_error:
            raise PipelineRunValidationError("stage command lease heartbeat was lost") from heartbeat_error[0]
        if type(result) is not PipelineStageResult or result.command_id != command.command_id:  # noqa: E721
            raise PipelineRunValidationError("stage result does not bind the dispatched command")
        await self._command_store.record_result(
            snapshot.run_id,
            result=result,
            expected_version=version[0],
            lease_id=lease_id,
        )
        return result


class PipelineStageReconciler:
    """Resolve an indeterminate command without repeating its external action."""

    def __init__(
        self,
        command_store: PipelineCommandClaimStore,
        ports: tuple[tuple[str, PipelineStageReconcilePort], ...],
    ) -> None:
        if not ports:
            raise PipelineRunValidationError("reconcile registry must not be empty")
        names: set[str] = set()
        for name, port in ports:
            if type(name) is not str or not name.strip():  # noqa: E721
                raise PipelineRunValidationError("reconcile stage name must be non-empty")
            if name in names:
                raise PipelineRunValidationError(f"duplicate reconcile stage name: {name}")
            if not callable(getattr(port, "reconcile", None)):
                raise PipelineRunValidationError(
                    f"reconcile stage {name} does not implement reconcile"
                )
            names.add(name)
        self._command_store = command_store
        self._ports = ports
        self._debug_sink: PipelineStageDebugSink | None = None

    @classmethod
    def from_ports(
        cls,
        command_store: PipelineCommandClaimStore,
        *ports: tuple[str, PipelineStageReconcilePort],
        debug_sink: PipelineStageDebugSink | None = None,
    ) -> PipelineStageReconciler:
        result = cls(command_store, ports)
        result._debug_sink = debug_sink
        return result

    def _require(self, name: str) -> PipelineStageReconcilePort:
        for registered_name, port in self._ports:
            if registered_name == name:
                return port
        raise PipelineRunValidationError(f"unregistered reconcile stage: {name}")

    async def reconcile(
        self,
        snapshot: PipelineRunSnapshot,
    ) -> PipelineStageResult | None:
        if type(snapshot) is not PipelineRunSnapshot:  # noqa: E721
            raise PipelineRunValidationError("reconciler requires a persisted run snapshot")
        uncertain = next(
            (item for item in snapshot.commands if item.status == "indeterminate"),
            None,
        )
        if uncertain is None:
            return None
        if uncertain.stage in ("stage1_narrative", "stage2_portfolio", "stage3_blueprint"):
            snapshot.execution_profile.build_stage1_command_policy()
        if uncertain.stage in ("stage2_portfolio", "stage3_blueprint"):
            snapshot.execution_profile.build_stage2_command_policy()
        if uncertain.stage == "stage3_blueprint":
            snapshot.execution_profile.build_stage3_command_policy()
        command = await self._command_store.read_indeterminate(
            snapshot.run_id,
            expected_version=uncertain.version,
        )
        if command is None:
            return None
        if command.status != "indeterminate" or command.version != uncertain.version:
            raise PipelineRunValidationError(
                "reconcile command must bind the indeterminate version"
            )
        if (
            command.stage == "media_preflight"
            and not snapshot.execution_profile.has_media_preflight_policy
        ):
            raise PipelineRunValidationError(
                "media-preflight cannot reconcile without its frozen policy"
            )
        context = PipelineStageContext(
            snapshot.run_id,
            snapshot.request,
            command,
            snapshot.execution_profile,
        )
        debug_context = PipelineStageDebugContext(
            context.run_id,
            command.stage,
            command.command_id,
            command.version,
            "reconcile",
        )
        stage_scope = (
            self._debug_sink.stage_scope(debug_context)
            if self._debug_sink is not None
            else nullcontext()
        )
        isolated_result = False
        with stage_scope:
            if self._debug_sink is not None:
                self._debug_sink.capture_stage_input(
                    debug_context, value=_stage_input_snapshot(context)
                )
            try:
                result = await self._require(command.stage).reconcile(context)
            except PipelineStageIsolationError as error:
                if self._debug_sink is not None:
                    self._debug_sink.capture_stage_error(debug_context, error)
                result = await self._command_store.record_isolated_failure(
                    snapshot.run_id,
                    failure=error,
                )
                isolated_result = True
            except Exception as error:
                if self._debug_sink is not None:
                    self._debug_sink.capture_stage_error(debug_context, error)
                raise
            if self._debug_sink is not None:
                self._debug_sink.capture_stage_output(
                    debug_context, value=_stage_output_snapshot(result)
                )
        if result is None:
            return None
        if (
            type(result) is not PipelineStageResult  # noqa: E721
            or result.command_id != command.command_id
            or result.outcome == "indeterminate"
        ):
            raise PipelineRunValidationError(
                "reconcile result must be an exact durable outcome for the command"
            )
        if isolated_result:
            # The store-minted isolation Receipt has already atomically
            # terminalized the exact indeterminate command.
            return result
        await self._command_store.record_reconciled_result(
            snapshot.run_id,
            result=result,
            expected_version=command.version,
        )
        return result


def _stage_input_snapshot(context: PipelineStageContext) -> dict[str, object]:
    """Stable non-authoritative input view, with detailed provider I/O nested below."""

    return {
        "request": context.request.to_mapping(),
        "command": context.command.to_mapping(),
        "execution_profile": {
            "schema_version": context.execution_profile.schema_version,
            "canonical_hash": context.execution_profile_hash,
        },
    }


def _stage_output_snapshot(result: PipelineStageResult | None) -> dict[str, object]:
    if result is None:
        return {"result": None}
    if type(result) is not PipelineStageResult:  # noqa: E721
        # A diagnostic snapshot must not replace the runner's authoritative
        # validation error when a malformed third-party stage result appears.
        return {"result_type": type(result).__name__}
    return {
        "result": {
            "command_id": result.command_id,
            "outcome": result.outcome,
            "receipt_id": str(result.receipt_id) if result.receipt_id is not None else None,
        }
    }
