"""Closed stage registration seam for runtime composition and workers."""

from __future__ import annotations

from .errors import PipelineRunValidationError
from .models import PipelineCommand, PipelineStageResult, validate_run_id
from .ports import PipelineCommandClaimStore, PipelineStagePort


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
    """Atomically lease one pending command before invoking a stage port.

    Running commands are not reclaimed here, and indeterminate commands are
    deliberately left for a separately injected reconciler in a later slice.
    """

    def __init__(
        self,
        registry: PipelineStageRegistry,
        command_store: PipelineCommandClaimStore,
    ) -> None:
        self._registry = registry
        self._command_store = command_store

    async def claim_and_execute(
        self,
        run_id: str,
        *,
        expected_version: int,
        lease_id: str,
    ) -> PipelineStageResult | None:
        validate_run_id(run_id)
        if type(expected_version) is not int or expected_version < 0:  # noqa: E721
            raise PipelineRunValidationError("expected_version must be a non-negative integer")
        if type(lease_id) is not str or not lease_id.strip():  # noqa: E721
            raise PipelineRunValidationError("lease_id must be a non-empty string")
        command = await self._command_store.claim_next_pending(
            run_id,
            expected_version=expected_version,
            lease_id=lease_id,
        )
        if command is None:
            return None
        if (
            type(command) is not PipelineCommand  # noqa: E721
            or command.status != "running"
            or command.lease_id != lease_id
            or command.version != expected_version + 1
        ):
            raise PipelineRunValidationError(
                "claimed command must bind the expected version and lease"
            )
        result = await self._registry.require(command.stage).execute(command)
        if type(result) is not PipelineStageResult or result.command_id != command.command_id:  # noqa: E721
            raise PipelineRunValidationError("stage result does not bind the dispatched command")
        return result
