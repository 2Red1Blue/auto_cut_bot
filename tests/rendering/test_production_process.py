from __future__ import annotations

import hashlib
import os
import sys
import time
from pathlib import Path

import autocut_kernel.rendering.production_process as production_process_module
import pytest
from autocut_kernel.rendering.production_process import (
    FIXED_PROCESS_ENVIRONMENT,
    ProductionProcessSinkError,
    ProductionProcessTimeoutError,
    close_process_pipe,
    copy_pin_executable,
    create_process_pipe,
    probe_executable_version,
    resolve_executable,
    reverify_pinned_executable,
    run_bounded_process,
    run_streaming_process,
)


def _python(source: str, *arguments: str) -> tuple[str, ...]:
    return (sys.executable, "-c", source, *arguments)


def test_streaming_runner_drains_both_pipes_and_applies_closed_fixed_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    monkeypatch.setenv("PRODUCTION_PROCESS_UNRELATED_HOST_VALUE", "must-not-leak")
    source = (
        "import os,sys\n"
        "assert [os.environ[key] for key in "
        "('LC_ALL','LANG','TZ','AV_LOG_FORCE_NOCOLOR')] == "
        "['C','C','UTC','1']\n"
        "assert os.environ['PRODUCTION_PROCESS_APPROVED_VALUE'] == 'allowed'\n"
        "assert 'PRODUCTION_PROCESS_UNRELATED_HOST_VALUE' not in os.environ\n"
        "for _ in range(128):\n"
        "    sys.stdout.buffer.write(b'o' * 8192); sys.stdout.buffer.flush()\n"
        "    sys.stderr.buffer.write(b'e' * 8192); sys.stderr.buffer.flush()\n"
    )

    result = run_streaming_process(
        _python(source),
        timeout_milliseconds=10_000,
        stdout_diagnostic_max_bytes=1024,
        stderr_diagnostic_max_bytes=1024,
        stdout_sink=stdout_chunks.append,
        stderr_sink=stderr_chunks.append,
        environment={
            "LC_ALL": "bad",
            "LANG": "bad",
            "TZ": "bad",
            "AV_LOG_FORCE_NOCOLOR": "0",
            "PRODUCTION_PROCESS_APPROVED_VALUE": "allowed",
        },
    )

    expected_length = 128 * 8192
    assert result.returncode == 0
    assert result.stdout.byte_count == result.stderr.byte_count == expected_length
    assert b"".join(stdout_chunks) == b"o" * expected_length
    assert b"".join(stderr_chunks) == b"e" * expected_length
    assert result.stdout.sha256 == "sha256:" + hashlib.sha256(b"o" * expected_length).hexdigest()
    assert result.stderr.sha256 == "sha256:" + hashlib.sha256(b"e" * expected_length).hexdigest()
    assert result.stdout.diagnostic_prefix == b"o" * 1024
    assert result.stderr.diagnostic_prefix == b"e" * 1024
    assert dict(FIXED_PROCESS_ENVIRONMENT) == {
        "AV_LOG_FORCE_NOCOLOR": "1",
        "LANG": "C",
        "LC_ALL": "C",
        "TZ": "UTC",
    }


def test_bounded_compatibility_runner_reports_prefix_and_full_stream_identity() -> None:
    result = run_bounded_process(
        _python("import sys; sys.stdout.buffer.write(b'a' * 8192); sys.stderr.buffer.write(b'b' * 4096)"),
        timeout_milliseconds=10_000,
        stdout_max_bytes=128,
        stderr_max_bytes=128,
        pass_fds=(),
    )

    assert result.stdout_limit_exceeded
    assert result.stderr_limit_exceeded
    assert result.stdout == b"a" * 128
    assert result.stderr == b"b" * 128
    assert result.stdout_byte_count >= len(result.stdout)
    assert result.stderr_byte_count >= len(result.stderr)
    assert result.stdout_sha256.startswith("sha256:")
    assert result.stderr_sha256.startswith("sha256:")


def test_streaming_runner_drains_independent_progress_pipe() -> None:
    progress = create_process_pipe()
    seen: list[bytes] = []
    source = "import os,sys; os.write(int(sys.argv[1]), b'progress=continue\\nprogress=end\\n')"
    try:
        result = run_streaming_process(
            _python(source, str(progress.write_fd)),
            timeout_milliseconds=10_000,
            stdout_diagnostic_max_bytes=128,
            stderr_diagnostic_max_bytes=128,
            progress_pipe=progress,
            progress_sink=seen.append,
        )
    finally:
        close_process_pipe(progress)

    assert result.returncode == 0
    assert b"".join(seen) == b"progress=continue\nprogress=end\n"
    assert result.progress is not None
    assert result.progress.byte_count == len(b"".join(seen))


