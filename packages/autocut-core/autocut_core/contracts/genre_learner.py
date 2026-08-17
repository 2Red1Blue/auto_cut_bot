"""GenreLearner — thin backward-compatible wrapper around GenreEvolutionEngine.

After a pipeline produces a story_render output, the learner can inspect the
Series Bible and Story Scripts to infer a genre profile and save it to
``_references/editorial-knowledge/`` so future runs can route to it automatically.

This module delegates to ``GenreEvolutionEngine`` in ``genre_evolution.py``
for the full CAPTURED pipeline.  The ``GenreLearner`` class is preserved as a
thin wrapper for backward compatibility with existing callers.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from autocut_core.contracts.genre_evolution import GenreEvolutionEngine
from autocut_core.contracts.genre_registry import GenreAdapter


class GenreLearner:
    """Thin backward-compatible wrapper around GenreEvolutionEngine.

    All extraction, validation, and persistence logic lives in
    ``GenreEvolutionEngine``.  This class preserves the original API for
    callers that import ``GenreLearner`` directly.

    Usage::

        adapter = GenreLearner.extract(bible, stories)
        if adapter:
            path = GenreLearner.save_profile(adapter, output_dir)

    For the full CAPTURED pipeline, use ``GenreEvolutionEngine.capture()``
    directly.
    """

    # ── Minimum required keys to produce a valid adapter ──────────────────
    _MIN_REQUIRED_KEYS = GenreEvolutionEngine._MIN_REQUIRED_KEYS

    @classmethod
    def extract(
        cls,
        bible: dict[str, Any] | None,
        stories: list[dict[str, Any]] | None = None,
    ) -> GenreAdapter | None:
        """Extract a GenreAdapter from the Series Bible and optional Story Scripts.

        Delegates to ``GenreEvolutionEngine.extract()``.
        """
        return GenreEvolutionEngine.extract(bible, stories)

    @classmethod
    def _infer_support_roles(cls, bible: dict[str, Any]) -> list[str]:
        """Infer support roles from the Bible's character list.

        Delegates to ``GenreEvolutionEngine._infer_support_roles()``.
        """
        return GenreEvolutionEngine._infer_support_roles(bible)

    @classmethod
    def save_profile(cls, adapter: GenreAdapter, output_dir: Path) -> Path:
        """Save *adapter* as a JSON golden case in *output_dir*.

        Delegates to ``GenreEvolutionEngine.save_profile()``.
        """
        return GenreEvolutionEngine.save_profile(adapter, output_dir)

    @classmethod
    def should_learn(cls, genre_route: dict[str, Any]) -> bool:
        """Return True if the genre route indicates a learning opportunity.

        Delegates to ``GenreEvolutionEngine.should_learn()``.
        """
        return GenreEvolutionEngine.should_learn(genre_route)