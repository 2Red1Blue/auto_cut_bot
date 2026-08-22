"""The checked-in partial source tree is never a runtime fallback."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_repository_source_cannot_be_selected_as_an_implicit_registry() -> None:
    from autocut_kernel.contracts.compiler.errors import RegistryValidationError
    from autocut_kernel.contracts.compiler.registry_source import compile_registry_source

    source = (
        Path(__file__).resolve().parents[2]
        / "packages"
        / "autocut-kernel"
        / "src"
        / "autocut_kernel"
        / "contracts"
        / "source"
        / "2_1_3"
    )
    with pytest.raises(RegistryValidationError):
        compile_registry_source(source)
