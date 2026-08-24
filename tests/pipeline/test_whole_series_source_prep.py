from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Callable
from uuid import uuid4

import pytest
from autocut_kernel.store import BlobRef, Job

from auto_cut_bot.pipeline.source_prep import (
    AuthorizedSeriesSourceRoot,
    SeriesCensusError,
    WholeSeriesIdentityPreparer,
    census_series_sources,
)
from auto_cut_bot.pipeline.source_prep import census as census_module

_AUTHORIZED_REAL_ROOT = Path(
    "/Users/liuzx/Code/python/work_ai/ac_auto_cut/jobs_backup/when-lucifer-kneels/videos"
)


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _root(tmp_path: Path, *, count: int) -> AuthorizedSeriesSourceRoot:
    source_root = tmp_path / "videos"
    source_root.mkdir()
    for number in range(count, 0, -1):
        (source_root / f"episode-{number:02d}.mp4").write_bytes(f"episode-{number}".encode())
    return AuthorizedSeriesSourceRoot(
        root=source_root.resolve(),
        authorization_id="fixture-authority-v1",
        series_id="fixture-series",
        expected_source_count=count,
    )


def test_census_is_relative_path_sorted_and_content_bound(tmp_path: Path) -> None:
    authorized = _root(tmp_path, count=3)

    census = census_series_sources(authorized)

    assert [item.relative_path for item in census.sources] == [
        "episode-01.mp4",
        "episode-02.mp4",
        "episode-03.mp4",
    ]
    assert census.sources[0].content_sha256 == _digest(b"episode-1")
    assert census.canonical_hash.startswith("sha256:")
    assert str(authorized.root) not in repr(census.to_mapping())


def test_census_hash_changes_when_source_bytes_change(tmp_path: Path) -> None:
    authorized = _root(tmp_path, count=1)
    before = census_series_sources(authorized)
    (authorized.root / "episode-01.mp4").write_bytes(b"mutated")

    after = census_series_sources(authorized)

    assert after.canonical_hash != before.canonical_hash


@pytest.mark.skipif(
    not _AUTHORIZED_REAL_ROOT.is_dir(),
    reason="authorized when-lucifer-kneels corpus is not mounted",
)
def test_authorized_real_corpus_has_stable_45_source_census() -> None:
    census = census_series_sources(
        AuthorizedSeriesSourceRoot(
            _AUTHORIZED_REAL_ROOT.resolve(),
            "when-lucifer-kneels-authorized-v1",
            "when-lucifer-kneels",
            45,
        )
    )

    assert len(census.sources) == 45
    assert census.sources[0].relative_path == "ep01.mp4"
    assert census.sources[-1].relative_path == "ep45.mp4"
    assert sum(item.byte_size for item in census.sources) == 586_394_631
    assert census.canonical_hash == (
        "sha256:0bd97c6224df57d6649431d40c23aba27299b4c227fef66fda27edc88caaa0b8"
    )


def test_census_fails_closed_for_count_mismatch_and_symlink(tmp_path: Path) -> None:
    authorized = _root(tmp_path, count=2)
    (authorized.root / "episode-02.mp4").unlink()
    with pytest.raises(SeriesCensusError, match="exactly 2"):
        census_series_sources(authorized)

    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"outside")
    (authorized.root / "episode-02.mp4").symlink_to(outside)
    with pytest.raises(SeriesCensusError, match="symbolic links"):
        census_series_sources(authorized)


def test_census_rejects_symlinked_ancestor_and_hardlinked_source(tmp_path: Path) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    videos = real_parent / "videos"
    videos.mkdir()
    (videos / "episode.mp4").write_bytes(b"episode")
    alias = tmp_path / "alias"
    alias.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(SeriesCensusError, match="symbolic path component"):
        census_series_sources(
            AuthorizedSeriesSourceRoot(alias / "videos", "authority", "series", 1)
        )

    alias.unlink()
    outside = tmp_path / "outside.mp4"
    os.link(videos / "episode.mp4", outside)
    with pytest.raises(SeriesCensusError, match="hard links"):
        census_series_sources(
            AuthorizedSeriesSourceRoot(videos.resolve(), "authority", "series", 1)
        )


def test_census_rejects_mp4_fifo_without_blocking_for_a_writer(tmp_path: Path) -> None:
    videos = tmp_path / "videos"
    videos.mkdir()
    os.mkfifo(videos / "blocked.mp4")
    authorized = AuthorizedSeriesSourceRoot(videos.resolve(), "authority", "series", 1)

    started = time.monotonic()
    with pytest.raises(SeriesCensusError, match="regular file"):
        census_series_sources(authorized)

    assert time.monotonic() - started < 1.0


def test_census_rejects_regular_to_fifo_swap_after_stat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorized = _root(tmp_path, count=1)
    source = authorized.root / "episode-01.mp4"
    original_open = os.open
    swapped = False

    def swapping_open(path: str | bytes, flags: int, *args: object, **kwargs: object) -> int:
        nonlocal swapped
        if (
            not swapped
            and path == "episode-01.mp4"
            and kwargs.get("dir_fd") is not None
            and not flags & getattr(os, "O_DIRECTORY", 0)
        ):
            swapped = True
            source.unlink()
            os.mkfifo(source)
        return original_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(census_module.os, "open", swapping_open)

    with pytest.raises(SeriesCensusError, match="regular file|changed after enumeration"):
        census_series_sources(authorized)
    assert swapped


