#!/usr/bin/env python3
"""One bounded shadow-bootstrap observation call against the running HTTP route.

This runner never retries and never prints a secret.  It performs exactly one
authenticated POST, prints the bounded outcome projection, and leaves an unknown
transport outcome non-terminal (HTTP 202 with ``state`` carried through).

Usage:
    export AUTO_CUT_BOT_PIPELINE_API_KEY=...
    python scripts/run_shadow_bootstrap_observation.py \
        --source-run-id <run-id> --episode-index 0
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from uuid import uuid4

DEFAULT_BASE_URL = "http://127.0.0.1:18769"
ROUTE_PATH = "/v1/pipeline/shadow-bootstrap-observation"
API_KEY_ENV = "AUTO_CUT_BOT_PIPELINE_API_KEY"
MAX_RESPONSE_BYTES = 8192


def _api_key(explicit_file: str | None) -> str:
    if explicit_file is not None:
        return Path(explicit_file).read_text(encoding="utf-8").strip()
    value = os.environ.get(API_KEY_ENV, "").strip()
    if not value:
        print(
            json.dumps(
                {
                    "status": "error",
                    "error_code": "API_KEY_NOT_CONFIGURED",
                    "detail": f"set {API_KEY_ENV} or pass --api-key-file",
                },
                ensure_ascii=False,
            )
        )
        raise SystemExit(2)
    return value


def _post(base_url: str, api_key: str, payload: dict[str, object], timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(
        base_url.rstrip("/") + ROUTE_PATH,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "Idempotency-Key": f"shadow-bootstrap-{uuid4()}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return int(response.status), response.read(MAX_RESPONSE_BYTES)
    except urllib.error.HTTPError as error:
        return int(error.code), error.read(MAX_RESPONSE_BYTES)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run-id", required=True)
    parser.add_argument("--episode-index", required=True, type=int)
    parser.add_argument("--base-url", default=os.environ.get("AUTO_CUT_BOT_PIPELINE_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--api-key-file", default=None)
    parser.add_argument("--timeout-seconds", type=float, default=600.0)
    args = parser.parse_args(argv)

    if args.episode_index < 0:
        print(json.dumps({"status": "error", "error_code": "EPISODE_INDEX_INVALID"}))
        return 2

    api_key = _api_key(args.api_key_file)
    status, raw = _post(
        args.base_url,
        api_key,
        {"source_run_id": args.source_run_id, "episode_index": args.episode_index},
        args.timeout_seconds,
    )
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        decoded = {"raw_sha256_length": len(raw)}

    if status != 202:
        print(
            json.dumps(
                {"status": "error", "http_status": status, "body": _bounded(decoded)},
                ensure_ascii=False,
            )
        )
        return 3

    state = decoded.get("state") if isinstance(decoded, dict) else None
    print(
        json.dumps(
            {
                "status": "accepted",
                "http_status": status,
                "state": state,
                "receipt_id": decoded.get("receipt_id") if isinstance(decoded, dict) else None,
                "artifact_set_id": decoded.get("artifact_set_id") if isinstance(decoded, dict) else None,
                "replayed": decoded.get("replayed") if isinstance(decoded, dict) else None,
                "terminal": state not in (None, "running", "pending"),
            },
            ensure_ascii=False,
        )
    )
    return 0


def _bounded(value: object) -> object:
    rendered = json.dumps(value, ensure_ascii=False, default=str)
    if len(rendered) <= MAX_RESPONSE_BYTES:
        return value
    return {"truncated": True, "head": rendered[:MAX_RESPONSE_BYTES]}


if __name__ == "__main__":
    raise SystemExit(main())
