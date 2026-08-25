"""Ordinary CLI must not expose an authority bootstrap before A/B/C lock support."""

from auto_cut_bot.cli.commands import app


def test_ordinary_cli_has_no_timed_speech_authority_bootstrap_command() -> None:
    assert all(
        command.name != "authority-bootstrap-timed-speech-profile"
        for command in app.registered_commands
    )
