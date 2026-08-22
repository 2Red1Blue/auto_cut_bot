"""Canonical JSON encoding used for reproducible contract compiler evidence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from .errors import CanonicalizationError

_JCS_SAFE_INTEGER = 2**53 - 1


def canonical_json_bytes(value: Any) -> bytes:
    """Return deterministic UTF-8 JSON bytes for the supported JSON subset.

    Floats are deliberately rejected.  Contract time and numeric representations
    are specified by later source packs; accepting a Python float here would make
    the foundation silently choose a precision and serialization policy for them.
    """

    _validate_json_value(value, path="$", seen=set())
    try:
        return json.dumps(
            _jcs_ordered(value),
            ensure_ascii=False,
            sort_keys=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:  # Defensive: validation is authoritative.
        raise CanonicalizationError(f"cannot encode canonical JSON: {error}") from error


def canonical_json_hash(value: Any) -> str:
    """Return a sha256-prefixed digest of :func:`canonical_json_bytes`."""

    return sha256_bytes(canonical_json_bytes(value))


def sha256_bytes(value: bytes) -> str:
    """Return a consistently formatted SHA-256 digest for arbitrary bytes."""

    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def load_canonical_json_bytes(raw: bytes, *, origin: str) -> tuple[Any, bytes]:
    """Decode strict UTF-8 JSON and return the decoded value plus canonical bytes."""

    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise CanonicalizationError(f"{origin}: source must be UTF-8 JSON") from error

    try:
        value = json.loads(
            text,
            parse_constant=_reject_non_json_constant,
            object_pairs_hook=_reject_duplicate_object_keys,
        )
    except (json.JSONDecodeError, ValueError) as error:
        raise CanonicalizationError(f"{origin}: invalid JSON source: {error}") from error
    return value, canonical_json_bytes(value)


def _reject_non_json_constant(value: str) -> None:
    raise ValueError(f"non-JSON numeric constant {value!r} is forbidden")


def _validate_json_value(value: Any, *, path: str, seen: set[int]) -> None:
    if value is None or type(value) is bool:  # noqa: E721 - reject bool-as-int ambiguity.
        return
    if type(value) is str:  # noqa: E721 - reject string subclasses and invalid Unicode.
        _validate_json_string(value, path=path)
        return
    if type(value) is int:  # noqa: E721 - Python integers must remain exact JCS/ECMAScript values.
        if not -_JCS_SAFE_INTEGER <= value <= _JCS_SAFE_INTEGER:
            raise CanonicalizationError(
                f"{path}: integer is outside the exact JCS/ECMAScript safe range"
            )
        return
    if type(value) is float:  # noqa: E721 - floats are intentionally unsupported.
        raise CanonicalizationError(f"{path}: float values are forbidden in compiler sources")

    if isinstance(value, Mapping):
        value_id = id(value)
        if value_id in seen:
            raise CanonicalizationError(f"{path}: cyclic values cannot be canonical JSON")
        seen.add(value_id)
        try:
            for key, child in value.items():
                if type(key) is not str:  # noqa: E721 - JSON object keys must be actual strings.
                    raise CanonicalizationError(f"{path}: JSON object keys must be strings")
                _validate_json_string(key, path=f"{path}.<key>")
                _validate_json_value(child, path=f"{path}.{key}", seen=seen)
        finally:
            seen.remove(value_id)
        return

    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        value_id = id(value)
        if value_id in seen:
            raise CanonicalizationError(f"{path}: cyclic values cannot be canonical JSON")
        seen.add(value_id)
        try:
            for index, child in enumerate(value):
                _validate_json_value(child, path=f"{path}[{index}]", seen=seen)
        finally:
            seen.remove(value_id)
        return

    raise CanonicalizationError(f"{path}: unsupported canonical JSON value {type(value).__name__}")


def _jcs_ordered(value: Any) -> Any:
    """Return an equivalent JSON value whose object keys follow JCS UTF-16 order.

    The foundation deliberately supports the exact JSON subset accepted by
    :func:`_validate_json_value`: finite safe integers, strings, booleans,
    null, arrays and objects. Decimal/tick values are represented as strings
    by later source packs, so accepting binary floats here would not be a
    harmless convenience.
    """

    if isinstance(value, Mapping):
        return {key: _jcs_ordered(value[key]) for key in sorted(value, key=_utf16_sort_key)}
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return [_jcs_ordered(item) for item in value]
    return value


def _utf16_sort_key(value: str) -> bytes:
    """Return the UTF-16BE code-unit order mandated by RFC 8785/JCS."""

    return value.encode("utf-16be", errors="strict")


def _validate_json_string(value: str, *, path: str) -> None:
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise CanonicalizationError(f"{path}: invalid Unicode string") from error


def _reject_duplicate_object_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build one JSON object while rejecting source ambiguity at every depth."""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result
