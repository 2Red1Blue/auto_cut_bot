"""Explicit admin bootstrap for the controlled installed local-run resource.

This is separate from read-only runtime startup. It requires previously accepted
calibration and delegates all claim, commit and replay behavior to the existing
protected bootstrap command. It never measures or manufactures calibration.
"""

from __future__ import annotations

from typing import Protocol

from ..store.models import CommandOutcome
from .calibration_binding import CalibrationRecordAnchorReader, bind_profile_calibration
from .installed_local_run import load_installed_local_run_resource
from .timed_speech import (
    AuthorityBootstrapStore,
    AuthorityRegistrySnapshot,
    BootstrapTimedSpeechProfileRegistryCommand,
    TimedSpeechProfileKey,
    VerifiedTimedSpeechAuthorityContext,
)


class InstalledLocalRunBootstrapStore(CalibrationRecordAnchorReader, AuthorityBootstrapStore, Protocol):
    """Admin-only Store surface; not a runtime startup dependency."""


def bootstrap_installed_local_run(store: InstalledLocalRunBootstrapStore) -> CommandOutcome:
    """Bootstrap the fixed installed entry only after its accepted-anchor check."""
    resource = load_installed_local_run_resource()
    bind_profile_calibration(
        local_run=resource.local_run,
        shadow=resource.shadow,
        predecessor_registry_sha256=resource.predecessor_registry_sha256,
        store=store,
    )
    entry = resource.local_run.timed_speech_registry_entry
    snapshot = AuthorityRegistrySnapshot(
        resource.current_registry_sha256,
        TimedSpeechProfileKey(entry.profile_id, entry.profile_version),
    )
    request = VerifiedTimedSpeechAuthorityContext(snapshot, entry).bootstrap_request()
    return BootstrapTimedSpeechProfileRegistryCommand(store).execute(request)
