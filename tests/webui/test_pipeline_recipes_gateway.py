from __future__ import annotations

import json
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import pytest
from websockets.http11 import Request as WsRequest

from auto_cut_bot.webui.ws_http import GatewayHTTPHandler

_RUN_ID = "pipeline_run_0123456789abcdef0123456789abcdef"
_TIMELINE = f"/api/pipeline/runs/{_RUN_ID}/recipes/story-1/timeline?revision=2"
_DIFF = f"/api/pipeline/runs/{_RUN_ID}/recipes/story-1/diff?base_revision=1&target_revision=2"


class _Result:
    def __init__(self, mapping: dict[str, object]) -> None:
        self._mapping = mapping

    def to_mapping(self) -> dict[str, object]:
        return self._mapping


class _RecipeService:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.timeline_calls: list[tuple[str, str, int]] = []
        self.diff_calls: list[tuple[str, str, int, int]] = []

    async def get_timeline(self, run_id: str, story_id: str, revision: int) -> _Result:
        self.timeline_calls.append((run_id, story_id, revision))
        if self.error is not None:
            raise self.error
        return _Result({"status": "ready", "timeline": {"story_id": story_id, "revision": revision}})

    async def get_diff(self, run_id: str, story_id: str, base: int, target: int) -> _Result:
        self.diff_calls.append((run_id, story_id, base, target))
        if self.error is not None:
            raise self.error
        return _Result({"status": "ready", "diff": {"story_id": story_id, "requires_qc": True}})


def _request(path: str, *, method: str = "GET") -> WsRequest:
    return cast(WsRequest, SimpleNamespace(path=path, method=method))


def _handler(*, authorized: bool, service: _RecipeService | None) -> GatewayHTTPHandler:
    handler = object.__new__(GatewayHTTPHandler)
    handler.check_api_token = MagicMock(return_value=authorized)
    handler.pipeline_recipe_read_service = service
    handler._log = MagicMock()
    return handler


@pytest.mark.asyncio
async def test_recipe_route_authenticates_before_path_parsing() -> None:
    service = _RecipeService()
    response = await _handler(authorized=False, service=service)._dispatch_pipeline_recipe_routes(
        _request("/api/pipeline/runs/not-a-run-id/recipes/../timeline"),
        "/api/pipeline/runs/not-a-run-id/recipes/../timeline",
    )

    assert response is not None and response.status_code == 401
    assert service.timeline_calls == service.diff_calls == []


@pytest.mark.asyncio
async def test_recipe_timeline_and_diff_routes_preserve_service_payloads() -> None:
    service = _RecipeService()
    handler = _handler(authorized=True, service=service)

    timeline = await handler._dispatch_pipeline_recipe_routes(_request(_TIMELINE), _TIMELINE.split("?", 1)[0])
    diff = await handler._dispatch_pipeline_recipe_routes(_request(_DIFF), _DIFF.split("?", 1)[0])

    assert timeline is not None and timeline.status_code == 200
    assert json.loads(timeline.body)["timeline"]["revision"] == 2
    assert diff is not None and diff.status_code == 200
    assert json.loads(diff.body)["diff"]["requires_qc"] is True
    assert service.timeline_calls == [(_RUN_ID, "story-1", 2)]
    assert service.diff_calls == [(_RUN_ID, "story-1", 1, 2)]


@pytest.mark.asyncio
async def test_recipe_route_rejects_invalid_queries_and_hides_service_errors() -> None:
    invalid = await _handler(authorized=True, service=_RecipeService())._dispatch_pipeline_recipe_routes(
        _request(f"{_TIMELINE}&revision=3"), _TIMELINE.split("?", 1)[0]
    )
    assert invalid is not None and invalid.status_code == 400

    failing = await _handler(authorized=True, service=_RecipeService(error=RuntimeError("private receipt")))._dispatch_pipeline_recipe_routes(
        _request(_TIMELINE), _TIMELINE.split("?", 1)[0]
    )
    assert failing is not None and failing.status_code == 500
    assert b"private receipt" not in failing.body


@pytest.mark.asyncio
async def test_recipe_route_reports_uncomposed_and_ignores_non_get() -> None:
    unavailable = await _handler(authorized=True, service=None)._dispatch_pipeline_recipe_routes(
        _request(_TIMELINE), _TIMELINE.split("?", 1)[0]
    )
    assert unavailable is not None and unavailable.status_code == 503

    service = _RecipeService()
    response = await _handler(authorized=True, service=service)._dispatch_pipeline_recipe_routes(
        _request(_TIMELINE, method="POST"), _TIMELINE.split("?", 1)[0]
    )
    assert response is None
    assert service.timeline_calls == []
