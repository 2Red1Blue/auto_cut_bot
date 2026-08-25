"""Explicit authority-admin bootstrap for the timed-speech registry.

This command is intentionally outside the Pipeline HTTP surface. It accepts no
authority source or profile payload from CLI options; deployment composition
injects the immutable lock locator before command registration.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import cast

import psycopg
import typer
from autocut_kernel.registry import (
    BootstrapTimedSpeechProfileRegistryCommand,
)
from autocut_kernel.store import PostgresRuntimeStore
from autocut_kernel.store.postgres import DbConnection

from auto_cut_bot.authority import (
    LockedRegistryDeployment,
    LockedRegistrySourceError,
    load_locked_timed_speech_authority_context,
)


def register_authority_bootstrap_command(
    app: typer.Typer, *, deployment: LockedRegistryDeployment | None = None
) -> None:
    """Register the one authority-only writer with the administrative CLI."""

    @app.command("authority-bootstrap-timed-speech-profile")
    def authority_bootstrap_timed_speech_profile(  # pyright: ignore[reportUnusedFunction]
        postgres_dsn: str = typer.Option(
            ...,
            "--postgres-dsn",
            help="Authority PostgreSQL DSN; never sourced from Pipeline runtime configuration.",
        ),
        authority_admin_confirmation: bool = typer.Option(
            False,
            "--authority-admin-confirmation",
            help="Required acknowledgement for an immutable authority write.",
        ),
    ) -> None:
        """Bootstrap or replay one immutable profile anchor as an authority admin."""
        if not authority_admin_confirmation:
            raise typer.BadParameter(
                "--authority-admin-confirmation is required for authority bootstrap"
            )
        if not postgres_dsn.startswith(("postgresql://", "postgres://")):
            raise typer.BadParameter("--postgres-dsn must be a PostgreSQL DSN")
        if type(deployment) is not LockedRegistryDeployment:  # noqa: E721
            raise typer.BadParameter("authority bootstrap requires deployment lock injection")
        try:
            context = load_locked_timed_speech_authority_context(deployment)
            store = PostgresRuntimeStore(
                cast(Callable[[], DbConnection], lambda: psycopg.connect(postgres_dsn))
            )
            outcome = BootstrapTimedSpeechProfileRegistryCommand(store).execute(
                context.bootstrap_request()
            )
        except (LockedRegistrySourceError, ValueError) as error:
            raise typer.BadParameter(str(error)) from error
        typer.echo(
            json.dumps(
                {
                    "artifact_set_id": str(outcome.artifact_set_id) if outcome.artifact_set_id else None,
                    "receipt_id": str(outcome.receipt_id) if outcome.receipt_id else None,
                    "registry_set_sha256": context.snapshot.registry_set_sha256,
                    "state": outcome.state,
                },
                sort_keys=True,
            )
        )
