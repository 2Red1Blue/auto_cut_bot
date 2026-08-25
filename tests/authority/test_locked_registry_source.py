from __future__ import annotations

import hashlib
import inspect
import json
import subprocess
from pathlib import Path

import pytest
import typer

from auto_cut_bot.authority import (
    LockedRegistryDeployment,
    LockedRegistrySourceError,
    load_locked_timed_speech_authority_context,
)
from auto_cut_bot.cli.authority_bootstrap import register_authority_bootstrap_command


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def _deployment(tmp_path: Path, *, tamper: bool = False) -> LockedRegistryDeployment:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "test")
    profile = tmp_path / "stage_05" / "timed_speech_profiles.yaml"
    profile.parent.mkdir()
    profile.write_text("not: a-valid-profile-lock\n", encoding="utf-8")
    raw = profile.read_bytes()
    lock = {
        "format": "autocut.locked-registry-source/v1",
        "registry_set_sha256": "sha256:" + "a" * 64,
        "enabled_profile": {"profile_id": "profile", "profile_version": "v1"},
        "sources": [{"path": "stage_05/timed_speech_profiles.yaml", "git_blob_oid": "0" * 40, "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(), "byte_length": len(raw)}],
    }
    (tmp_path / "authority.lock.json").write_text(json.dumps(lock), encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "lock")
    commit = _git(tmp_path, "rev-parse", "HEAD")
    if tamper:
        profile.write_text("checkout mutation\n", encoding="utf-8")
    return LockedRegistryDeployment(tmp_path, commit, "authority.lock.json")


def test_loader_rejects_lock_blob_identity_before_parsing_checkout_source(tmp_path: Path) -> None:
    with pytest.raises(LockedRegistrySourceError, match="blob"):
        load_locked_timed_speech_authority_context(_deployment(tmp_path, tamper=True))


def test_bootstrap_cli_has_no_authority_source_or_profile_options() -> None:
    app = typer.Typer()
    register_authority_bootstrap_command(app)
    callback = next(command.callback for command in app.registered_commands if command.name == "authority-bootstrap-timed-speech-profile")
    assert callback is not None
    assert "authority_source" not in inspect.signature(callback).parameters
    assert "profile_id" not in inspect.signature(callback).parameters
