from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from websockets.http11 import Request as WsRequest

from auto_cut_bot.channels.websocket.runtime import WebSocketConfig
from auto_cut_bot.webui.gateway_services import build_gateway_services
from auto_cut_bot.webui.ws_http import GatewayHTTPHandler

_RUN_ID = "0123456789abcdef0123456789abcdef"
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
