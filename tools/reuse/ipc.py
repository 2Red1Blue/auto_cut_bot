"""JSONL IPC for producer subprocesses that need model calls.

The external producer process never touches network or credentials: it emits
`ProviderCallRequest/v1` lines on stdout and reads one
`ProviderCallResponse/v1` line back on stdin. The host validates provider and
schema, checks the shared budget ledger, calls the provider adapter and
returns a secret-free response.
"""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from tools.reuse.budget import BudgetLedger
from tools.reuse.models import (
    ErrorCode,
    ExperimentError,
    ProviderCallRequest,
    ProviderResult,
    load_json_strict,
)

RESPONSE_SCHEMA = "ProviderCallResponse/v1"


# ---------------------------------------------------------------------------
# Child side (linked into the producer process)
# ---------------------------------------------------------------------------


def child_emit_request(request: ProviderCallRequest, out: TextIO | None = None) -> dict[str, Any]:
    """Send one request over stdout and block for the host's response line."""
    stream = out if out is not None else sys.stdout
    stream.write(json.dumps(request.to_dict(), ensure_ascii=False) + "\n")
    stream.flush()
    line = sys.stdin.readline()
    if not line:
        raise ExperimentError(ErrorCode.PROVIDER_RESULT_UNKNOWN, "IPC host closed the pipe")
    response = load_json_strict(line, context="ipc response")
    if not isinstance(response, dict) or response.get("schema") != RESPONSE_SCHEMA:
        raise ExperimentError(ErrorCode.INVALID_SPEC, "unexpected IPC response schema")
    return response


# ---------------------------------------------------------------------------
# Host side
# ---------------------------------------------------------------------------


class IpcHost:
    """Serves one child's model requests against the shared budget ledger.

    Validates the request against the frozen spec identity (provider allowlist,
    expected model) before calling the provider; any exception degrades to an
    error response line instead of crashing the host."""

    MAX_LINE_BYTES = 2 * 1024 * 1024

    def __init__(
        self,
        provider: Any,
        ledger: BudgetLedger,
        allowed_providers: set[str],
        *,
        expected_model: str | None = None,
    ) -> None:
        self._provider = provider
        self._ledger = ledger
        self._allowed_providers = allowed_providers
        self._expected_model = expected_model

    def serve_line(self, line: str) -> dict[str, Any]:
        if len(line.encode("utf-8")) > self.MAX_LINE_BYTES:
            raise ExperimentError(ErrorCode.INVALID_SPEC, "IPC request line exceeds size limit")
        request = ProviderCallRequest.from_dict(load_json_strict(line, context="ipc request"))
        if request.provider not in self._allowed_providers:
            raise ExperimentError(
                ErrorCode.SOURCE_MISMATCH, f"provider {request.provider!r} not allowlisted for IPC"
            )
        if self._expected_model is not None and request.model != self._expected_model:
            raise ExperimentError(
                ErrorCode.SOURCE_MISMATCH,
                f"model {request.model!r} != frozen spec model {self._expected_model!r}",
            )
        self._ledger.check_can_call()
        result: ProviderResult = self._provider.call(request)
        self._ledger.charge(result.usage)
        return self._response(request, result)

    def serve_stream(self, reader: TextIO, writer: TextIO) -> int:
        served = 0
        for line in reader:
            line = line.strip()
            if not line:
                continue
            try:
                response = self.serve_line(line)
            except ExperimentError as exc:
                response = {
                    "schema": RESPONSE_SCHEMA,
                    "status": "error",
                    "error_code": exc.code.value,
                    "message": exc.message,
                }
            except Exception as exc:  # noqa: BLE001 - child input must never crash the host
                response = {
                    "schema": RESPONSE_SCHEMA,
                    "status": "error",
                    "error_code": ErrorCode.INVALID_SPEC.value,
                    "message": f"unexpected IPC failure: {type(exc).__name__}",
                }
            writer.write(json.dumps(response, ensure_ascii=False) + "\n")
            writer.flush()
            served += 1
            if response.get("status") == "error":
                break
        return served

    @staticmethod
    def _response(request: ProviderCallRequest, result: ProviderResult) -> dict[str, Any]:
        return {
            "schema": RESPONSE_SCHEMA,
            "call_id": request.call_id,
            "status": result.status,
            "raw": result.raw,
            "provider_response_id": result.provider_response_id,
            "usage": {
                "input_tokens": result.usage.input_tokens,
                "output_tokens": result.usage.output_tokens,
            },
        }