def test_nested_census_is_globally_relative_path_sorted(tmp_path: Path) -> None:
    videos = tmp_path / "videos"
    (videos / "a").mkdir(parents=True)
    (videos / "a.mp4").write_bytes(b"root-a")
    (videos / "a" / "z.mp4").write_bytes(b"nested-z")

    census = census_series_sources(
        AuthorizedSeriesSourceRoot(videos.resolve(), "authority", "series", 2)
    )

    assert [item.relative_path for item in census.sources] == ["a.mp4", "a/z.mp4"]


def test_authorized_root_must_be_explicit_absolute_directory(tmp_path: Path) -> None:
    with pytest.raises(SeriesCensusError, match="absolute"):
        AuthorizedSeriesSourceRoot(Path("videos"), "authority", "series", 1)


class _BlobStore:
    def __init__(self) -> None:
        self.contents: dict[str, bytes] = {}

    def put_immutable_blob(
        self,
        _job: Job,
        *,
        content: bytes,
        content_hash: str,
        media_type: str,
    ) -> BlobRef:
        assert _digest(content) == content_hash
        self.contents[content_hash] = content
        return BlobRef(uuid4(), content_hash, len(content), media_type)


class _Built:
    def __init__(self, source_id: str, source_hash: str) -> None:
        self.manifest = _Manifest(source_id, source_hash)


class _Manifest:
    def __init__(self, source_id: str, source_hash: str) -> None:
        self.source_id = source_id
        self.source_sha256 = source_hash


class _IdentityBuilder:
    def __init__(self, *, between_reads: Callable[[], None] | None = None) -> None:
        self.calls: list[tuple[Path, str]] = []
        self.snapshot_modes: list[tuple[int, int]] = []
        self.between_reads = between_reads

    def build(self, *, store: object, job: Job, source_path: Path, source_id: str) -> _Built:
        del store, job
        self.calls.append((source_path, source_id))
        self.snapshot_modes.append(
            (source_path.stat().st_mode & 0o777, source_path.parent.stat().st_mode & 0o777)
        )
        before = source_path.read_bytes()
        if self.between_reads is not None:
            self.between_reads()
        after = source_path.read_bytes()
        assert after == before
        return _Built(source_id, _digest(after))


def test_whole_series_preparer_uses_private_snapshot_and_cleans_it(
    tmp_path: Path,
) -> None:
    authorized = _root(tmp_path, count=2)
    builder = _IdentityBuilder()
    preparer = WholeSeriesIdentityPreparer(builder=builder)  # type: ignore[arg-type]

    prepared = preparer.prepare(store=_BlobStore(), job=Job("job", "test"), source_root=authorized)

    assert prepared.census.canonical_hash == census_series_sources(authorized).canonical_hash
    assert len(prepared.windows) == len(prepared.census.sources)
    assert [source_id for _, source_id in builder.calls] == [
        item.source_id for item in prepared.census.sources
    ]
    assert [path.name for path, _ in builder.calls] == ["episode-01.mp4", "episode-02.mp4"]
    assert all(authorized.root not in path.parents for path, _ in builder.calls)
    assert builder.snapshot_modes == [(0o400, 0o500), (0o400, 0o500)]
    assert all(not path.exists() for path, _ in builder.calls)


def test_probe_sample_reads_ignore_authorized_file_replace_and_restore(tmp_path: Path) -> None:
    authorized = _root(tmp_path, count=1)
    source = authorized.root / "episode-01.mp4"
    saved = authorized.root / "saved-original"

    def replace_and_restore() -> None:
        source.rename(saved)
        source.write_bytes(b"attacker replacement")
        source.unlink()
        saved.rename(source)

    builder = _IdentityBuilder(between_reads=replace_and_restore)
    prepared = WholeSeriesIdentityPreparer(builder=builder).prepare(  # type: ignore[arg-type]
        store=_BlobStore(),
        job=Job("replace-restore", "test"),
        source_root=authorized,
    )

    assert prepared.census.sources[0].content_sha256 == _digest(b"episode-1")
    assert source.read_bytes() == b"episode-1"


def test_probe_sample_reads_ignore_authorized_ancestor_swap(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    videos = parent / "videos"
    videos.mkdir(parents=True)
    source = videos / "episode.mp4"
    source.write_bytes(b"original")
    authorized = AuthorizedSeriesSourceRoot(videos.resolve(), "authority", "series", 1)
    saved_parent = tmp_path / "saved-parent"
    attacker_parent = tmp_path / "attacker-parent"
    (attacker_parent / "videos").mkdir(parents=True)
    (attacker_parent / "videos" / "episode.mp4").write_bytes(b"attacker")

    def swap_and_restore() -> None:
        parent.rename(saved_parent)
        parent.symlink_to(attacker_parent, target_is_directory=True)
        parent.unlink()
        saved_parent.rename(parent)

    builder = _IdentityBuilder(between_reads=swap_and_restore)
    prepared = WholeSeriesIdentityPreparer(builder=builder).prepare(  # type: ignore[arg-type]
        store=_BlobStore(),
        job=Job("ancestor-swap", "test"),
        source_root=authorized,
    )

    assert prepared.census.sources[0].content_sha256 == _digest(b"original")
    assert source.read_bytes() == b"original"
