"""Startup checks for selecting the authoritative checkout Kernel."""

from __future__ import annotations

import os
from pathlib import Path

KERNEL_SOURCE_ROOT_ENV = "AUTO_CUT_BOT_KERNEL_SOURCE_ROOT"


class KernelOriginError(RuntimeError):
    """The process loaded Kernel code from an unexpected installation."""


def validate_kernel_origin(
    *, expected_root: str | Path | None = None, auto_detect: bool = False
) -> Path | None:
    """Require ``autocut_kernel`` to come from an explicitly selected source root.

    A normal wheel deployment has no checkout and therefore leaves the check
    disabled.  Worktree runs set ``AUTO_CUT_BOT_KERNEL_SOURCE_ROOT`` (or pass
    ``expected_root``) so an older installed wheel cannot silently shadow the
    code being tested.  The check returns the resolved package file for a
    startup log and raises before the service constructs its runtime.
    """

    configured_root = expected_root or os.environ.get(KERNEL_SOURCE_ROOT_ENV)
    if not configured_root and auto_detect:
        # In a source checkout the root package is imported from
        # ``<repo>/auto_cut_bot``.  Derive the sibling Kernel source without
        # trusting the current working directory (which may be a subdirectory).
        import auto_cut_bot

        app_file = Path(auto_cut_bot.__file__ or "").resolve()
        checkout_root = app_file.parents[1]
        candidate = checkout_root / "packages" / "autocut-kernel" / "src"
        if (candidate / "autocut_kernel" / "__init__.py").is_file():
            configured_root = candidate
    if not configured_root:
        return None

    import autocut_kernel

    package_file = Path(autocut_kernel.__file__ or "").resolve()
    source_root = Path(configured_root).expanduser().resolve()
    if not package_file.is_relative_to(source_root):
        raise KernelOriginError(
            "autocut_kernel was loaded from "
            f"{package_file}, outside the required worktree source root {source_root}; "
            "set PYTHONPATH to the checked-out packages/autocut-kernel/src before starting"
        )
    return package_file


__all__ = ["KERNEL_SOURCE_ROOT_ENV", "KernelOriginError", "validate_kernel_origin"]
