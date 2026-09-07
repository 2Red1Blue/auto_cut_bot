"""JSONL IPC host/child protocol tests, including a real subprocess round-trip."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools.reuse.budget import BudgetLedger
from tools.reuse.ipc import IpcHost
from tools.reuse.models import (
    Budget,
    ErrorCode,
    ExperimentError,
    ProviderCallRequest,
)
from tools.reuse.providers import FakeProvider

REPO_ROOT = Path(__file__).resolve().parents[2]


def _host(**budget_kw: int) -> IpcHost:
    budget = Budget(
        max_calls=budget_kw.get("max_calls", 5),
        max_input_tokens=budget_kw.get("max_input_tokens", 10_000),
        max_output_tokens=budget_kw.get("max_output_tokens", 10_000),
        max_concurrency=1,
    )
    return IpcHost(FakeProvider(), BudgetLedger(budget), allowed_providers={"fake"})


class TestIpcHost:
    def test_round_trip_line(self) -> None:
        host = _host()
        request = ProviderCallRequest(call_id="c1", provider="fake", model="m", payload={"n": 1})
        response = host.serve_line(json.dumps(request.to_dict()))
        assert response["schema"] == "ProviderCallResponse/v1"
        assert response["status"] == "done"
        assert response["call_id"] == "c1"
        assert "usage" in response and "raw" in response

    def test_non_allowlisted_provider_refused(self) -> None:
        host = _host()
        request = ProviderCallRequest(call_id="c1", provider="openai", model="m", payload={})
        with pytest.raises(ExperimentError) as exc:
            host.serve_line(json.dumps(request.to_dict()))
        assert exc.value.code == ErrorCode.SOURCE_MISMATCH

    def test_budget_exhausted_via_ipc(self) -> None:
        host = _host(max_calls=1)
        request = ProviderCallRequest(call_id="c1", provider="fake", model="m", payload={"n": 1})
        assert host.serve_line(json.dumps(request.to_dict()))["status"] == "done"
        request2 = ProviderCallRequest(call_id="c2", provider="fake", model="m", payload={"n": 2})
        with pytest.raises(ExperimentError) as exc:
            host.serve_line(json.dumps(request2.to_dict()))
        assert exc.value.code == ErrorCode.BUDGET_EXHAUSTED
        # over the stream the same case degrades to an error response line
        reader = io.StringIO(json.dumps(request2.to_dict()) + "\n")
        writer = io.StringIO()
        assert host.serve_stream(reader, writer) == 1
        response = json.loads(writer.getvalue())
        assert response["status"] == "error"
        assert response["error_code"] == ErrorCode.BUDGET_EXHAUSTED.value


CHILD_SCRIPT = """
import json, sys
sys.path.insert(0, {repo_root!r})
from tools.reuse.ipc import child_emit_request
from tools.reuse.models import ProviderCallRequest
req = ProviderCallRequest(call_id={call_id!r}, provider="fake", model="m", payload={{"n": 1}})
resp = child_emit_request(req)
json.dump(resp, open({out!r}, "w"))
"""


class TestSubprocessRoundTrip:
    def test_child_with_clean_env_gets_model_response(self, tmp_path: Path) -> None:
        """A producer subprocess with a whitelisted env completes a model call over IPC."""
        out_path = tmp_path / "child-response.json"
        script = tmp_path / "child.py"
        script.write_text(
            CHILD_SCRIPT.format(repo_root=str(REPO_ROOT), call_id="ipc-1", out=str(out_path)),
            encoding="utf-8",
        )
        # Host side serves the child over pipes.
        host = _host()
        proc = subprocess.Popen(
            [sys.executable, str(script)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        assert proc.stdin is not None and proc.stdout is not None
        served = host.serve_stream(proc.stdout, proc.stdin)
        proc.wait(timeout=30)
        assert proc.returncode == 0
        assert served == 1
        response = json.loads(out_path.read_text(encoding="utf-8"))
        assert response["status"] == "done"

    def test_child_env_contains_no_secret_vars(self) -> None:
        from tools.reuse.sandbox import build_child_env, child_has_secret_markers

        env = build_child_env({"AC_REUSE_MEDIA_ROOT": "/tmp/x"})
        assert child_has_secret_markers(env) == []
        assert "HTTP_PROXY" not in env and "HTTPS_PROXY" not in env
