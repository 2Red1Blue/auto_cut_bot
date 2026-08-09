"""PlatformAPIClient -- HTTP client for the platform asset API.

Covers two endpoints:
  - ``batch-content-assets``: book metadata + subjects + relationships + tags
  - ``batch-episodes-info``: episode details + subtitles + shots

When ``base_url`` is None (default), all methods become no-ops -- the platform API
is an accelerator, not a required component. Every call is wrapped in try/except
to never crash the pipeline.

Dependencies (optional):
  - httpx -- ``pip install httpx`` (transitively available via litellm)
  Not installed -> client is always a no-op (same as base_url=None).
"""

from __future__ import annotations

import random
import time
from typing import Any

from autocut_core.logging import get_logger

logger = get_logger(__name__)

# -- Optional driver imports ---------------------------------------------------
try:
    import httpx

    _HAS_HTTPX = True
except ImportError:  # pragma: no cover -- optional dependency
    _HAS_HTTPX = False

# -- Constants -----------------------------------------------------------------

_DEFAULT_TIMEOUT = 30.0
_MAX_RETRIES = 3
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


# -- PlatformAPIClient ---------------------------------------------------------


class PlatformAPIClient:
    """HTTP client for the platform batch-content API.

    Usage::

        client = PlatformAPIClient(
            base_url="https://platform.example.com",
            api_key="sk-...",
        )
        if client.is_available:
            book = client.fetch_book_metadata(book_id="42000023011")
            episodes = client.fetch_episodes(book_id="42000023011")

    When ``base_url`` is None or ``httpx`` is not installed, all methods
    return sensible defaults (empty lists, empty dicts) without raising.
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._base_url = base_url.rstrip("/") if base_url else None
        self._api_key = api_key
        self._timeout = timeout
        self._client: httpx.Client | None = None

    # -- properties --------------------------------------------------------

    @property
    def is_available(self) -> bool:
        """True when a base_url is configured and httpx is installed."""
        return self._base_url is not None and _HAS_HTTPX

    # -- client lifecycle ---------------------------------------------------

    def _ensure_client(self) -> httpx.Client | None:
        """Lazy-init httpx.Client; returns None if unavailable."""
        if not self.is_available:
            return None
        if self._client is not None:
            return self._client
        try:
            headers: dict[str, str] = {
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"
            self._client = httpx.Client(
                base_url=self._base_url,
                headers=headers,
                timeout=self._timeout,
            )
            return self._client
        except Exception as exc:
            logger.warning("Platform API client init failed: %s", exc)
            self._client = None
            return None

    def close(self) -> None:
        """Close the underlying HTTP client, if any."""
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    # -- request helpers ----------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        json: dict[str, Any] | None = None,
        *,
        retries: int = _MAX_RETRIES,
    ) -> dict[str, Any] | list[Any]:
        """Send an HTTP request with exponential-backoff retry.

        Returns an empty dict/list on any failure.
        """
        client = self._ensure_client()
        if client is None:
            return {}

        last_error: Exception | None = None
        for attempt in range(retries + 1):
            retry_delay = min(30.0, (2**attempt) + random.random())
            try:
                response = client.request(method, path, json=json)
                status = response.status_code

                if status < 400:
                    data = response.json()
                    _update_rate_limit_state(response)
                    return data

                if status in _RETRYABLE_STATUSES and attempt < retries:
                    logger.warning(
                        "Platform API %s %s returned %d, retrying in %.1fs (attempt %d/%d)",
                        method,
                        path,
                        status,
                        retry_delay,
                        attempt + 1,
                        retries,
                    )
                    time.sleep(retry_delay)
                    continue

                # Non-retryable client error
                logger.warning(
                    "Platform API %s %s returned %d: %s",
                    method,
                    path,
                    status,
                    response.text[:500],
                )
                return {}

            except httpx.TimeoutException as exc:
                last_error = exc
                if attempt < retries:
                    logger.warning(
                        "Platform API %s %s timed out, retrying in %.1fs (attempt %d/%d)",
                        method,
                        path,
                        retry_delay,
                        attempt + 1,
                        retries,
                    )
                    time.sleep(retry_delay)
                    continue
                logger.warning("Platform API %s %s timed out after %d retries", method, path, retries)
                return {}

            except httpx.HTTPError as exc:
                last_error = exc
                if attempt < retries:
                    logger.warning(
                        "Platform API %s %s error: %s, retrying in %.1fs (attempt %d/%d)",
                        method,
                        path,
                        exc,
                        retry_delay,
                        attempt + 1,
                        retries,
                    )
                    time.sleep(retry_delay)
                    continue
                logger.warning("Platform API %s %s failed: %s", method, path, exc)
                return {}

        if last_error is not None:
            logger.warning("Platform API %s %s failed: %s", method, path, last_error)
        return {}

    # -- public methods -----------------------------------------------------

    def fetch_book_metadata(self, book_id: str) -> dict[str, Any]:
        """Fetch book metadata from the batch-content-assets endpoint.

        API: POST /assets/tmp/batch-content-assets
        Returns a dict with keys like:
          - bookId, bookName, overallSynopsis
          - characters (CharacterAsset list)
          - relationships
          - keywords, themeTags

        Returns an empty dict on failure or when unavailable.
        """
        if not self.is_available:
            logger.debug("Platform API not available, returning empty metadata for book_id=%s", book_id)
            return {}

        try:
            result = self._request(
                "POST",
                "/assets/tmp/batch-content-assets",
                json={"bookId": book_id},
            )
            if isinstance(result, list):
                # Defensive: some APIs return a list; take the first element
                return result[0] if result else {}
            return result if isinstance(result, dict) else {}
        except Exception as exc:
            logger.warning(
                "fetch_book_metadata(%s) failed: %s",
                book_id,
                exc,
            )
            return {}

    def fetch_episodes(self, book_id: str) -> list[dict[str, Any]]:
        """Fetch episode details from the batch-episodes-info endpoint.

        API: POST /assets/tmp/batch-episodes-info
        Returns a list of episode dicts, each with keys like:
          - chapterId, episodeId, summary
          - characters (CharacterInfo list)
          - subtitles (list[dict] with start_time, end_time, speaker, text, ...)
          - shots (list[dict] with start_time, end_time, scene, subjects, ...)

        Returns an empty list on failure or when unavailable.
        """
        if not self.is_available:
            logger.debug("Platform API not available, returning empty episodes for book_id=%s", book_id)
            return []

        try:
            result = self._request(
                "POST",
                "/assets/tmp/batch-episodes-info",
                json={"bookId": book_id},
            )
            if isinstance(result, dict):
                # Some APIs wrap episodes in a dict key
                episodes = result.get("episodes", result.get("data", []))
                if isinstance(episodes, list):
                    return episodes
                # Fallback: return the whole result as a single-element list
                return [result]
            if isinstance(result, list):
                return result
            return []
        except Exception as exc:
            logger.warning(
                "fetch_episodes(%s) failed: %s",
                book_id,
                exc,
            )
            return []


# -- Rate-limit state tracking ------------------------------------------------


# Module-level rate-limit state (shared across all PlatformAPIClient instances).
# Thread-safe enough for the single-threaded pipeline use case.
_RATE_LIMIT_REMAINING: int | None = None
_RATE_LIMIT_RESET: float | None = None  # epoch seconds


def _update_rate_limit_state(response: httpx.Response) -> None:
    """Update module-level rate-limit state from response headers."""
    global _RATE_LIMIT_REMAINING, _RATE_LIMIT_RESET

    try:
        remaining = response.headers.get("X-RateLimit-Remaining")
        if remaining is not None:
            _RATE_LIMIT_REMAINING = int(remaining)
    except (ValueError, TypeError):
        pass

    try:
        reset = response.headers.get("X-RateLimit-Reset")
        if reset is not None:
            _RATE_LIMIT_RESET = float(reset)
    except (ValueError, TypeError):
        pass


def get_rate_limit_state() -> dict[str, Any]:
    """Return current rate-limit state.

    Returns a dict with ``remaining`` (int or None) and ``reset_epoch`` (float or None).
    """
    return {
        "remaining": _RATE_LIMIT_REMAINING,
        "reset_epoch": _RATE_LIMIT_RESET,
    }