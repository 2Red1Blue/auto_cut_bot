"""Compose Agent Runtime state from the kernel's public API only."""

from dataclasses import dataclass

from autocut_kernel import KernelIdentity


@dataclass(frozen=True, slots=True)
class AgentRuntimeComposition:
    """The kernel selected by an Agent Runtime composition root."""

    kernel: KernelIdentity


def compose_runtime(kernel: KernelIdentity) -> AgentRuntimeComposition:
    """Bind an explicit kernel identity without reaching into its internals."""

    return AgentRuntimeComposition(kernel=kernel)
