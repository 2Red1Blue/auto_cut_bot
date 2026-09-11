"""Fail-closed composition root for the shadow-bootstrap observation entry.

The bootstrap entry is deliberately not a Pipeline stage.  It is composed only
from the configured local PostgreSQL store, the fixed loopback FunASR bootstrap
route, and the installed materialization limits.  No client request, catalog,
or VLM observation input can select any of them.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from typing import cast

import psycopg
from autocut_kernel.store import PostgresRuntimeStore
from autocut_kernel.store.postgres import DbConnection

from auto_cut_bot.pipeline.media_preflight.models import LocalMediaPolicyError
from auto_cut_bot.pipeline.media_preflight.shadow_bootstrap_http import (
    ShadowBootstrapObservationHttpPort,
)

from .composition import (
    FUNASR_SHARED_TOKEN_ENV,
    PIPELINE_KERNEL_POSTGRES_DSN_ENV,
    PIPELINE_MEDIA_PREFLIGHT_MATERIALIZATION_LIMITS_ENV,
    PIPELINE_MEDIA_PREFLIGHT_STAGING_ROOT_ENV,
    PIPELINE_POSTGRES_DSN_ENV,
    PipelineRuntimeConfigurationError,
    _materialization_limits_from_json,
    _staging_root,
)
from .shadow_bootstrap_entry import (
    ShadowBootstrapEntryStore,
    ShadowBootstrapObservationEntryService,
)

SHADOW_BOOTSTRAP_OBSERVATION_ENDPOINT_ENV = (
    "AUTO_CUT_BOT_SHADOW_BOOTSTRAP_OBSERVATION_ENDPOINT"
)
SHADOW_BOOTSTRAP_OBSERVATION_MAX_RESPONSE_BYTES_ENV = (
    "AUTO_CUT_BOT_SHADOW_BOOTSTRAP_OBSERVATION_MAX_RESPONSE_BYTES"
)
SHADOW_BOOTSTRAP_OBSERVATION_TIMEOUT_SECONDS_ENV = (
    "AUTO_CUT_BOT_SHADOW_BOOTSTRAP_OBSERVATION_TIMEOUT_SECONDS"
)

# Matches the installed FunASR inference timeout; the route is one long upload.
DEFAULT_OBSERVATION_TIMEOUT_SECONDS = 180

_SHADOW_BOOTSTRAP_REQUIRED_ENVIRONMENT = (
    SHADOW_BOOTSTRAP_OBSERVATION_ENDPOINT_ENV,
    SHADOW_BOOTSTRAP_OBSERVATION_MAX_RESPONSE_BYTES_ENV,
    FUNASR_SHARED_TOKEN_ENV,
    PIPELINE_MEDIA_PREFLIGHT_MATERIALIZATION_LIMITS_ENV,
    PIPELINE_MEDIA_PREFLIGHT_STAGING_ROOT_ENV,
)
# Only these three select the bootstrap entry.  The DSN, shared token and
# materialization limits are shared with the ordinary media runtime, so they
# must never by themselves turn the bootstrap route on.
_SHADOW_BOOTSTRAP_ONLY_ENVIRONMENT = (
    SHADOW_BOOTSTRAP_OBSERVATION_ENDPOINT_ENV,
    SHADOW_BOOTSTRAP_OBSERVATION_MAX_RESPONSE_BYTES_ENV,
    SHADOW_BOOTSTRAP_OBSERVATION_TIMEOUT_SECONDS_ENV,
)


def compose_shadow_bootstrap_entry_from_environment(
    environ: Mapping[str, str] | None = None,
) -> ShadowBootstrapObservationEntryService | None:
    """Compose the bootstrap entry, rejecting every partial configuration.

    Returns ``None`` when no bootstrap-specific variable is configured, so the
    ordinary media/pipeline deployment keeps starting exactly as before.  The
    shared DSN, token and materialization limits never enable the route on
    their own.  A partial bootstrap configuration fails closed instead of
    degrading into a silently disabled route.
    """
    return _compose_shadow_bootstrap_entry(os.environ if environ is None else environ)


def _compose_shadow_bootstrap_entry(
    values: Mapping[str, str],
) -> ShadowBootstrapObservationEntryService | None:
    dsn = values.get(PIPELINE_KERNEL_POSTGRES_DSN_ENV, "").strip() or values.get(
        PIPELINE_POSTGRES_DSN_ENV, ""
    ).strip()
    if not any(values.get(name, "").strip() for name in _SHADOW_BOOTSTRAP_ONLY_ENVIRONMENT):
        return None
    missing = [
        name for name in _SHADOW_BOOTSTRAP_REQUIRED_ENVIRONMENT if not values.get(name, "").strip()
    ]
    if not dsn:
        missing.extend(
            (PIPELINE_KERNEL_POSTGRES_DSN_ENV, PIPELINE_POSTGRES_DSN_ENV),
        )
    if missing:
        raise PipelineRuntimeConfigurationError(
            "shadow bootstrap observation configuration is incomplete; missing: "
            + ", ".join(missing)
        )
    try:
        timeout_seconds = _positive_int(
            values.get(SHADOW_BOOTSTRAP_OBSERVATION_TIMEOUT_SECONDS_ENV, "").strip()
            or str(DEFAULT_OBSERVATION_TIMEOUT_SECONDS),
            SHADOW_BOOTSTRAP_OBSERVATION_TIMEOUT_SECONDS_ENV,
        )
        max_response_bytes = _positive_int(
            values[SHADOW_BOOTSTRAP_OBSERVATION_MAX_RESPONSE_BYTES_ENV].strip(),
            SHADOW_BOOTSTRAP_OBSERVATION_MAX_RESPONSE_BYTES_ENV,
        )
        port = ShadowBootstrapObservationHttpPort(
            endpoint_url=values[SHADOW_BOOTSTRAP_OBSERVATION_ENDPOINT_ENV].strip(),
            shared_token=values[FUNASR_SHARED_TOKEN_ENV].strip(),
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
        )
        staging_root = _staging_root(
            values[PIPELINE_MEDIA_PREFLIGHT_STAGING_ROOT_ENV].strip()
        )
        materialization_limits = _materialization_limits_from_json(
            values[PIPELINE_MEDIA_PREFLIGHT_MATERIALIZATION_LIMITS_ENV].strip()
        )
    except PipelineRuntimeConfigurationError:
        raise
    except (LocalMediaPolicyError, TypeError, ValueError) as error:
        raise PipelineRuntimeConfigurationError(
            "shadow bootstrap observation endpoint/limit configuration is invalid"
        ) from error

    factory = cast(Callable[[], DbConnection], lambda: psycopg.connect(dsn))
    store = PostgresRuntimeStore(
        factory, materialization_staging_root=staging_root
    )
    return ShadowBootstrapObservationEntryService(
        cast(ShadowBootstrapEntryStore, store),
        port,
        materialization_limits=materialization_limits,
        max_response_bytes=max_response_bytes,
    )


def _positive_int(raw: str, field_name: str) -> int:
    if not raw.isdecimal():
        raise PipelineRuntimeConfigurationError(f"{field_name} must be a decimal integer")
    value = int(raw)
    if value <= 0:
        raise PipelineRuntimeConfigurationError(f"{field_name} must be positive")
    return value


__all__ = (
    "DEFAULT_OBSERVATION_TIMEOUT_SECONDS",
    "SHADOW_BOOTSTRAP_OBSERVATION_ENDPOINT_ENV",
    "SHADOW_BOOTSTRAP_OBSERVATION_MAX_RESPONSE_BYTES_ENV",
    "SHADOW_BOOTSTRAP_OBSERVATION_TIMEOUT_SECONDS_ENV",
    "compose_shadow_bootstrap_entry_from_environment",
)
