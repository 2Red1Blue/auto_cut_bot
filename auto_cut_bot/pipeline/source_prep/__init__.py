"""Authorized whole-series source census and identity window preparation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .census import SeriesSourceSnapshot, census_series_sources, snapshot_series_sources
from .command import (
    IdentitySourceWindowBuilder,
    PersistedPreparedSources,
    PreparedSeriesSources,
    PreparedSourceEpisode,
    PrepareWholeSeriesSourcesCommand,
    PrepareWholeSeriesSourcesRequest,
    PrepareWholeSeriesSourcesResult,
    SourceManifestDecodeError,
    SourcePrepStore,
    read_persisted_prepared_sources,
    read_persisted_prepared_sources_bundle,
)
from .models import (
    AuthorizedSeriesSourceRoot,
    SeriesCensusError,
    SeriesSource,
    SeriesSourceCensus,
    SourceOperationPolicy,
    SourceOperationPurpose,
    SourcePurposeDeniedError,
)
from .probe import (
    DECODED_AUDIO_BOUNDARY_GENERATION_POLICY_SHA256,
    IDENTITY_FRAME_GENERATION_POLICY_SHA256,
    FFprobeSourceMediaPort,
    SourceMediaProbe,
)
from .reuse import (
    BindWholeSeriesSourcesCommand,
    BindWholeSeriesSourcesRequest,
    BindWholeSeriesSourcesResult,
    SourceReuseStore,
)

if TYPE_CHECKING:
    from .identity import PreparedWholeSeries, WholeSeriesIdentityPreparer


def __getattr__(name: str) -> object:
    if name in {"PreparedWholeSeries", "WholeSeriesIdentityPreparer"}:
        from .identity import PreparedWholeSeries, WholeSeriesIdentityPreparer

        return {
            "PreparedWholeSeries": PreparedWholeSeries,
            "WholeSeriesIdentityPreparer": WholeSeriesIdentityPreparer,
        }[name]
    raise AttributeError(name)


__all__ = [
    "AuthorizedSeriesSourceRoot",
    "FFprobeSourceMediaPort",
    "DECODED_AUDIO_BOUNDARY_GENERATION_POLICY_SHA256",
    "IDENTITY_FRAME_GENERATION_POLICY_SHA256",
    "IdentitySourceWindowBuilder",
    "PrepareWholeSeriesSourcesCommand",
    "PrepareWholeSeriesSourcesRequest",
    "PrepareWholeSeriesSourcesResult",
    "PreparedWholeSeries",
    "PreparedSeriesSources",
    "PreparedSourceEpisode",
    "PersistedPreparedSources",
    "SeriesCensusError",
    "SeriesSource",
    "SeriesSourceCensus",
    "SeriesSourceSnapshot",
    "SourceMediaProbe",
    "SourceManifestDecodeError",
    "SourceOperationPolicy",
    "SourceOperationPurpose",
    "SourcePrepStore",
    "SourcePurposeDeniedError",
    "WholeSeriesIdentityPreparer",
    "census_series_sources",
    "snapshot_series_sources",
    "read_persisted_prepared_sources",
    "read_persisted_prepared_sources_bundle",
    "BindWholeSeriesSourcesCommand",
    "BindWholeSeriesSourcesRequest",
    "BindWholeSeriesSourcesResult",
    "SourceReuseStore",
]
