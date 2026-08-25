"""Deployment-owned authority loaders."""

from .locked_registry_source import (
    LockedRegistryDeployment,
    LockedRegistrySourceError,
    load_locked_timed_speech_authority_context,
)

__all__ = [
    "LockedRegistryDeployment",
    "LockedRegistrySourceError",
    "load_locked_timed_speech_authority_context",
]
