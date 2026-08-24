"""Argv-only subprocess execution with hard output and time bounds."""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import BinaryIO, Protocol, cast

from .models import LocalMediaToolError


@dataclass(frozen=True, slots=True)
class CommandOutput:
    argv: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes


class CommandRunner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: int,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
    ) -> CommandOutput: ...


@dataclass(slots=True)
class _DrainState:
    data: bytearray
    exceeded: bool = False


def _drain(
    stream: BinaryIO,
    state: _DrainState,
    limit: int,
    process: subprocess.Popen[bytes],
) -> None:
    try:
        while chunk := stream.read(64 * 1024):
            remaining = limit - len(state.data)
            if len(chunk) > remaining:
                state.data.extend(chunk[: max(remaining, 0)])
                state.exceeded = True
                process.kill()
                return
            state.data.extend(chunk)
    finally:
        stream.close()


class BoundedSubprocessRunner:
    """Run a direct executable while draining both pipes within fixed limits."""

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: int,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
    ) -> CommandOutput:
        arguments = tuple(argv)
        if not arguments or any(type(item) is not str or not item for item in arguments):  # noqa: E721
            raise LocalMediaToolError("tool argv must contain non-empty strings")
        if min(timeout_seconds, max_stdout_bytes, max_stderr_bytes) <= 0:
            raise LocalMediaToolError("tool execution bounds must be positive")
        try:
            process = subprocess.Popen(
                arguments,
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as error:
            raise LocalMediaToolError("required local media tool could not start") from error
        if process.stdout is None or process.stderr is None:  # pragma: no cover - PIPE contract
            process.kill()
            raise LocalMediaToolError("tool output pipes were not created")
        stdout_state = _DrainState(bytearray())
        stderr_state = _DrainState(bytearray())
        stdout_thread = threading.Thread(
            target=_drain,
            args=(cast(BinaryIO, process.stdout), stdout_state, max_stdout_bytes, process),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_drain,
            args=(cast(BinaryIO, process.stderr), stderr_state, max_stderr_bytes, process),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        try:
            returncode = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as error:
            process.kill()
            process.wait()
            stdout_thread.join()
            stderr_thread.join()
            raise LocalMediaToolError("local media tool timed out") from error
        stdout_thread.join()
        stderr_thread.join()
        if stdout_state.exceeded:
            raise LocalMediaToolError("local media tool exceeded bounded stdout")
        if stderr_state.exceeded:
            raise LocalMediaToolError("local media tool exceeded bounded stderr")
        return CommandOutput(
            arguments,
            returncode,
            bytes(stdout_state.data),
            bytes(stderr_state.data),
        )


__all__ = ["BoundedSubprocessRunner", "CommandOutput", "CommandRunner"]
