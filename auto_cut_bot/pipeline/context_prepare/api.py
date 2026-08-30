"""HTTP boundary for the external narrative API.

The client returns raw JSON and a non-secret snapshot identity.  It has no
knowledge of VLM prompts, source files, clip timing, legacy database tables or
fallback context.  Callers persist the raw bytes before normalizing them.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import cast
from urllib.parse import urlsplit

import httpx
from autocut_kernel.context_pack import ExternalContextSnapshot

DEFAULT_ASSET_RESOURCE_PATH = "/assets/tmp/batch-content-assets"
DEFAULT_EPISODE_RESOURCE_PATH = "/assets/tmp/batch-episodes-info"


class ExternalNarrativeApiError(RuntimeError):
    """The external metadata service returned an unusable or unavailable result."""


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")


class _DuplicateJsonKeyError(ValueError):
    pass


def _closed_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKeyError(key)
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"unsupported JSON constant: {value}")


def _strict_json_body(raw: bytes, path: str) -> dict[str, object]:
    try:
        body = json.loads(
            raw.decode("utf-8", "strict"),
            object_pairs_hook=_closed_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ExternalNarrativeApiError(
            f"metadata API {path} returned non-canonical JSON"
        ) from error
    if type(body) is not dict:  # noqa: E721
        raise ExternalNarrativeApiError(
            f"metadata API {path} returned a non-object response"
        )
    return cast(dict[str, object], body)


def _origin(base_url: str) -> str:
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("metadata API base URL must be an origin without path, query, or fragment")
    return f"{parsed.scheme}://{parsed.netloc}"


@dataclass(frozen=True, slots=True)
class ExternalNarrativeApiConfig:
    base_url: str
    api_key: str
    credential_scope_id: str = "default"
    language: str = "ENGLISH"
    timeout_seconds: float = 30.0
    asset_resource_path: str = DEFAULT_ASSET_RESOURCE_PATH
    episode_resource_path: str = DEFAULT_EPISODE_RESOURCE_PATH

    def __post_init__(self) -> None:
        _origin(self.base_url)
        if type(self.api_key) is not str or not self.api_key.strip():  # noqa: E721
            raise ValueError("METADATA_API_KEY must be configured")
        for name in ("credential_scope_id", "language", "asset_resource_path", "episode_resource_path"):
            value = getattr(self, name)
            if type(value) is not str or not value.strip():  # noqa: E721
                raise ValueError(f"{name} must be non-empty")
        if not self.asset_resource_path.startswith("/") or not self.episode_resource_path.startswith("/"):
            raise ValueError("resource paths must be absolute API paths")
        if type(self.timeout_seconds) not in (int, float) or not 1 <= float(self.timeout_seconds) <= 120:  # noqa: E721
            raise ValueError("timeout_seconds must be between 1 and 120")

    @property
    def endpoint_origin(self) -> str:
        return _origin(self.base_url)


@dataclass(frozen=True, slots=True)
class FetchedExternalNarrativeContext:
    snapshot: ExternalContextSnapshot
    raw_payload: bytes
    asset_response: dict[str, object]
    episode_response: dict[str, object]

    def __post_init__(self) -> None:
        if type(self.snapshot) is not ExternalContextSnapshot:  # noqa: E721
            raise TypeError("snapshot must be an exact ExternalContextSnapshot")
        if type(self.raw_payload) is not bytes:  # noqa: E721
            raise TypeError("raw_payload must be exact bytes")
        if type(self.asset_response) is not dict or type(self.episode_response) is not dict:  # noqa: E721
            raise TypeError("external responses must be JSON objects")
        try:
            expected = _canonical_json_bytes({
                "asset_response": self.asset_response,
                "episode_response": self.episode_response,
            })
        except (TypeError, ValueError) as error:
            raise ExternalNarrativeApiError("external response is not canonical JSON") from error
        actual_hash = "sha256:" + hashlib.sha256(self.raw_payload).hexdigest()
        if actual_hash != self.snapshot.raw_payload_sha256 or expected != self.raw_payload:
            raise ExternalNarrativeApiError(
                "fetched responses do not match the immutable snapshot payload"
            )

    def debug_mapping(self) -> dict[str, object]:
        """Safe enough for local debug: never includes the configured API key."""
        return {
            "asset_response": self.asset_response,
            "episode_response": self.episode_response,
            "raw_payload_sha256": self.snapshot.raw_payload_sha256,
            "snapshot": self.snapshot.to_mapping(),
        }


class ExternalNarrativeApiClient:
    def __init__(self, config: ExternalNarrativeApiConfig) -> None:
        if type(config) is not ExternalNarrativeApiConfig:  # noqa: E721
            raise TypeError("config must be an exact ExternalNarrativeApiConfig")
        self._config = config

    def fetch(self, series_external_id: str) -> FetchedExternalNarrativeContext:
        if type(series_external_id) is not str or not series_external_id.strip():  # noqa: E721
            raise ValueError("series_external_id must be non-empty")
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self._config.api_key}"}
        with httpx.Client(base_url=self._config.endpoint_origin, headers=headers, timeout=self._config.timeout_seconds) as client:
            asset = self._post_json(
                client,
                self._config.asset_resource_path,
                {"bookIds": [series_external_id], "language": self._config.language},
            )
            episodes = self._post_json(
                client,
                self._config.episode_resource_path,
                {"bookIds": [series_external_id]},
            )
        payload = {"asset_response": asset, "episode_response": episodes}
        raw_payload = _canonical_json_bytes(payload)
        raw_hash = "sha256:" + hashlib.sha256(raw_payload).hexdigest()
        snapshot_id = "snapshot:" + hashlib.sha256(
            _canonical_json_bytes(
                {
                    "series_external_id": series_external_id,
                    "resource_paths": sorted((self._config.asset_resource_path, self._config.episode_resource_path)),
                    "raw_payload_sha256": raw_hash,
                }
            )
        ).hexdigest()
        return FetchedExternalNarrativeContext(
            snapshot=ExternalContextSnapshot(
                snapshot_id=snapshot_id,
                series_external_id=series_external_id,
                resource_paths=tuple(sorted((self._config.asset_resource_path, self._config.episode_resource_path))),
                endpoint_origin=self._config.endpoint_origin,
                credential_scope_id=self._config.credential_scope_id,
                raw_payload_sha256=raw_hash,
            ),
            raw_payload=raw_payload,
            asset_response=asset,
            episode_response=episodes,
        )

    @staticmethod
    def _post_json(client: httpx.Client, path: str, payload: dict[str, object]) -> dict[str, object]:
        try:
            response = client.post(path, json=payload)
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise ExternalNarrativeApiError(
                f"metadata API request failed for {path}"
            ) from error
        return _strict_json_body(response.content, path)
