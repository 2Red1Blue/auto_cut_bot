"""Closure must reject missing canonical command evidence."""

from __future__ import annotations

from pathlib import Path

import pytest

KERNEL_SOURCE = Path(__file__).resolve().parents[2] / "packages" / "autocut-kernel" / "src"


@pytest.fixture(autouse=True)
def _kernel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.syspath_prepend(str(KERNEL_SOURCE))


def test_empty_snapshot_fails_closed_on_command_matrix() -> None:
    from autocut_kernel.contracts.compiler.registry_closure import closure_errors

    assert "command matrix is incomplete or contains an unknown command" in closure_errors(
        (), (), (), (), ()
    )
