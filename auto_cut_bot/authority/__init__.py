"""Deployment-injected authority provenance boundaries."""

from .governed_registry_snapshot import (
    GovernedRegistryDeployment,
    GovernedRegistrySnapshotError,
    VerifiedAuthoritySourceSnapshot,
    load_verified_authority_source_snapshot,
)

__all__ = [
    "GovernedRegistryDeployment",
    "GovernedRegistrySnapshotError",
    "VerifiedAuthoritySourceSnapshot",
    "load_verified_authority_source_snapshot",
]
