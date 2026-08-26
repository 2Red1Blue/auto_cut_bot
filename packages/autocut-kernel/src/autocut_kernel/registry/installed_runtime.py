"""Read-only startup binding for content loaded from the controlled installed wheel.

A typed resource is content, not an authority capability. Production composition
must use the fixed installed loader and a real Store; neither this resolver nor a
fake Store establishes source provenance or database acceptance.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .calibration_binding import CalibrationRecordAnchorReader, bind_profile_calibration
from .installed_local_run import LocalRunResource, load_installed_local_run_resource
from .timed_speech import (
    AuthorityRegistrySnapshot,
    AuthorityRegistryStore,
    BootstrappedTimedSpeechProfile,
    StoreAnchoredTimedSpeechProfileResolver,
    TimedSpeechProfileKey,
)


class InstalledLocalRunError(ValueError):
    """Installed content differs from its exact persisted runtime profile."""


class InstalledLocalRunAuthorityStore(CalibrationRecordAnchorReader, AuthorityRegistryStore, Protocol):
    """Only the two authoritative reads needed for installed startup."""


@dataclass(frozen=True, slots=True)
class InstalledLocalRunProfileResolver:
    resource: LocalRunResource

    def __post_init__(self) -> None:
        if type(self.resource) is not LocalRunResource:  # noqa: E721
            raise InstalledLocalRunError("requires an exact decoded installed local-run resource")

    @property
    def snapshot(self) -> AuthorityRegistrySnapshot:
        entry = self.resource.local_run.timed_speech_registry_entry
        return AuthorityRegistrySnapshot(
            self.resource.current_registry_sha256,
            TimedSpeechProfileKey(entry.profile_id, entry.profile_version),
        )

    def resolve(self, store: InstalledLocalRunAuthorityStore) -> BootstrappedTimedSpeechProfile:
        bind_profile_calibration(
            local_run=self.resource.local_run,
            shadow=self.resource.shadow,
            predecessor_registry_sha256=self.resource.predecessor_registry_sha256,
            store=store,
        )
        resolved = StoreAnchoredTimedSpeechProfileResolver(self.snapshot).resolve(store)
        if resolved.entry != self.resource.local_run.timed_speech_registry_entry:
            raise InstalledLocalRunError("bootstrapped profile differs from installed local-run entry")
        return resolved


def load_installed_local_run_resolver() -> InstalledLocalRunProfileResolver:
    """Load only the fixed package resource; no caller snapshot or path override."""
    return InstalledLocalRunProfileResolver(load_installed_local_run_resource())
