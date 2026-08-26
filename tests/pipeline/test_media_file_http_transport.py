from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, cast

import httpx
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from auto_cut_bot.pipeline.media_preflight.http_transport import HttpxFileTransport
from auto_cut_bot.pipeline.media_preflight.models import LocalMediaToolError

ENDPOINT = "http://127.0.0.1:18765/v1/shadow-calibration-funasr-raw"


@pytest.mark.asyncio
async def test_real_http_upload_ignores_poisoned_environment_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.mp4"
    content = b"multiple upload chunks" * 10_000
    source.write_bytes(content)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    received: list[bytes] = []

    async def receive(request: web.Request) -> web.Response:
        assert request.headers["Authorization"] == "Bearer test-token"
        received.append(await request.read())
        return web.Response(body=b"native result")

    app = web.Application()
    app.router.add_post("/v1/shadow-calibration-funasr-raw", receive)
    async with TestServer(app) as server:
        result = await asyncio.to_thread(
            HttpxFileTransport().post,
            str(server.make_url("/v1/shadow-calibration-funasr-raw")),
            headers={"Authorization": "Bearer test-token"},
            body_path=source,
            timeout_seconds=3,
            max_response_bytes=13,
        )
    assert result == (200, b"native result")
    assert received == [content]


def test_transport_streams_file_directly_without_proxy_or_redirect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source content")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    bodies: list[BinaryIO] = []
    response = httpx.Response(200, content=b"native result")

    @contextmanager
    def stream(method: str, url: str, **kwargs: object) -> Iterator[httpx.Response]:
        assert method == "POST" and url == ENDPOINT
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        assert kwargs["timeout"] == 3.0
        assert kwargs["headers"] == {"Authorization": "Bearer test-token"}
        # Only an open file, never a copy of all source bytes, crosses the transport boundary.
        body = cast(BinaryIO, kwargs["content"])
        assert body.read() == b"source content"
        bodies.append(body)
        try:
            yield response
        finally:
            response.close()

    monkeypatch.setattr(httpx, "stream", stream)
    assert HttpxFileTransport().post(
        ENDPOINT,
        headers={"Authorization": "Bearer test-token"},
        body_path=source,
        timeout_seconds=3,
        max_response_bytes=20,
    ) == (200, b"native result")
    assert len(bodies) == 1 and bodies[0].closed
    assert response.is_closed


@pytest.mark.parametrize("failure", ["oversized", "timeout", "connection", "redirect"])
def test_transport_failure_closes_stream_and_never_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    bodies: list[BinaryIO] = []
    response = httpx.Response(
        307 if failure == "redirect" else 200,
        content=b"123456",
        headers={"Location": "http://example.invalid/private"},
    )

    @contextmanager
    def stream(_method: str, _url: str, **kwargs: object) -> Iterator[httpx.Response]:
        bodies.append(cast(BinaryIO, kwargs["content"]))
        try:
            if failure == "timeout":
                raise httpx.ReadTimeout("unknown outcome")
            if failure == "connection":
                raise httpx.ConnectError("unavailable")
            yield response
        finally:
            response.close()

    monkeypatch.setattr(httpx, "stream", stream)
    limit = 5 if failure == "oversized" else 6
    if failure == "redirect":
        assert HttpxFileTransport().post(
            ENDPOINT, headers={}, body_path=source, timeout_seconds=3, max_response_bytes=limit
        ) == (307, b"123456")
    else:
        with pytest.raises(LocalMediaToolError) as error:
            HttpxFileTransport().post(
                ENDPOINT, headers={}, body_path=source, timeout_seconds=3, max_response_bytes=limit
            )
        if failure != "oversized":
            assert error.value.code == "TIMED_SPEECH_RESULT_UNKNOWN"
    assert len(bodies) == 1 and bodies[0].closed
    assert response.is_closed


@pytest.mark.parametrize("timeout,limit", [(0, 1), (1, 0), (True, 1), (1, True), (-1, 1)])
def test_transport_invalid_limits_do_not_open_a_file(timeout: int, limit: int) -> None:
    with pytest.raises(LocalMediaToolError, match="positive integers"):
        HttpxFileTransport().post(
            ENDPOINT,
            headers={},
            body_path=Path("/nonexistent/never-open.mp4"),
            timeout_seconds=timeout,
            max_response_bytes=limit,
        )
