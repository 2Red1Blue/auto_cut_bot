"""Platform boundary tests for the physical-media staging ledger."""

from __future__ import annotations

import os
from pathlib import Path

import autocut_kernel.store.postgres as postgres_module
import pytest
from autocut_kernel.store.models import MaterializationError


def test_missing_posix_flock_rejects_materialization_without_process_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Windows must not replace the shared filesystem lock with a local lock."""

    lock_path = tmp_path / "ledger.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        monkeypatch.setattr(postgres_module, "_fcntl", None)
        with pytest.raises(MaterializationError) as error:
            postgres_module._acquire_materialization_ledger_lock(descriptor)
    finally:
        os.close(descriptor)

    assert error.value.code == "MEDIA_MATERIALIZATION_INFRASTRUCTURE_FAILED"
    assert error.value.outcome == "failed"
