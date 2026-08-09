"""Slash command routing and built-in handlers."""

from auto_cut_bot.command.builtin import register_builtin_commands
from auto_cut_bot.command.router import CommandContext, CommandRouter

__all__ = ["CommandContext", "CommandRouter", "register_builtin_commands"]
