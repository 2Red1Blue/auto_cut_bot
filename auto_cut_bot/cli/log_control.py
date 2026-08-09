"""Runtime log visibility controls shared by CLI commands."""

from loguru import logger

__all__ = ["_set_auto_cut_bot_logs"]


def _set_auto_cut_bot_logs(enabled: bool) -> None:
    if enabled:
        logger.enable("auto_cut_bot")
    else:
        logger.disable("auto_cut_bot")
