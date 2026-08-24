"""All-or-nothing whole-series identity window preparation."""

# pyright: reportMissingTypeStubs=false

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from autocut_kernel.store import Job

from auto_cut_bot.pipeline.vlm.identity_window import (
    BlobWriter,
    IdentityProxyWindow,
    IdentityProxyWindowBuilder,
)

from .census import snapshot_series_sources
from .models import AuthorizedSeriesSourceRoot, SeriesCensusError, SeriesSourceCensus


class IdentityWindowBuilder(Protocol):
    def build(
        self,
        *,
        store: BlobWriter,
        job: Job,
        source_path: Path,
        source_id: str,
    ) -> IdentityProxyWindow: ...


@dataclass(frozen=True, slots=True)
class PreparedWholeSeries:
    census: SeriesSourceCensus
    windows: tuple[IdentityProxyWindow, ...]


class WholeSeriesIdentityPreparer:
    """Build consumable identity windows only when the complete census remains unchanged."""

    def __init__(self, *, builder: IdentityWindowBuilder | None = None) -> None:
        self._builder = builder or IdentityProxyWindowBuilder()

    def prepare(
        self,
        *,
        store: BlobWriter,
        job: Job,
        source_root: AuthorizedSeriesSourceRoot,
    ) -> PreparedWholeSeries:
        with snapshot_series_sources(source_root) as snapshot:
            windows: list[IdentityProxyWindow] = []
            for source in snapshot.census.sources:
                window = self._builder.build(
                    store=store,
                    job=job,
                    source_path=snapshot.root / source.relative_path,
                    source_id=source.source_id,
                )
                if (
                    window.manifest.source_id != source.source_id
                    or window.manifest.source_sha256 != source.content_sha256
                ):
                    raise SeriesCensusError(
                        "identity window does not match its immutable source snapshot"
                    )
                windows.append(window)
            return PreparedWholeSeries(snapshot.census, tuple(windows))


__all__ = ["PreparedWholeSeries", "WholeSeriesIdentityPreparer"]
