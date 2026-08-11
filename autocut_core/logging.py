"""Logging utilities for autocut_core.

Provides get_logger and configure_logging as the canonical logging interface
for the pipeline layer. Delegates to stdlib logging.
"""

from __future__ import annotations

import logging
import sys
from typing import Any


def get_logger(name: str) -> Any:
    """Return a logger for the given module name."""
    return logging.getLogger(name)


def configure_logging(level: int = logging.INFO) -> None:
    """Configure basic logging for the pipeline.

    Sets up a stream handler with a simple format if none is already configured.
    """
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        root.addHandler(handler)
        root.setLevel(level)


__all__ = ["get_logger", "configure_logging"]