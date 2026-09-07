"""Provider port and FakeProvider.

The port is the only place a real provider adapter plugs in. Credentials stay
in the host process; producers (in-process or subprocess) only ever see the
non-secret request/response contract. R0 ships FakeProvider only — real
provider adapters attach in later phases and must reuse this port.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Protocol

from tools.reuse.models import ProviderCallRequest, ProviderResult, Usage


class ProviderPort(Protocol):
    def call(self, request: ProviderCallRequest) -> ProviderResult: ...

    def resolve_unknown(self, call_id: str, provider_response_id: str) -> ProviderResult:
        """Poll a previously-unknown call to a terminal result without re-calling."""
        ...


class FakeProvider:
    """Deterministic protocol-acceptance provider.

    Raw output is a pure function of the request hash, so replay and live
    produce identical bytes. Fault injection supports the unknown-result
    recovery path required by the R0 contract.
    """

    def __init__(
        self,
        *,
        unknown_results: set[str] | None = None,
        input_tokens: int = 100,
        output_tokens: int = 200,
    ) -> None:
        # call_id -> the provider_response_id of a call that started but whose
        # terminal result must be polled via resolve_unknown.
        self._pending: dict[str, str] = {}
        self._unknown_call_ids: set[str] = unknown_results or set()
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens
        self.calls_made = 0
        self.last_call: ProviderCallRequest | None = None

    def call(self, request: ProviderCallRequest) -> ProviderResult:
        self.calls_made += 1
        self.last_call = request
        usage = Usage(input_tokens=self._input_tokens, output_tokens=self._output_tokens)
        if request.call_id in self._unknown_call_ids:
            response_id = f"resp-unknown-{request.request_hash}"
            self._pending[request.call_id] = response_id
            return ProviderResult(raw="", provider_response_id=response_id, usage=usage, status="unknown")
        raw = self._deterministic_raw(request)
        return ProviderResult(
            raw=raw,
            provider_response_id=f"resp-{request.request_hash[:12]}",
            usage=usage,
            status="done",
        )

    def resolve_unknown(self, call_id: str, provider_response_id: str) -> ProviderResult:
        """Rebuild the terminal result from the recorded response id.

        The FakeProvider raw output is a pure function of the request hash, and
        the response id embeds that hash, so recovery works across process
        restarts without a re-call."""
        prefix = "resp-unknown-"
        if not provider_response_id.startswith(prefix):
            raise ValueError(f"cannot resolve non-unknown response id {provider_response_id!r}")
        request_hash = provider_response_id[len(prefix):]
        raw = json.dumps(
            {"fake_output": f"result-for-{request_hash}", "request_hash": request_hash},
            sort_keys=True, ensure_ascii=False,
        )
        self._pending.pop(call_id, None)
        return ProviderResult(
            raw=raw,
            provider_response_id=provider_response_id,
            usage=Usage(input_tokens=self._input_tokens, output_tokens=self._output_tokens),
            status="done",
        )

    @staticmethod
    def _deterministic_raw(request: ProviderCallRequest) -> str:
        digest = hashlib.sha256(
            json.dumps(
                {"provider": request.provider, "model": request.model, "payload": request.payload},
                sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        return json.dumps(
            {"fake_output": f"result-for-{digest}", "request_hash": request.request_hash},
            sort_keys=True, ensure_ascii=False,
        )


class RecordingIndex:
    """Append-only request-hash -> raw response store shared across attempts.

    Live runs write exactly once per request hash; replay reads by exact hash
    and any miss is REPLAY_MISS, never a re-call."""

    def __init__(self, root: Any) -> None:
        from pathlib import Path

        self.root = Path(root)

    def path_for(self, request_hash: str) -> Any:
        return self.root / f"{request_hash}.json"

    def get(self, request_hash: str) -> dict[str, Any] | None:
        """Load a recording by exact hash. Corrupt or mismatched entries are
        treated as a miss (REPLAY_MISS), never as evidence and never a crash."""
        path = self.path_for(request_hash)
        if not path.is_file():
            return None
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if not isinstance(entry, dict) or entry.get("request_hash") != request_hash:
            return None
        return entry

    def put_if_absent(
        self, request_hash: str, raw: str, provider_response_id: str | None, usage: Usage
    ) -> bool:
        """Write the recording; return False if an entry already existed.

        Uses a unique tmp name plus fsync so concurrent runs and crashes can
        never leave a half-written recording at the final path."""
        path = self.path_for(request_hash)
        if path.exists():
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        import os
        import uuid

        tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "request_hash": request_hash,
                        "raw": raw,
                        "provider_response_id": provider_response_id,
                        "usage": {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens},
                        "recorded_utc": time.time(),
                    },
                    sort_keys=True, ensure_ascii=False,
                )
            )
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        return True
