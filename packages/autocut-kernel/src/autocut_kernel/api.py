"""Small, dependency-free values shared by runtime composition roots."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class KernelIdentity:
    """The explicit identity of a kernel build selected by a runtime."""

    name: str
    version: str
