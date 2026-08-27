"""Model I/O file diagnostics remain safe, optional, and non-authoritative."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_cut_bot.pipeline.debug import FileModelIoDebugSink, ModelIoDebugContext
from auto_cut_bot.pipeline.runtime.composition import (
    PIPELINE_MODEL_DEBUG_DIR_ENV,
    PipelineRuntimeConfigurationError,
    _model_io_debug_sink,
)
from auto_cut_bot.pipeline.vlm.ark_responses_transport import (
    ArkResponsesTransport,
    ArkResponsesTransportConfig,
)


class _Stream:
    def __init__(self, events: list[object]) -> None:
        self._events = events

    def __iter__(self):
        return iter(self._events)

    def close(self) -> None:
        pass


class _Client:
    def __init__(self, stream: _Stream) -> None:
        self.responses = self
        self._stream = stream

    def create(self, **_kwargs: object) -> _Stream:
        return self._stream

    def close(self) -> None:
        pass


def _response(*, status: str, text: str = '{"ok":true}') -> object:
    return SimpleNamespace(
        id="resp-debug-1",
        model="doubao-test",
        status=status,
        output=[
            SimpleNamespace(
                type="message",
                content=[SimpleNamespace(type="output_text", text=text)],
            )
        ],
    )


def test_file_sink_redacts_secrets_and_keeps_raw_output_outside_request(tmp_path: Path) -> None:
    sink = FileModelIoDebugSink(tmp_path / "model-io")
    context = ModelIoDebugContext("ark", "attempt:one", "doubao-test", "vlm")

    sink.capture_request(
        context,
        operation="dispatch",
        body={"Authorization": "Bearer secret", "api_key": "secret", "video": b"bytes"},
    )
    sink.capture_terminal(
        context,
        operation="dispatch",
        terminal={"status": "completed"},
        raw_output='{"semantic":"evidence"}',
    )

    request = next((tmp_path / "model-io").rglob("request.json"))
    terminal = request.with_name("terminal.json")
    raw = request.with_name("raw-output.bin")
    request_text = request.read_text(encoding="utf-8")
    assert "secret" not in request_text
    assert json.loads(request_text)["body"] == {
        "Authorization": "<redacted>",
        "api_key": "<redacted>",
        "video": {"redacted_bytes": 5},
    }
    assert json.loads(terminal.read_text(encoding="utf-8"))["terminal"] == {"status": "completed"}
    assert raw.read_bytes() == b'{"semantic":"evidence"}'


def test_ark_transport_mirrors_actual_body_and_incomplete_terminal(tmp_path: Path) -> None:
    created = SimpleNamespace(type="response.created", response=_response(status="in_progress"))
    incomplete = SimpleNamespace(type="response.incomplete", response=_response(status="incomplete"))
    sink = FileModelIoDebugSink(tmp_path / "model-io")
    transport = ArkResponsesTransport(
        ArkResponsesTransportConfig("key", "https://ark.invalid/api/v3", 10, 1024),
        client_factory=lambda **_kwargs: _Client(_Stream([created, incomplete])),
        debug_sink=sink,
    )
    context = ModelIoDebugContext("ark", "attempt-two", "doubao-test", "vlm_semantic_evidence")

    result = transport.dispatch(
        {"model": "doubao-test", "stream": True, "store": True, "input": [{"prompt": "x"}]},
        expected_model="doubao-test",
        on_provider_request_id=lambda _value: None,
        debug_context=context,
    )

    assert result.failure_code == "PROVIDER_RESPONSE_INCOMPLETE"
    request = next((tmp_path / "model-io").rglob("request.json"))
    terminal = request.with_name("terminal.json")
    assert json.loads(request.read_text(encoding="utf-8"))["body"]["input"] == [{"prompt": "x"}]
    terminal_value = json.loads(terminal.read_text(encoding="utf-8"))
    assert terminal_value["terminal"]["status"] == "incomplete"


def test_debug_sink_configuration_requires_a_non_repository_absolute_directory(tmp_path: Path) -> None:
    sink = _model_io_debug_sink({PIPELINE_MODEL_DEBUG_DIR_ENV: str(tmp_path / "debug")})
    assert isinstance(sink, FileModelIoDebugSink)
    assert sink.root == (tmp_path / "debug").resolve()

    with pytest.raises(PipelineRuntimeConfigurationError, match="absolute path"):
        _model_io_debug_sink({PIPELINE_MODEL_DEBUG_DIR_ENV: "debug"})
