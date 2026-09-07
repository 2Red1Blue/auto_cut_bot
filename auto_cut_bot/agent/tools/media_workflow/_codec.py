"""Canonical JSON codec for media workflow DTOs (closed, duplicate-key safe)."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate JSON key: {key!r}")
        seen[key] = value
    return seen


def load_json_strict(text: str) -> Any:
    return json.loads(text, object_pairs_hook=_no_duplicate_keys)


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def json_sha256(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()
