from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from websockets.datastructures import Headers
from websockets.http11 import Request as WsRequest

from auto_cut_bot.bus.queue import MessageBus
from auto_cut_bot.channels.manager import ChannelManager
from auto_cut_bot.channels.plugin import load_channel_package
from auto_cut_bot.channels.websocket.runtime import WebSocketConfig
from auto_cut_bot.config.schema import Config
from auto_cut_bot.pipeline.runtime.highlight_projection import PipelineHighlightReadService
from auto_cut_bot.pipeline.runtime.models import PipelineRunRequest
from auto_cut_bot.webui.gateway_services import build_gateway_services
from auto_cut_bot.webui.ws_http import GatewayHTTPHandler

_RUN_ID = "pipeline_run_0123456789abcdef0123456789abcdef"
_PATH = f"/api/pipeline/runs/{_RUN_ID}/highlights"


class _ProjectionResult:
    def __init__(self, mapping: dict[str, object]) -> None:
        self._mapping = mapping

    def to_mapping(self) -> dict[str, object]:
        return self._mapping


class _ReadService:
    def __init__(
        self,
        result: _ProjectionResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self._result = result
        self._error = error
        self.calls: list[str] = []

    async def get(self, run_id: str) -> _ProjectionResult:
        self.calls.append(run_id)
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


class _RunStore:
    def __init__(self, snapshot: object | None) -> None:
        self.snapshot = snapshot
        self.calls: list[str] = []

    async def read_run(self, run_id: str) -> object | None:
        self.calls.append(run_id)
        return self.snapshot


class _NoEvidenceStore:
    pass


def _request(*, method: str = "GET") -> WsRequest:
    return cast(WsRequest, SimpleNamespace(method=method))


def _handler(
    *,
    authorized: bool,
    service: _ReadService | None,
) -> GatewayHTTPHandler:
    handler = object.__new__(GatewayHTTPHandler)
    handler.check_api_token = MagicMock(return_value=authorized)
    handler.pipeline_highlight_read_service = service
    handler._log = MagicMock()
    return handler


@pytest.mark.asyncio
async def test_pipeline_highlights_authenticates_before_parsing_or_service_call() -> None:
    service = _ReadService(_ProjectionResult({"status": "ready", "items": []}))
    handler = _handler(authorized=False, service=service)

    response = await handler._dispatch_pipeline_highlight_routes(
        _request(),
        "/api/pipeline/runs/not-a-run-id/highlights",
    )

    assert response is not None
    assert response.status_code == 401
    assert service.calls == []
    handler.check_api_token.assert_called_once()


@pytest.mark.asyncio
async def test_pipeline_highlights_returns_the_projection_mapping_unchanged() -> None:
    mapping: dict[str, object] = {
        "status": "ready",
        "items": [{"candidate_id": "candidate-1", "semantic_window": {"precision": "coarse_only"}}],
    }
    service = _ReadService(_ProjectionResult(mapping))
    handler = _handler(authorized=True, service=service)

    response = await handler._dispatch_pipeline_highlight_routes(_request(), _PATH)

    assert response is not None
    assert response.status_code == 200
    assert json.loads(response.body) == mapping
    assert service.calls == [_RUN_ID]


@pytest.mark.asyncio
async def test_authenticated_gateway_calls_the_real_highlight_read_service(
    tmp_path: Path,
) -> None:
    run_store = _RunStore(
        SimpleNamespace(
            commands=(),
            request=PipelineRunRequest("test", source_root="/authorized/source"),
            execution_profile=SimpleNamespace(is_legacy_unresolved=False),
            execution_profile_hash="profile-sha",
        )
    )
    service = PipelineHighlightReadService(
        cast(Any, run_store),
        cast(Any, _NoEvidenceStore()),
    )
    gateway = build_gateway_services(
        config=WebSocketConfig(),
        bus=MagicMock(),
        session_manager=None,
        static_dist_path=None,
        workspace_path=tmp_path,
        default_restrict_to_workspace=False,
        config_path=tmp_path / "config.json",
        runtime_model_name=None,
        runtime_surface="browser",
        runtime_capabilities_overrides=None,
        pipeline_highlight_read_service=service,
    )
    api_token = gateway.tokens.issue_api_token(300)
    request = WsRequest(_PATH, Headers({"Authorization": f"Bearer {api_token}"}))

    response = await gateway.http.dispatch(SimpleNamespace(remote_address=("127.0.0.1", 1)), request)

    assert response is not None
    assert response.status_code == 200
    assert json.loads(response.body) == {"status": "not_ready"}
    assert run_store.calls == [_RUN_ID]


@pytest.mark.asyncio
async def test_pipeline_highlights_keeps_malformed_and_unknown_runs_generic() -> None:
    malformed_service = _ReadService(_ProjectionResult({"status": "ready", "items": []}))
    malformed_handler = _handler(authorized=True, service=malformed_service)

    malformed = await malformed_handler._dispatch_pipeline_highlight_routes(
        _request(),
        "/api/pipeline/runs/not-a-run-id/highlights",
    )

    assert malformed is not None
    assert malformed.status_code == 404
    assert malformed.body == b"API route not found"
    assert malformed_service.calls == []

    bare = await malformed_handler._dispatch_pipeline_highlight_routes(
        _request(),
        "/api/pipeline/runs/0123456789abcdef0123456789abcdef/highlights",
    )

    assert bare is not None
    assert bare.status_code == 404
    assert bare.body == b"API route not found"
    assert malformed_service.calls == []

    unknown_service = _ReadService(error=RuntimeError("protected source receipt"))
    unknown_handler = _handler(authorized=True, service=unknown_service)

    unknown = await unknown_handler._dispatch_pipeline_highlight_routes(_request(), _PATH)

    assert unknown is not None
    assert unknown.status_code == 500
    assert unknown.body == b"pipeline highlights unavailable"
    assert b"protected source receipt" not in unknown.body
    assert unknown_service.calls == [_RUN_ID]


@pytest.mark.asyncio
async def test_pipeline_highlights_returns_generic_503_when_not_composed() -> None:
    response = await _handler(authorized=True, service=None)._dispatch_pipeline_highlight_routes(
        _request(),
        _PATH,
    )

    assert response is not None
    assert response.status_code == 503
    assert response.body == b"pipeline highlights unavailable"


@pytest.mark.asyncio
async def test_pipeline_highlights_does_not_route_non_get_requests() -> None:
    service = _ReadService(_ProjectionResult({"status": "ready", "items": []}))

    response = await _handler(authorized=True, service=service)._dispatch_pipeline_highlight_routes(
        _request(method="POST"),
        _PATH,
    )

    assert response is None
    assert service.calls == []


def test_gateway_composition_reuses_the_injected_read_service_without_calling_it(
    tmp_path: Path,
) -> None:
    service = _ReadService(_ProjectionResult({"status": "ready", "items": []}))

    gateway = build_gateway_services(
        config=WebSocketConfig(),
        bus=MagicMock(),
        session_manager=None,
        static_dist_path=None,
        workspace_path=tmp_path,
        default_restrict_to_workspace=False,
        config_path=tmp_path / "config.json",
        runtime_model_name=None,
        runtime_surface="browser",
        runtime_capabilities_overrides=None,
        pipeline_highlight_read_service=cast(Any, service),
    )

    assert gateway.pipeline_highlight_read_service is service
    assert gateway.http.pipeline_highlight_read_service is service
    assert service.calls == []


def test_channel_manager_composes_the_read_service_once_for_the_webui_gateway(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plugin = load_channel_package("websocket")
    assert plugin is not None
    monkeypatch.setattr(
        "auto_cut_bot.channels.registry.discover_plugins",
        lambda: {"websocket": plugin},
    )
    service = _ReadService(_ProjectionResult({"status": "ready", "items": []}))
    compose_calls: list[None] = []
    monkeypatch.setattr(
        "auto_cut_bot.pipeline.runtime.composition.compose_pipeline_highlight_read_service_from_environment",
        lambda: compose_calls.append(None) or service,
    )
    config = Config.model_validate({
        "channels": {
            "websocket": {
                "enabled": True,
                "allowFrom": ["*"],
                "websocketRequiresToken": False,
            }
        }
    })

    manager = ChannelManager(
        config,
        MessageBus(),
        config_path=tmp_path / "config.json",
        webui_static_dist=False,
    )

    channel = manager.channels["websocket"]
    assert channel.gateway.pipeline_highlight_read_service is service
    assert compose_calls == [None]
    assert service.calls == []
