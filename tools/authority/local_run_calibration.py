"""Build-side adapter to the Kernel-owned accepted-calibration comparison."""

from __future__ import annotations

from autocut_kernel.registry.calibration_binding import (
    CalibrationBindingError,
    CalibrationRecordAnchorReader,
    bind_profile_calibration,
)
from autocut_kernel.store.models import PersistedCalibrationRecordAnchor

from .errors import GateViolation
from .local_run_context import LockedLocalRunSourceContext


def bind_local_run_calibration(
    *, context: LockedLocalRunSourceContext, store: CalibrationRecordAnchorReader,
) -> PersistedCalibrationRecordAnchor:
    """Bind verified source inputs; grant no runtime permission or Store write."""
    if type(context) is not LockedLocalRunSourceContext:  # noqa: E721
        raise GateViolation("AUTH-LOCAL-CALIBRATION", "requires a locked local-run source context")
    try:
        return bind_profile_calibration(
            local_run=context.local_run, shadow=context.predecessor.profiles.shadow,
            predecessor_registry_sha256=context.predecessor.compilation.registry_sha256,
            store=store,
        )
    except CalibrationBindingError as error:
        raise GateViolation("AUTH-LOCAL-CALIBRATION", str(error)) from error