def test_sink_failure_terminates_process_group_and_descendants() -> None:
    source = (
        "import subprocess,sys,time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "sys.stdout.buffer.write(b'first'); sys.stdout.buffer.flush()\n"
        "time.sleep(30)\n"
    )
    started = time.monotonic()
    with pytest.raises(ProductionProcessSinkError):
        run_streaming_process(
            _python(source),
            timeout_milliseconds=10_000,
            stdout_diagnostic_max_bytes=128,
            stderr_diagnostic_max_bytes=128,
            stdout_sink=lambda _chunk: (_ for _ in ()).throw(RuntimeError("sink failed")),
        )
    assert time.monotonic() - started < 5


def test_timeout_terminates_parent_and_descendant_holding_output_pipe() -> None:
    source = (
        "import subprocess,sys,time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "time.sleep(30)\n"
    )
    started = time.monotonic()
    with pytest.raises(ProductionProcessTimeoutError):
        run_streaming_process(
            _python(source),
            timeout_milliseconds=100,
            stdout_diagnostic_max_bytes=128,
            stderr_diagnostic_max_bytes=128,
        )
    assert time.monotonic() - started < 5


def test_runner_kills_descendant_that_keeps_a_pipe_open_after_parent_exits() -> None:
    source = (
        "import subprocess,sys\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "sys.stdout.write('done'); sys.stdout.flush()\n"
    )
    started = time.monotonic()
    result = run_streaming_process(
        _python(source),
        timeout_milliseconds=10_000,
        stdout_diagnostic_max_bytes=128,
        stderr_diagnostic_max_bytes=128,
    )

    assert result.returncode == 0
    assert result.stdout.diagnostic_prefix == b"done"
    assert time.monotonic() - started < 5


def test_pinned_executable_version_and_reverification_are_path_independent(tmp_path: Path) -> None:
    source = tmp_path / "fixture-tool"
    source.write_text("#!/bin/sh\nprintf 'fixture tool version 1\\n'\n", encoding="utf-8")
    source.chmod(0o700)
    assert resolve_executable(str(source), default_name="unused") == source.resolve()
    first = copy_pin_executable(source, tmp_path / "one")
    second = copy_pin_executable(source, tmp_path / "two")
    first_version = probe_executable_version(first)
    second_version = probe_executable_version(second)

    assert first.sha256 == second.sha256
    assert first.byte_length == second.byte_length
    assert first_version.output_sha256 == second_version.output_sha256
    reverify_pinned_executable(first)
    os.chmod(first.path, 0o700)
    first.path.write_bytes(b"changed")
    with pytest.raises(production_process_module.ProductionExecutableError, match="identity changed"):
        reverify_pinned_executable(first)


def test_outer_base_exception_kills_process_group_and_reraises_original(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_popen = production_process_module.subprocess.Popen
    killed_process_groups: list[int] = []
    original_kill = production_process_module._kill_process_group  # pyright: ignore[reportPrivateUsage]

    class InterruptingPopen:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self._process = real_popen(*args, **kwargs)  # type: ignore[arg-type]

        @property
        def pid(self) -> int:
            return self._process.pid

        @property
        def stdout(self) -> object:
            return self._process.stdout

        @property
        def stderr(self) -> object:
            return self._process.stderr

        def wait(self, *, timeout: float | None = None) -> int:
            raise KeyboardInterrupt

    def recording_kill(process: object) -> None:
        killed_process_groups.append(process.pid)  # type: ignore[attr-defined]
        original_kill(process)  # type: ignore[arg-type]

    monkeypatch.setattr(production_process_module.subprocess, "Popen", InterruptingPopen)
    monkeypatch.setattr(production_process_module, "_kill_process_group", recording_kill)
    source = (
        "import subprocess,sys,time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "time.sleep(30)\n"
    )

    with pytest.raises(KeyboardInterrupt):
        run_streaming_process(
            _python(source),
            timeout_milliseconds=10_000,
            stdout_diagnostic_max_bytes=128,
            stderr_diagnostic_max_bytes=128,
        )

    assert killed_process_groups
    deadline = time.monotonic() + 1
    while True:
        try:
            os.killpg(killed_process_groups[0], 0)
        except ProcessLookupError:
            break
        if time.monotonic() >= deadline:
            pytest.fail("outer cancellation left a process group alive")
        time.sleep(0.01)
