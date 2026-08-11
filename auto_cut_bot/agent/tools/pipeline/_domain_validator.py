"""DomainValidator — validates pre/post conditions for domain agent execution.

Lightweight, stateless validation that checks whether all required upstream
artifacts are present in the ArtifactBus before a domain agent runs, and
whether all promised output artifacts were produced after it completes.

All methods are pure static functions. No state is held and no side effects
are performed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from auto_cut_bot.agent.tools.pipeline._domain_contract import DomainAgentContract


def _bus_has(bus: object, name: str) -> bool:
    """Check whether an artifact named *name* exists in the bus.

    Uses duck-typing: prefers ``bus.has(name)``, falls back to
    ``name in bus``, and returns ``False`` for anything else.

    This avoids a hard dependency on a concrete ``ArtifactBus`` class so
    the validator works with any reasonably-shaped bus implementation.
    """
    if hasattr(bus, "has"):
        return bool(bus.has(name))  # type: ignore[union-attr]
    if hasattr(bus, "__contains__"):
        return name in bus  # type: ignore[operator]
    return False


class DomainValidator:
    """Stateless pre/post-condition checks for domain agent execution.

    Usage::

        missing_inputs = DomainValidator.validate_prerequisites(
            contract, bus,
        )
        if missing_inputs:
            raise RuntimeError(f"Missing artifacts: {missing_inputs}")

        # ... run the domain agent ...

        missing_outputs = DomainValidator.validate_postconditions(
            contract, bus,
        )
        if missing_outputs:
            raise RuntimeError(f"Agent did not produce: {missing_outputs}")
    """

    @staticmethod
    def validate_prerequisites(
        contract: DomainAgentContract,
        bus: object,
    ) -> list[str]:
        """Check that all input artifacts required by *contract* exist in *bus*.

        Args:
            contract: The domain agent's contract declaring its dependencies.
            bus: An ``ArtifactBus``-compatible object.

        Returns:
            A list of artifact names that are missing from the bus. An empty
            list means all prerequisites are satisfied.
        """
        if not contract.input_artifacts:
            return []

        missing: list[str] = []
        for name in contract.input_artifacts:
            if not _bus_has(bus, name):
                missing.append(name)

        return missing

    @staticmethod
    def validate_postconditions(
        contract: DomainAgentContract,
        bus: object,
    ) -> list[str]:
        """Check that all output artifacts promised by *contract* were produced
        and are present in *bus*.

        Args:
            contract: The domain agent's contract declaring its outputs.
            bus: An ``ArtifactBus``-compatible object.

        Returns:
            A list of promised artifact names that are missing from the bus.
            An empty list means all postconditions hold.
        """
        if not contract.output_artifacts:
            return []

        missing: list[str] = []
        for name in contract.output_artifacts:
            if not _bus_has(bus, name):
                missing.append(name)

        return missing