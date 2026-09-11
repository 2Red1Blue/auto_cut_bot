"""HTTP application injection tests for the shadow-bootstrap observation entry."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from auto_cut_bot.api.server import (
    _PIPELINE_SHADOW_BOOTSTRAP_ENTRY_KEY,
    create_pipeline_app,
)


def test_pipeline_app_injects_composed_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    from auto_cut_bot.api import server

    sentinel = object()
    monkeypatch.setattr(
        server,
        "compose_shadow_bootstrap_entry_from_environment",
        lambda: sentinel,
    )
    app = create_pipeline_app(
        api_key="pipeline-only-secret",
        pipeline_runtime=SimpleNamespace(service=None),
    )
    assert app[_PIPELINE_SHADOW_BOOTSTRAP_ENTRY_KEY] is sentinel


def test_pipeline_app_keeps_route_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    from auto_cut_bot.api import server

    monkeypatch.setattr(
        server,
        "compose_shadow_bootstrap_entry_from_environment",
        lambda: None,
    )
    app = create_pipeline_app(
        api_key="pipeline-only-secret",
        pipeline_runtime=SimpleNamespace(service=None),
    )
    paths = {str(resource.canonical) for resource in app.router.resources()}
    assert "/v1/pipeline/shadow-bootstrap-observation" in paths
