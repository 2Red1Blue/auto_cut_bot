"""autocut_core/platform/ -- Platform API client for batch content/episode retrieval.

Provides:
  - ``PlatformAPIClient``: HTTP client for the platform API endpoints
    (batch-content-assets, batch-episodes-info).
    When ``base_url`` is None, all methods degrade to no-op returning empty defaults.

Import: ``from autocut_core.platform import PlatformAPIClient``
"""

from __future__ import annotations

from autocut_core.platform.client import PlatformAPIClient

__all__ = ["PlatformAPIClient"]
