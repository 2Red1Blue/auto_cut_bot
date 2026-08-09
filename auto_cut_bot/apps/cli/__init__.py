"""CLI app adapter for the unified Apps domain."""

from auto_cut_bot.apps.cli.service import (
    CliAppError,
    CliAppManager,
    CliAppsRuntimeConfig,
)

__all__ = [
    "CliAppError",
    "CliAppManager",
    "CliAppsRuntimeConfig",
]
