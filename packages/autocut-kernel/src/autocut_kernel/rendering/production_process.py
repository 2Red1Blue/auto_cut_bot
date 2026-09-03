"""Shared shell-free process and executable-identity boundary for production media work.

The renderer uses the bounded compatibility entry point.  Full-file QC callers
use the streaming entry point so that a reducer can consume every byte without
retaining an unbounded process transcript in memory.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import signal
import stat
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol

from ..media.types import sha256_prefixed

FIXED_PROCESS_ENVIRONMENT: Final = {
    "AV_LOG_FORCE_NOCOLOR": "1",
    "LANG": "C",
    "LC_ALL": "C",
    "TZ": "UTC",
}
_READ_CHUNK_BYTES: Final = 64 * 1024
_TOOL_VERSION_MAX_BYTES: Final = 64 * 1024
_FILE_HASH_CHUNK_BYTES: Final = 1024 * 1024


class ProductionProcessError(RuntimeError):
    """Base failure for the shared production process boundary."""


class ProductionProcessTimeoutError(ProductionProcessError):
    """The process exceeded its explicit wall-clock deadline."""


class ProductionProcessSinkError(ProductionProcessError):
    """A streaming consumer failed while processing an output chunk."""


class ProductionProcessReaderError(ProductionProcessError):
    """A process output pipe could not be drained safely."""


class ProductionExecutableError(RuntimeError):
    """An executable could not be resolved, pinned, or reverified."""


class _HashAccumulator(Protocol):
    def update(self, data: bytes, /) -> None: ...

    def hexdigest(self) -> str: ...


@dataclass(frozen=True, slots=True)
class ProductionStreamResult:
    """Complete-stream identity plus a bounded diagnostic prefix."""

    byte_count: int
    sha256: str
    diagnostic_prefix: bytes
    diagnostic_limit_exceeded: bool

    def __post_init__(self) -> None:
        if type(self.byte_count) is not int or self.byte_count < 0:  # noqa: E721
            raise ValueError("production stream byte_count must be a non-negative integer")
        sha256_prefixed(self.sha256, "production stream sha256")
        if type(self.diagnostic_prefix) is not bytes:  # noqa: E721
            raise ValueError("production stream diagnostic_prefix must be bytes")
        if type(self.diagnostic_limit_exceeded) is not bool:  # noqa: E721
            raise ValueError("production stream diagnostic_limit_exceeded must be boolean")


@dataclass(frozen=True, slots=True)
class ProductionStreamingProcessResult:
    """Result of a streaming execution; no unbounded transcript is retained."""

    returncode: int
    stdout: ProductionStreamResult
    stderr: ProductionStreamResult
    progress: ProductionStreamResult | None = None

    def __post_init__(self) -> None:
        if type(self.returncode) is not int:  # noqa: E721
            raise ValueError("production process returncode must be an integer")
        if type(self.stdout) is not ProductionStreamResult:  # noqa: E721
            raise ValueError("production process stdout result is invalid")
        if type(self.stderr) is not ProductionStreamResult:  # noqa: E721
            raise ValueError("production process stderr result is invalid")
        if self.progress is not None and type(self.progress) is not ProductionStreamResult:  # noqa: E721
            raise ValueError("production process progress result is invalid")


@dataclass(frozen=True, slots=True)
class ProductionProcessResult:
    """Compatibility result retained for the existing production renderer API."""

    returncode: int
    stdout: bytes
    stderr: bytes
    stdout_limit_exceeded: bool = False
    stderr_limit_exceeded: bool = False
    stdout_byte_count: int | None = None
    stderr_byte_count: int | None = None
    stdout_sha256: str | None = None
    stderr_sha256: str | None = None

    def __post_init__(self) -> None:
        if type(self.returncode) is not int:  # noqa: E721
            raise ValueError("production process returncode must be an integer")
        if type(self.stdout) is not bytes or type(self.stderr) is not bytes:  # noqa: E721
            raise ValueError("production process output must be bytes")
        if (  # noqa: E721
            type(self.stdout_limit_exceeded) is not bool
            or type(self.stderr_limit_exceeded) is not bool
        ):
            raise ValueError("production process limit state must be boolean")
        for name, value, fallback in (
            ("stdout_byte_count", self.stdout_byte_count, len(self.stdout)),
            ("stderr_byte_count", self.stderr_byte_count, len(self.stderr)),
        ):
            if value is None:
                object.__setattr__(self, name, fallback)
            elif type(value) is not int or value < fallback:  # noqa: E721
                raise ValueError(f"production process {name} is invalid")
        for name, value, fallback in (
            ("stdout_sha256", self.stdout_sha256, _sha256_bytes(self.stdout)),
            ("stderr_sha256", self.stderr_sha256, _sha256_bytes(self.stderr)),
        ):
            if value is None:
                object.__setattr__(self, name, fallback)
            else:
                sha256_prefixed(value, f"production process {name}")


class ProductionProcessRunner(Protocol):
    def __call__(
        self,
        argv: tuple[str, ...],
        *,
        timeout_milliseconds: int,
        stdout_max_bytes: int,
        stderr_max_bytes: int,
        pass_fds: tuple[int, ...],
    ) -> ProductionProcessResult: ...


ProductionStreamSink = Callable[[bytes], None]


@dataclass(frozen=True, slots=True)
class ProductionProcessPipe:
    """A runner-owned pipe for a third output stream such as FFmpeg progress."""

    read_fd: int
    write_fd: int

    def __post_init__(self) -> None:
        if (
            type(self.read_fd) is not int
            or type(self.write_fd) is not int
            or self.read_fd < 0
            or self.write_fd < 0
            or self.read_fd == self.write_fd
        ):
            raise ValueError("production process pipe descriptors are invalid")


@dataclass(frozen=True, slots=True)
class PinnedExecutable:
    """Private exact executable bytes, copied into one attempt directory."""

    path: Path
    sha256: str
    byte_length: int

    def __post_init__(self) -> None:
        if not self.path.is_absolute():
            raise ValueError("pinned executable path must be absolute")
        sha256_prefixed(self.sha256, "pinned executable sha256")
        if type(self.byte_length) is not int or self.byte_length <= 0:  # noqa: E721
            raise ValueError("pinned executable byte_length must be positive")


@dataclass(frozen=True, slots=True)
class ProductionExecutableIdentity:
    """Portable identity shared by FFmpeg, FFprobe, and future media tools."""

    executable_sha256: str
    executable_byte_length: int
    version_output_sha256: str

    def __post_init__(self) -> None:
        sha256_prefixed(self.executable_sha256, "production executable_sha256")
        if type(self.executable_byte_length) is not int or self.executable_byte_length <= 0:  # noqa: E721
            raise ValueError("production executable_byte_length must be positive")
        sha256_prefixed(self.version_output_sha256, "production executable version_output_sha256")

    def to_mapping(self) -> dict[str, object]:
        return {
            "executable_byte_length": self.executable_byte_length,
            "executable_sha256": self.executable_sha256,
            "version_output_sha256": self.version_output_sha256,
        }


@dataclass(frozen=True, slots=True)
class ProductionExecutableVersion:
    """Bounded version probe identity without retaining unbounded tool output."""

    output_sha256: str
    stdout: bytes
    stderr: bytes

    def __post_init__(self) -> None:
        sha256_prefixed(self.output_sha256, "production executable version output_sha256")
        if type(self.stdout) is not bytes or type(self.stderr) is not bytes:  # noqa: E721
            raise ValueError("production executable version output must be bytes")


@dataclass(slots=True)
class _StreamAccumulator:
    diagnostic_max_bytes: int
    byte_count: int = 0
    _digest: _HashAccumulator = field(default_factory=hashlib.sha256)
    _prefix: bytearray = field(default_factory=bytearray)
    diagnostic_limit_exceeded: bool = False

    def add(self, chunk: bytes) -> None:
        self.byte_count += len(chunk)
        self._digest.update(chunk)
        remaining = self.diagnostic_max_bytes - len(self._prefix)
        if remaining > 0:
            self._prefix.extend(chunk[:remaining])
        if len(chunk) > max(remaining, 0):
            self.diagnostic_limit_exceeded = True

    def result(self) -> ProductionStreamResult:
        return ProductionStreamResult(
            self.byte_count,
            f"sha256:{self._digest.hexdigest()}",
            bytes(self._prefix),
            self.diagnostic_limit_exceeded,
        )


def create_process_pipe() -> ProductionProcessPipe:
    """Create a private extra output pipe; the runner closes both ends when done."""

    read_fd, write_fd = os.pipe()
    return ProductionProcessPipe(read_fd, write_fd)


def close_process_pipe(pipe: ProductionProcessPipe) -> None:
    """Close a pipe only before ownership transfers to a process runner.

    File descriptor numbers can be reused after a close, therefore calling this
    helper twice is not generally safe even though an already-closed descriptor
    commonly raises ``OSError``.  ``run_streaming_process`` owns and closes its
    pipe on every path once invoked.
    """

    for descriptor in (pipe.read_fd, pipe.write_fd):
        try:
            os.close(descriptor)
        except OSError:
            pass


def run_streaming_process(
    argv: tuple[str, ...],
    *,
    timeout_milliseconds: int,
    stdout_diagnostic_max_bytes: int,
    stderr_diagnostic_max_bytes: int,
    progress_diagnostic_max_bytes: int = 0,
    stdout_sink: ProductionStreamSink | None = None,
    stderr_sink: ProductionStreamSink | None = None,
    progress_sink: ProductionStreamSink | None = None,
    progress_pipe: ProductionProcessPipe | None = None,
    pass_fds: tuple[int, ...] = (),
    environment: Mapping[str, str] | None = None,
    terminate_on_diagnostic_limit: bool = False,
) -> ProductionStreamingProcessResult:
    """Run an argv vector while draining stdout, stderr, and optional progress in parallel.

    A sink receives immutable byte chunks.  Its exception, a timeout, or any
    outer ``BaseException`` terminates the whole session/process group before
    this function returns or reraises.  Prefix caps never limit hashing or byte
    accounting; callers get complete identities for every drained stream.
    """

    _validate_process_request(
        argv,
        timeout_milliseconds,
        stdout_diagnostic_max_bytes,
        stderr_diagnostic_max_bytes,
        progress_diagnostic_max_bytes,
        pass_fds,
        environment,
        progress_pipe,
    )
    inherited_fds = pass_fds
    if progress_pipe is not None:
        inherited_fds = (*pass_fds, progress_pipe.write_fd)
    effective_environment = _effective_environment(environment)
    capture_stdout = stdout_diagnostic_max_bytes > 0 or stdout_sink is not None
    capture_stderr = stderr_diagnostic_max_bytes > 0 or stderr_sink is not None
    process: subprocess.Popen[bytes] | None = None
    readers: list[threading.Thread] = []
    reader_failure: list[BaseException] = []
    failure_lock = threading.Lock()
    stdout = _StreamAccumulator(stdout_diagnostic_max_bytes)
    stderr = _StreamAccumulator(stderr_diagnostic_max_bytes)
    progress = _StreamAccumulator(progress_diagnostic_max_bytes) if progress_pipe else None

    def record_reader_failure(error: BaseException) -> None:
        with failure_lock:
            if not reader_failure:
                reader_failure.append(error)
        if process is not None:
            _kill_process_group(process)

    def read_descriptor(
        descriptor: int,
        accumulator: _StreamAccumulator,
        sink: ProductionStreamSink | None,
        *,
        close_stream: Callable[[], None],
    ) -> None:
        try:
            while chunk := os.read(descriptor, _READ_CHUNK_BYTES):
                accumulator.add(chunk)
                if terminate_on_diagnostic_limit and accumulator.diagnostic_limit_exceeded:
                    if process is not None:
                        _kill_process_group(process)
                if sink is not None:
                    sink(chunk)
        except BaseException as error:
            record_reader_failure(error)
        finally:
            try:
                close_stream()
            except OSError as error:
                record_reader_failure(error)

    try:
        process = subprocess.Popen(  # noqa: S603 - callers supply closed argv templates.
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
            stderr=subprocess.PIPE if capture_stderr else subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            pass_fds=inherited_fds,
            env=effective_environment,
        )
        if progress_pipe is not None:
            os.close(progress_pipe.write_fd)
        streams: list[
            tuple[int, _StreamAccumulator, ProductionStreamSink | None, Callable[[], None], str]
        ] = []
        if process.stdout is not None:
            streams.append(
                (process.stdout.fileno(), stdout, stdout_sink, process.stdout.close, "stdout")
            )
        if process.stderr is not None:
            streams.append(
                (process.stderr.fileno(), stderr, stderr_sink, process.stderr.close, "stderr")
            )
        if progress_pipe is not None and progress is not None:
            streams.append(
                (
                    progress_pipe.read_fd,
                    progress,
                    progress_sink,
                    lambda: os.close(progress_pipe.read_fd),
                    "progress",
                )
            )
        for descriptor, accumulator, sink, closer, name in streams:
            reader = threading.Thread(
                target=read_descriptor,
                args=(descriptor, accumulator, sink),
                kwargs={"close_stream": closer},
                name=f"production-process-{name}",
                daemon=False,
            )
            readers.append(reader)
            reader.start()
        try:
            returncode = process.wait(timeout=timeout_milliseconds / 1000)
        except subprocess.TimeoutExpired as error:
            _kill_process_group(process)
            _join_process_readers(process, readers, progress_pipe=progress_pipe)
            raise ProductionProcessTimeoutError("production process exceeded its timeout") from error
        _join_process_readers(process, readers, progress_pipe=progress_pipe)
        if reader_failure:
            error = reader_failure[0]
            if isinstance(error, OSError):
                raise ProductionProcessReaderError("production process pipe drain failed") from error
            raise ProductionProcessSinkError("production process output sink failed") from error
        return ProductionStreamingProcessResult(
            returncode,
            stdout.result(),
            stderr.result(),
            progress.result() if progress is not None else None,
        )
    except BaseException:
        if process is not None:
            _kill_process_group(process)
            _join_process_readers(process, readers, progress_pipe=progress_pipe)
        elif progress_pipe is not None:
            close_process_pipe(progress_pipe)
        raise


def run_bounded_process(
    argv: tuple[str, ...],
    *,
    timeout_milliseconds: int,
    stdout_max_bytes: int,
    stderr_max_bytes: int,
    pass_fds: tuple[int, ...],
) -> ProductionProcessResult:
    """Compatibility runner that retains bounded stdout/stderr prefixes only."""

    if stdout_max_bytes < 0 or stderr_max_bytes <= 0:
        raise ValueError("bounded process output limits are invalid")
    result = run_streaming_process(
        argv,
        timeout_milliseconds=timeout_milliseconds,
        stdout_diagnostic_max_bytes=stdout_max_bytes,
        stderr_diagnostic_max_bytes=stderr_max_bytes,
        pass_fds=pass_fds,
        terminate_on_diagnostic_limit=True,
    )
    return ProductionProcessResult(
        result.returncode,
        result.stdout.diagnostic_prefix,
        result.stderr.diagnostic_prefix,
        result.stdout.diagnostic_limit_exceeded,
        result.stderr.diagnostic_limit_exceeded,
        result.stdout.byte_count,
        result.stderr.byte_count,
        result.stdout.sha256,
        result.stderr.sha256,
    )


def resolve_executable(executable: str | os.PathLike[str] | None, *, default_name: str) -> Path:
    """Resolve one regular executable without treating a shell or PATH string as authority."""

    if type(default_name) is not str or not default_name:
        raise ValueError("production executable default name must be non-empty text")
    selected = shutil.which(os.fspath(executable) if executable is not None else default_name)
    if selected is None:
        raise ProductionExecutableError("production executable is unavailable")
    try:
        path = Path(selected).resolve(strict=True)
        status = path.lstat()
        if not stat.S_ISREG(status.st_mode) or stat.S_ISLNK(status.st_mode):
            raise OSError("production executable is not a regular file")
        return path
    except OSError as error:
        raise ProductionExecutableError("production executable identity could not be verified") from error


def copy_pin_executable(source: Path, destination: Path) -> PinnedExecutable:
    """Copy exact regular executable bytes into a private destination without following links."""

    if not destination.is_absolute():
        raise ValueError("production executable pin paths are invalid")
    source_fd: int | None = None
    destination_fd: int | None = None
    try:
        source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        source_status = os.fstat(source_fd)
        if not stat.S_ISREG(source_status.st_mode):
            raise OSError("production executable source is not a regular file")
        destination_fd = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o500,
        )
        digest = hashlib.sha256()
        length = 0
        while chunk := os.read(source_fd, _FILE_HASH_CHUNK_BYTES):
            digest.update(chunk)
            length += len(chunk)
            written = 0
            while written < len(chunk):
                written += os.write(destination_fd, chunk[written:])
        os.fsync(destination_fd)
        os.fchmod(destination_fd, 0o500)
        destination_status = os.fstat(destination_fd)
        final_source_status = os.fstat(source_fd)
        if (
            destination_status.st_size != length
            or destination_status.st_nlink != 1
            or not stat.S_ISREG(destination_status.st_mode)
            or (source_status.st_dev, source_status.st_ino, source_status.st_size)
            != (final_source_status.st_dev, final_source_status.st_ino, final_source_status.st_size)
        ):
            raise OSError("production executable changed while it was pinned")
        return PinnedExecutable(destination, f"sha256:{digest.hexdigest()}", length)
    except OSError as error:
        raise ProductionExecutableError("production executable could not be pinned") from error
    finally:
        if destination_fd is not None:
            os.close(destination_fd)
        if source_fd is not None:
            os.close(source_fd)


def probe_executable_version(
    executable: PinnedExecutable,
    *,
    runner: ProductionProcessRunner = run_bounded_process,
    timeout_milliseconds: int = 10_000,
    max_bytes: int = _TOOL_VERSION_MAX_BYTES,
) -> ProductionExecutableVersion:
    """Run a bounded ``-version`` probe against exact pinned bytes."""

    if type(executable) is not PinnedExecutable:  # noqa: E721
        raise ValueError("production executable version probe requires a pinned executable")
    if type(timeout_milliseconds) is not int or timeout_milliseconds <= 0:  # noqa: E721
        raise ValueError("production executable version timeout is invalid")
    if type(max_bytes) is not int or max_bytes <= 0:  # noqa: E721
        raise ValueError("production executable version cap is invalid")
    try:
        result = runner(
            (str(executable.path), "-version"),
            timeout_milliseconds=timeout_milliseconds,
            stdout_max_bytes=max_bytes,
            stderr_max_bytes=max_bytes,
            pass_fds=(),
        )
    except Exception as error:
        raise ProductionExecutableError("production executable version probe failed") from error
    if (
        type(result) is not ProductionProcessResult
        or result.returncode != 0
        or result.stdout_limit_exceeded
        or result.stderr_limit_exceeded
    ):
        raise ProductionExecutableError("production executable returned an invalid version response")
    return ProductionExecutableVersion(_sha256_bytes(result.stdout + b"\0" + result.stderr), result.stdout, result.stderr)


def pin_executable(
    source: Path,
    destination: Path,
    *,
    runner: ProductionProcessRunner = run_bounded_process,
) -> tuple[PinnedExecutable, ProductionExecutableIdentity]:
    """Pin exact bytes and bind them to one bounded version response."""

    pinned = copy_pin_executable(source, destination)
    version = probe_executable_version(pinned, runner=runner)
    return (
        pinned,
        ProductionExecutableIdentity(pinned.sha256, pinned.byte_length, version.output_sha256),
    )


def reverify_pinned_executable(
    executable: PinnedExecutable,
    *,
    expected_sha256: str | None = None,
    expected_byte_length: int | None = None,
) -> None:
    """Fail closed if private executable bytes or stable file identity changed."""

    if type(executable) is not PinnedExecutable:  # noqa: E721
        raise ValueError("production executable reverification requires a pinned executable")
    expected_hash = expected_sha256 or executable.sha256
    expected_length = expected_byte_length if expected_byte_length is not None else executable.byte_length
    sha256_prefixed(expected_hash, "production executable expected sha256")
    if type(expected_length) is not int or expected_length <= 0:  # noqa: E721
        raise ValueError("production executable expected byte length is invalid")
    try:
        status = executable.path.lstat()
        actual_sha256, actual_length = _sha256_file(executable.path)
        final_status = executable.path.lstat()
    except OSError as error:
        raise ProductionExecutableError("pinned production executable could not be reverified") from error
    if (
        status.st_uid != os.geteuid()
        or status.st_mode & 0o077
        or not stat.S_ISREG(status.st_mode)
        or status.st_nlink != 1
        or actual_sha256 != expected_hash
        or actual_length != expected_length
        or (final_status.st_dev, final_status.st_ino, final_status.st_size)
        != (status.st_dev, status.st_ino, status.st_size)
    ):
        raise ProductionExecutableError("pinned production executable identity changed")


def _validate_process_request(
    argv: tuple[str, ...],
    timeout_milliseconds: int,
    stdout_max_bytes: int,
    stderr_max_bytes: int,
    progress_max_bytes: int,
    pass_fds: tuple[int, ...],
    environment: Mapping[str, str] | None,
    progress_pipe: object,
) -> None:
    if (
        type(argv) is not tuple
        or not argv
        or any(type(item) is not str or not item for item in argv)
        or type(timeout_milliseconds) is not int
        or timeout_milliseconds <= 0
        or any(type(value) is not int or value < 0 for value in (stdout_max_bytes, stderr_max_bytes, progress_max_bytes))
        or any(type(value) is not int or value < 0 for value in pass_fds)
        or len(pass_fds) != len(set(pass_fds))
        or (progress_pipe is not None and type(progress_pipe) is not ProductionProcessPipe)
    ):
        raise ValueError("production process request is invalid")
    if environment is not None and any(
        type(key) is not str or type(value) is not str for key, value in environment.items()
    ):
        raise ValueError("production process environment is invalid")
    if progress_pipe is not None:
        exact_progress_pipe = progress_pipe
        if (
            exact_progress_pipe.read_fd in pass_fds
            or exact_progress_pipe.write_fd in pass_fds
        ):
            raise ValueError("production progress descriptors must not be duplicated")


def _effective_environment(overlay: Mapping[str, str] | None) -> dict[str, str]:
    environment: dict[str, str] = {}
    if overlay is not None:
        environment.update(overlay)
    environment.update(FIXED_PROCESS_ENVIRONMENT)
    return environment


def _join_process_readers(
    process: subprocess.Popen[bytes],
    readers: list[threading.Thread],
    *,
    progress_pipe: ProductionProcessPipe | None,
) -> None:
    deadline = time.monotonic() + 0.25
    for reader in readers:
        reader.join(max(0, deadline - time.monotonic()))
    if any(reader.is_alive() for reader in readers):
        _kill_process_group(process)
        for reader in readers:
            reader.join(timeout=1)
    if any(reader.is_alive() for reader in readers):
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
        if progress_pipe is not None:
            try:
                os.close(progress_pipe.read_fd)
            except OSError:
                pass
        for reader in readers:
            reader.join(timeout=1)
    if any(reader.is_alive() for reader in readers):
        raise ProductionProcessReaderError("production process output readers did not terminate")


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    """Terminate the process session and helpers retaining inherited descriptors."""

    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (PermissionError, ProcessLookupError):
        pass


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    length = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(_FILE_HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
            length += len(chunk)
    return f"sha256:{digest.hexdigest()}", length


def _sha256_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


__all__ = (
    "FIXED_PROCESS_ENVIRONMENT",
    "PinnedExecutable",
    "ProductionExecutableError",
    "ProductionExecutableIdentity",
    "ProductionExecutableVersion",
    "ProductionProcessError",
    "ProductionProcessPipe",
    "ProductionProcessReaderError",
    "ProductionProcessResult",
    "ProductionProcessRunner",
    "ProductionProcessSinkError",
    "ProductionProcessTimeoutError",
    "ProductionStreamResult",
    "ProductionStreamingProcessResult",
    "close_process_pipe",
    "copy_pin_executable",
    "create_process_pipe",
    "pin_executable",
    "probe_executable_version",
    "resolve_executable",
    "reverify_pinned_executable",
    "run_bounded_process",
    "run_streaming_process",
)
