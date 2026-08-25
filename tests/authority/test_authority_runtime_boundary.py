"""Packaged runtime must not depend on repository-only authority tooling."""

from __future__ import annotations

import importlib.util
import inspect

from auto_cut_bot.pipeline.runtime import composition


def test_packaged_runtime_has_no_governed_source_loader_or_tools_authority_import() -> None:
    assert "tools.authority" not in inspect.getsource(composition)
    assert importlib.util.find_spec("auto_cut_bot.authority") is None
