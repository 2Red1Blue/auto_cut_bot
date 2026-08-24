"""Authorized whole-series source census and identity window preparation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .census import SeriesSourceSnapshot, census_series_sources, snapshot_series_sources
from .command import (
    IdentitySourceWindowBuilder,
    PreparedSeriesSources,
    PreparedSourceEpisode,
    PrepareWholeSeriesSourcesCommand,
    PrepareWholeSeriesSourcesRequest,
    PrepareWholeSeriesSourcesResult,
    SourceManifestDecodeError,
    SourcePrepStore,
    read_persisted_prepared_sources,
)
from .models import (
    AuthorizedSeriesSourceRoot,
    SeriesCensusError,
    SeriesSource,
    SeriesSourceCensus,
)
from .probe import FFprobeSourceMediaPort, SourceMediaProbe

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
    "IdentitySourceWindowBuilder",
    "PrepareWholeSeriesSourcesCommand",
    "PrepareWholeSeriesSourcesRequest",
    "PrepareWholeSeriesSourcesResult",
    "PreparedWholeSeries",
    "PreparedSeriesSources",
    "PreparedSourceEpisode",
    "SeriesCensusError",
    "SeriesSource",
    "SeriesSourceCensus",
    "SeriesSourceSnapshot",
    "SourceMediaProbe",
    "SourceManifestDecodeError",
    "SourcePrepStore",
    "WholeSeriesIdentityPreparer",
    "census_series_sources",
    "snapshot_series_sources",
    "read_persisted_prepared_sources",
]
