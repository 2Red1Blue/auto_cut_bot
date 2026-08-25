"""Secure snapshot and stable census for an explicitly authorized MP4 corpus."""

# pyright: reportMissingTypeStubs=false

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from autocut_kernel.media.types import canonical_sha256

from .models import (
    AuthorizedSeriesSourceRoot,
    SeriesCensusError,
    SeriesSource,
    SeriesSourceCensus,
)

_READ_SIZE = 1024 * 1024
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_OPEN_SUPPORTS_DIR_FD = os.open in os.supports_dir_fd
_DIR_FLAGS = os.O_RDONLY | _NOFOLLOW | _DIRECTORY | _CLOEXEC
_FILE_FLAGS = os.O_RDONLY | _NOFOLLOW | _CLOEXEC | _NONBLOCK


@dataclass(frozen=True, slots=True)
class SeriesSourceSnapshot:
    """Private immutable copy used by every probe/sample operation in one preparation."""

    root: Path
    census: SeriesSourceCensus


def census_series_sources(source_root: AuthorizedSeriesSourceRoot) -> SeriesSourceCensus:
    """Return a content census collected through the same secure snapshot boundary."""

    with snapshot_series_sources(source_root) as snapshot:
        return snapshot.census


@contextmanager
def snapshot_series_sources(
    source_root: AuthorizedSeriesSourceRoot,
) -> Generator[SeriesSourceSnapshot, None, None]:
    """Copy verified file descriptors into a private, short-lived snapshot."""

    root_fd = _open_absolute_directory(source_root.root)
    try:
        with tempfile.TemporaryDirectory(prefix="autocut-source-snapshot-") as temporary:
            snapshot_root = Path(temporary)
            copied = tuple(sorted(_copy_tree(root_fd, snapshot_root), key=lambda item: item[0]))
            if len(copied) != source_root.policy.expected_source_count:
                raise SeriesCensusError(
                    "authorized source root must contain exactly "
                    f"{source_root.policy.expected_source_count} MP4 files"
                )
            sources = tuple(
                SeriesSource(
                    relative_path,
                    "source-"
                    + canonical_sha256(
                        {
                            "content_sha256": content_sha256,
                            "relative_path": relative_path,
                        }
                    )[7:39],
                    content_sha256,
                    byte_size,
                )
                for relative_path, content_sha256, byte_size in copied
            )
            census = SeriesSourceCensus(
                source_root.policy,
                "all_or_nothing",
                sources,
            )
            _set_snapshot_directory_mode(snapshot_root, 0o500)
            try:
                yield SeriesSourceSnapshot(snapshot_root, census)
            finally:
                _set_snapshot_directory_mode(snapshot_root, 0o700)
    finally:
        os.close(root_fd)


def _open_absolute_directory(path: Path) -> int:
    if _NOFOLLOW == 0 or _DIRECTORY == 0 or not _OPEN_SUPPORTS_DIR_FD:
        raise SeriesCensusError("secure dirfd/openat source preparation is unavailable")
    current = os.open(path.anchor, _DIR_FLAGS)
    try:
        for component in path.parts[1:]:
            if component in ("", ".", ".."):
                raise SeriesCensusError("authorized source root contains an unsafe component")
            following = os.open(component, _DIR_FLAGS, dir_fd=current)
            os.close(current)
            current = following
        opened = os.fstat(current)
        if not stat.S_ISDIR(opened.st_mode):
            raise SeriesCensusError("authorized source root must be a directory")
        return current
    except OSError as error:
        os.close(current)
        raise SeriesCensusError(
            "authorized source root contains a missing or symbolic path component"
        ) from error
    except Exception:
        os.close(current)
        raise


def _copy_tree(
    directory_fd: int,
    snapshot_root: Path,
    relative_parts: tuple[str, ...] = (),
) -> tuple[tuple[str, str, int], ...]:
    try:
        with os.scandir(directory_fd) as iterator:
            entries = sorted(iterator, key=lambda item: item.name)
    except OSError as error:
        raise SeriesCensusError("authorized source directory could not be enumerated") from error
    copied: list[tuple[str, str, int]] = []
    for entry in entries:
        if entry.name in ("", ".", "..") or entry.is_symlink():
            raise SeriesCensusError("symbolic links are forbidden in the authorized source tree")
        try:
            observed = entry.stat(follow_symlinks=False)
        except OSError as error:
            raise SeriesCensusError("source tree entry changed during enumeration") from error
        if entry.is_dir(follow_symlinks=False):
            child_fd = _open_child_directory(directory_fd, entry.name, observed)
            try:
                child_parts = (*relative_parts, entry.name)
                snapshot_root.joinpath(*child_parts).mkdir(mode=0o700)
                copied.extend(_copy_tree(child_fd, snapshot_root, child_parts))
            finally:
                os.close(child_fd)
        elif Path(entry.name).suffix.lower() == ".mp4":
            if not stat.S_ISREG(observed.st_mode):
                raise SeriesCensusError("source MP4 must be a regular file")
            relative = (*relative_parts, entry.name)
            content_sha256, byte_size = _copy_regular_source(
                directory_fd,
                entry.name,
                snapshot_root.joinpath(*relative),
                observed,
            )
            copied.append((Path(*relative).as_posix(), content_sha256, byte_size))
    return tuple(copied)


def _open_child_directory(parent_fd: int, name: str, observed: os.stat_result) -> int:
    try:
        child_fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
        opened = os.fstat(child_fd)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (observed.st_dev, observed.st_ino)
        ):
            os.close(child_fd)
            raise SeriesCensusError("source directory changed after enumeration")
        return child_fd
    except OSError as error:
        raise SeriesCensusError("source directory changed or became a symbolic link") from error


def _copy_regular_source(
    parent_fd: int,
    name: str,
    destination: Path,
    observed: os.stat_result,
) -> tuple[str, int]:
    try:
        source_fd = os.open(name, _FILE_FLAGS, dir_fd=parent_fd)
    except OSError as error:
        raise SeriesCensusError("source MP4 changed or became a symbolic link") from error
    try:
        before = os.fstat(source_fd)
        if not stat.S_ISREG(before.st_mode):
            raise SeriesCensusError("source MP4 must be a regular file")
        if (
            (before.st_dev, before.st_ino) != (observed.st_dev, observed.st_ino)
            or before.st_mode != observed.st_mode
        ):
            raise SeriesCensusError("source MP4 changed after enumeration")
        if before.st_nlink != 1:
            raise SeriesCensusError("source MP4 hard links are forbidden")
        digest = hashlib.sha256()
        byte_size = 0
        with os.fdopen(os.dup(source_fd), "rb", closefd=True) as source:
            with destination.open("xb") as target:
                while chunk := source.read(_READ_SIZE):
                    digest.update(chunk)
                    target.write(chunk)
                    byte_size += len(chunk)
                target.flush()
                os.fsync(target.fileno())
        after = os.fstat(source_fd)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
            before.st_nlink,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
            after.st_nlink,
        )
        if identity_before != identity_after or byte_size != after.st_size or byte_size < 1:
            raise SeriesCensusError("source changed while snapshotting or is empty")
        destination.chmod(0o400)
        return "sha256:" + digest.hexdigest(), byte_size
    finally:
        os.close(source_fd)


def _set_snapshot_directory_mode(root: Path, mode: int) -> None:
    directories = [Path(directory) for directory, _, _ in os.walk(root)]
    for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        directory.chmod(mode)


__all__ = ["SeriesSourceSnapshot", "census_series_sources", "snapshot_series_sources"]
