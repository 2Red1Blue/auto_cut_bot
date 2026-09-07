"""Replay subprocess isolation and child environment whitelist.

A real offline replay runs producer code in a Linux network namespace
(`unshare --net`) or a container with `--network=none`. On platforms that
cannot provide verifiable isolation the runner may only perform static
checks — it must never mark `isolation_passed=true`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

# Only these variables pass into child processes. Credentials, proxy settings
# and DSNs inherited from the host never reach a producer subprocess.
CHILD_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "PYTHONPATH",
    "PYTHONHASHSEED",
    "VIRTUAL_ENV",
    "AC_REUSE_MEDIA_ROOT",
    "AC_REUSE_CHILD_ID",
)


class IsolationUnavailableError(RuntimeError):
    """Raised when verifiable network isolation cannot be provided."""


def platform_supports_isolation(platform: str = sys.platform) -> bool:
    return platform.startswith("linux")


def _has_unshare() -> bool:
    return shutil.which("unshare") is not None


def isolation_mode(platform: str = sys.platform, unshare_available: bool | None = None) -> str | None:
    """None on unsupported platforms; 'netns' when verifiable isolation exists.

    `unshare_available` lets callers inject the tool check (defaults to a real
    PATH lookup)."""
    if not platform_supports_isolation(platform):
        return None
    available = _has_unshare() if unshare_available is None else unshare_available
    return "netns" if available else None


def build_sandboxed_command(
    argv: list[str], platform: str = sys.platform, unshare_bin: str | None = None
) -> list[str]:
    """Wrap argv for `--network=none` execution or raise IsolationUnavailableError.

    Passing an explicit `unshare_bin` asserts the tool exists at that path and
    enables the netns plan even where PATH lookup fails."""
    mode = isolation_mode(platform, unshare_available=unshare_bin is not None)
    if mode != "netns":
        raise IsolationUnavailableError(
            f"verifiable network isolation unavailable on {platform}; "
            "only static checks are allowed and isolation must not be marked passed"
        )
    bin_path = unshare_bin or shutil.which("unshare")
    assert bin_path is not None
    return [bin_path, "--net", "--", *argv]


def build_child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Whitelist-based child environment; secrets never leak by omission."""
    env: dict[str, str] = {}
    for key in CHILD_ENV_ALLOWLIST:
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    if extra:
        env.update(extra)
    return env


def child_has_secret_markers(env: dict[str, str]) -> list[str]:
    """Defense-in-depth check used by tests and the IPC host before spawning."""
    suspicious = []
    for key in env:
        upper = key.upper()
        if any(
            marker in upper
            for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD", "DSN", "PROXY", "ANTHROPIC", "OPENAI")
        ):
            suspicious.append(key)
    return suspicious


def verify_no_network(timeout: float = 5.0) -> bool:
    """Best-effort probe for use inside a supposedly isolated child.

    The authoritative guarantee is the netns/container itself; this probe is an
    additional assertion, not a substitute."""
    try:
        result = subprocess.run(
            ["getent", "hosts", "example.com"],
            capture_output=True, timeout=timeout, check=False,
        )
        return result.returncode != 0
    except (OSError, subprocess.TimeoutExpired):
        return True
