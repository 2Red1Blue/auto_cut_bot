"""Test modules exposed under ``tools.*`` without hiding repo tooling.

Pytest may import this directory as the ``tools`` package while collecting the
tool tests.  Extend its package search path with the repository's real
authority/architecture tools so collection order cannot make imports
non-deterministic.
"""

from __future__ import annotations

from pathlib import Path

_REPOSITORY_TOOLS = Path(__file__).resolve().parents[2] / "tools"
if str(_REPOSITORY_TOOLS) not in __path__:
    __path__.append(str(_REPOSITORY_TOOLS))
