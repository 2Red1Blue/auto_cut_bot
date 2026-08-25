"""Explicit authority-admin bootstrap for the timed-speech registry.

This command is intentionally outside the Pipeline HTTP surface.  It accepts
only a compiled authority source root and an exact profile key; profile payloads
are never supplied by environment variables, ordinary CLI request JSON, or a
Pipeline run request.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import cast

import psycopg
import typer
from autocut_kernel.contracts.compiler.registry import RegistrySet
from autocut_kernel.contracts.compiler.registry_source import (
    _load_yaml_bytes,  # pyright: ignore[reportPrivateUsage]
    load_registry_source_manifest,
)
from autocut_kernel.media import (
    TimedSpeechProfileRegistryEntry,
    decode_timed_speech_profile_registry_entry,
)
from autocut_kernel.registry import (
    AuthorityRegistrySnapshot,
    BootstrapTimedSpeechProfileRegistryCommand,
    TimedSpeechProfileKey,
    VerifiedTimedSpeechAuthorityContext,
)
from autocut_kernel.store import PostgresRuntimeStore
from autocut_kernel.store.postgres import DbConnection

_TIMED_SPEECH_PROFILE_SOURCE_PATH = "stage_05/timed_speech_profiles.yaml"
_TIMED_SPEECH_PROFILE_SOURCE_FORMAT = "autocut.timed-speech-profiles.source/v1"


class AuthorityBootstrapSourceError(ValueError):
    """The locked authority source cannot supply one exact profile."""


def load_verified_timed_speech_authority_context(
    authority_source: Path,
    *,
    profile_id: str,
    profile_version: str,
) -> VerifiedTimedSpeechAuthorityContext:
    """Compile one locked source and resolve exactly one signed profile entry."""
    try:
        profile_key = TimedSpeechProfileKey(profile_id, profile_version)
        manifest = load_registry_source_manifest(authority_source)
        registry = RegistrySet.from_manifest(manifest)
        registry.require_ready()
        snapshot = next(
            item
            for item in manifest.source_snapshot
            if item.path == _TIMED_SPEECH_PROFILE_SOURCE_PATH
        )
    except (StopIteration, ValueError) as error:
        raise AuthorityBootstrapSourceError(
            "compiled authority source does not contain the timed-speech profile lock"
        ) from error
    try:
        value = _load_yaml_bytes(snapshot.raw, origin=_TIMED_SPEECH_PROFILE_SOURCE_PATH)
    except ValueError as error:
        raise AuthorityBootstrapSourceError("timed-speech profile lock is invalid") from error
    if type(value) is not dict:  # noqa: E721
        raise AuthorityBootstrapSourceError("timed-speech profile lock must be an object")
    mapping = cast(dict[str, object], value)
    if frozenset(mapping) != frozenset({"format", "profiles"}):
        raise AuthorityBootstrapSourceError("timed-speech profile lock has unknown or missing fields")
    if mapping["format"] != _TIMED_SPEECH_PROFILE_SOURCE_FORMAT:
        raise AuthorityBootstrapSourceError("timed-speech profile lock format is invalid")
    profiles = mapping["profiles"]
    if type(profiles) is not list or not profiles:  # noqa: E721
        raise AuthorityBootstrapSourceError("timed-speech profile lock must contain profiles")
    resolved: list[TimedSpeechProfileRegistryEntry] = []
    for profile in cast(list[object], profiles):
        try:
            entry = decode_timed_speech_profile_registry_entry(profile)
            if TimedSpeechProfileKey(entry.profile_id, entry.profile_version) == profile_key:
                resolved.append(entry)
        except ValueError as error:
            raise AuthorityBootstrapSourceError("timed-speech profile lock entry is invalid") from error
    if len(resolved) != 1:
        raise AuthorityBootstrapSourceError(
            "compiled authority source must resolve exactly one timed-speech profile"
        )
    return VerifiedTimedSpeechAuthorityContext(
        AuthorityRegistrySnapshot(registry.source_hash, profile_key),
        resolved[0],
    )


def register_authority_bootstrap_command(app: typer.Typer) -> None:
    """Register the one authority-only writer with the administrative CLI."""

    @app.command("authority-bootstrap-timed-speech-profile")
    def authority_bootstrap_timed_speech_profile(  # pyright: ignore[reportUnusedFunction]
        authority_source: Path = typer.Option(
            ...,
            "--authority-source",
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
            help="Root of the compiled, locked authority registry source.",
        ),
        profile_id: str = typer.Option(..., "--profile-id"),
        profile_version: str = typer.Option(..., "--profile-version"),
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
        try:
            context = load_verified_timed_speech_authority_context(
                authority_source,
                profile_id=profile_id,
                profile_version=profile_version,
            )
            store = PostgresRuntimeStore(
                cast(Callable[[], DbConnection], lambda: psycopg.connect(postgres_dsn))
            )
            outcome = BootstrapTimedSpeechProfileRegistryCommand(store).execute(
                context.bootstrap_request()
            )
        except (AuthorityBootstrapSourceError, ValueError) as error:
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
