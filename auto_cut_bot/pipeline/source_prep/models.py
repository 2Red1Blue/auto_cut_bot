"""Closed contracts for authorized whole-series source preparation."""

# pyright: reportMissingTypeStubs=false

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from autocut_kernel.media.types import canonical_sha256, sha256_prefixed


class SeriesCensusError(ValueError):
    """The authorized source corpus cannot produce consumable evidence."""

    code = "SERIES_SOURCE_CENSUS_DENIED"


@dataclass(frozen=True, slots=True)
class AuthorizedSeriesSourceRoot:
    """An explicitly authorized, canonical local source boundary."""

    root: Path
    authorization_id: str
    series_id: str
    expected_source_count: int

    def __post_init__(self) -> None:
        root = Path(self.root)
        if not root.is_absolute():
            raise SeriesCensusError("authorized source root must be absolute")
        if not self.authorization_id.strip() or not self.series_id.strip():
            raise SeriesCensusError("authorization_id and series_id must be non-empty")
        if type(self.expected_source_count) is not int or self.expected_source_count < 1:  # noqa: E721
            raise SeriesCensusError("expected_source_count must be a positive integer")
        object.__setattr__(self, "root", root)


@dataclass(frozen=True, slots=True)
class SeriesSource:
    """One content-addressed source with no absolute locator."""

    relative_path: str
    source_id: str
    content_sha256: str
    byte_size: int

    def __post_init__(self) -> None:
        if not self.relative_path or self.relative_path.startswith(("/", "../")):
            raise SeriesCensusError("source relative_path must stay within the authorized root")
        if not self.source_id.strip():
            raise SeriesCensusError("source_id must be non-empty")
        sha256_prefixed(self.content_sha256, "series_source.content_sha256")
        if type(self.byte_size) is not int or self.byte_size < 1:  # noqa: E721
            raise SeriesCensusError("series source byte_size must be positive")

    def to_mapping(self) -> dict[str, object]:
        return {
            "byte_size": self.byte_size,
            "content_sha256": self.content_sha256,
            "relative_path": self.relative_path,
            "source_id": self.source_id,
        }


@dataclass(frozen=True, slots=True)
class SeriesSourceCensus:
    authorization_id: str
    series_id: str
    completion_policy: str
    sources: tuple[SeriesSource, ...]

    def __post_init__(self) -> None:
        sources = tuple(self.sources)
        if self.completion_policy != "all_or_nothing":
            raise SeriesCensusError("whole-series completion policy must be all_or_nothing")
        paths = tuple(item.relative_path for item in sources)
        if not sources or paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            raise SeriesCensusError("series sources must be non-empty, sorted, and unique")
        object.__setattr__(self, "sources", sources)

    def to_mapping(self) -> dict[str, object]:
        return {
            "authorization_id": self.authorization_id,
            "completion_policy": self.completion_policy,
            "series_id": self.series_id,
            "sources": [item.to_mapping() for item in self.sources],
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


__all__ = [
    "AuthorizedSeriesSourceRoot",
    "SeriesCensusError",
    "SeriesSource",
    "SeriesSourceCensus",
]
