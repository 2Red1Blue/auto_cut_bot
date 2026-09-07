"""Isolation planning and child environment whitelist."""

from __future__ import annotations

import pytest

from tools.reuse.sandbox import (
    IsolationUnavailableError,
    build_child_env,
    build_sandboxed_command,
    child_has_secret_markers,
    isolation_mode,
    platform_supports_isolation,
)


class TestIsolationPlan:
    def test_darwin_has_no_verifiable_isolation(self) -> None:
        assert not platform_supports_isolation("darwin")
        assert isolation_mode("darwin") is None

    def test_linux_with_unshare_gets_netns(self) -> None:
        assert isolation_mode("linux", unshare_available=True) == "netns"
        assert isolation_mode("linux", unshare_available=False) is None

    def test_sandboxed_command_wraps_unshare(self) -> None:
        argv = build_sandboxed_command(["python", "x.py"], platform="linux", unshare_bin="/usr/bin/unshare")
        assert argv[:3] == ["/usr/bin/unshare", "--net", "--"]

    def test_sandboxed_command_refuses_on_darwin(self) -> None:
        with pytest.raises(IsolationUnavailableError):
            build_sandboxed_command(["python", "x.py"], platform="darwin")


class TestChildEnv:
    def test_whitelist_strips_secrets(self) -> None:

        env = build_child_env()
        assert "PATH" in env or "HOME" in env
        for key in env:
            assert key not in (
                "ANTHROPIC_API_KEY",
                "OPENAI_API_KEY",
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "DATABASE_URL",
            )

    def test_secret_marker_detection(self) -> None:
        assert child_has_secret_markers({"MY_API_KEY": "x"}) == ["MY_API_KEY"]
        assert child_has_secret_markers({"PATH": "/usr/bin"}) == []
