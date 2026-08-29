from __future__ import annotations

from pathlib import Path

import pytest

from auto_cut_bot.cli.kernel_origin import KernelOriginError, validate_kernel_origin


def test_worktree_kernel_origin_is_explicitly_verifiable() -> None:
    import autocut_kernel

    package_file = Path(autocut_kernel.__file__).resolve()
    source_root = package_file.parents[1]

    assert validate_kernel_origin(expected_root=source_root) == package_file


def test_pipeline_startup_auto_detects_a_checkout_kernel() -> None:
    import autocut_kernel

    assert validate_kernel_origin(auto_detect=True) == Path(autocut_kernel.__file__).resolve()


def test_installed_kernel_is_rejected_when_worktree_source_is_required(tmp_path: Path) -> None:
    with pytest.raises(KernelOriginError, match="outside the required worktree source root"):
        validate_kernel_origin(expected_root=tmp_path)


def test_origin_check_is_disabled_for_normal_wheel_deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AUTO_CUT_BOT_KERNEL_SOURCE_ROOT", raising=False)

    assert validate_kernel_origin() is None
