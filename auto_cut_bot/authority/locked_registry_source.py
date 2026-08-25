"""Read timed-speech authority only from lock-verified committed Git blobs."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from autocut_kernel.contracts.compiler.registry_source import (
    _load_yaml_bytes,  # pyright: ignore[reportPrivateUsage]
)
from autocut_kernel.media import (
    TimedSpeechProfileRegistryEntry,
    decode_timed_speech_profile_registry_entry,
)
from autocut_kernel.registry import (
    AuthorityRegistrySnapshot,
    TimedSpeechProfileKey,
    VerifiedTimedSpeechAuthorityContext,
)

_COMMIT = re.compile(r"[0-9a-f]{40,64}\Z")
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_BLOB = re.compile(r"[0-9a-f]{40,64}\Z")
_PROFILE_PATH = "stage_05/timed_speech_profiles.yaml"
_PROFILE_FORMAT = "autocut.timed-speech-profiles.source/v1"


class LockedRegistrySourceError(ValueError):
    """Deployment lock or immutable registry blob verification failed."""


@dataclass(frozen=True, slots=True)
class LockedRegistryDeployment:
    """Deployment injection; no CLI/runtime request may select these values."""

    repository: Path
    commit: str
    lock_path: str

    def __post_init__(self) -> None:
        if not _COMMIT.fullmatch(self.commit):
            raise LockedRegistrySourceError("deployment authority commit must be a full hex commit")
        if not self.lock_path or self.lock_path.startswith("/") or ".." in self.lock_path.split("/"):
            raise LockedRegistrySourceError("deployment authority lock path is invalid")


def _git(deployment: LockedRegistryDeployment, *args: str) -> bytes:
    try:
        return subprocess.run(
            ["git", *args], cwd=deployment.repository, check=True, capture_output=True
        ).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        raise LockedRegistrySourceError("locked authority Git object is unavailable") from error


def _blob(deployment: LockedRegistryDeployment, path: str) -> tuple[bytes, str]:
    if not path or path.startswith("/") or ".." in path.split("/"):
        raise LockedRegistrySourceError("locked authority source path is invalid")
    spec = f"{deployment.commit}:{path}"
    oid = _git(deployment, "rev-parse", spec).decode("ascii", "strict").strip()
    if not _BLOB.fullmatch(oid):
        raise LockedRegistrySourceError("locked authority source is not a Git blob")
    return _git(deployment, "show", spec), oid


def _object(raw: bytes, label: str) -> dict[str, object]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8", "strict"), object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise LockedRegistrySourceError(f"{label} is not closed JSON") from error
    if type(value) is not dict:  # noqa: E721
        raise LockedRegistrySourceError(f"{label} must be an object")
    return cast(dict[str, object], value)


def load_locked_timed_speech_authority_context(
    deployment: LockedRegistryDeployment,
) -> VerifiedTimedSpeechAuthorityContext:
    """Resolve the one enabled profile from immutable blobs named by a lock."""

    if type(deployment) is not LockedRegistryDeployment:  # noqa: E721
        raise LockedRegistrySourceError("authority deployment must be explicitly injected")
    lock_raw, _lock_oid = _blob(deployment, deployment.lock_path)
    lock = _object(lock_raw, "authority lock")
    expected = {"format", "registry_set_sha256", "enabled_profile", "sources"}
    if set(lock) != expected or lock.get("format") != "autocut.locked-registry-source/v1":
        raise LockedRegistrySourceError("authority lock schema is invalid")
    registry_hash = lock["registry_set_sha256"]
    if type(registry_hash) is not str or not _SHA256.fullmatch(registry_hash):  # noqa: E721
        raise LockedRegistrySourceError("authority lock registry hash is invalid")
    profile = lock["enabled_profile"]
    if type(profile) is not dict:  # noqa: E721
        raise LockedRegistrySourceError("authority lock enabled profile is invalid")
    profile_mapping = cast(dict[str, object], profile)
    if set(profile_mapping) != {"profile_id", "profile_version"}:
        raise LockedRegistrySourceError("authority lock enabled profile is invalid")
    profile_key = TimedSpeechProfileKey(
        cast(str, profile_mapping["profile_id"]), cast(str, profile_mapping["profile_version"])
    )
    sources = lock["sources"]
    if type(sources) is not list:  # noqa: E721
        raise LockedRegistrySourceError("authority lock must name exactly one timed-speech source")
    source_values = cast(list[object], sources)
    if len(source_values) != 1:
        raise LockedRegistrySourceError("authority lock must name exactly one timed-speech source")
    source = source_values[0]
    if type(source) is not dict:  # noqa: E721
        raise LockedRegistrySourceError("authority lock source schema is invalid")
    source_mapping = cast(dict[str, object], source)
    if set(source_mapping) != {"path", "git_blob_oid", "sha256", "byte_length"}:
        raise LockedRegistrySourceError("authority lock source schema is invalid")
    path, raw_oid, raw_sha, byte_length = (
        source_mapping["path"], source_mapping["git_blob_oid"], source_mapping["sha256"], source_mapping["byte_length"]
    )
    if path != _PROFILE_PATH or type(raw_oid) is not str or not _BLOB.fullmatch(raw_oid):  # noqa: E721
        raise LockedRegistrySourceError("authority lock source identity is invalid")
    if type(raw_sha) is not str or not _SHA256.fullmatch(raw_sha) or type(byte_length) is not int:  # noqa: E721
        raise LockedRegistrySourceError("authority lock source digest is invalid")
    raw, oid = _blob(deployment, _PROFILE_PATH)
    if oid != raw_oid or len(raw) != byte_length or "sha256:" + hashlib.sha256(raw).hexdigest() != raw_sha:
        raise LockedRegistrySourceError("authority lock source blob does not match its immutable lock")
    try:
        value = _load_yaml_bytes(raw, origin=f"git:{deployment.commit}:{_PROFILE_PATH}")
    except ValueError as error:
        raise LockedRegistrySourceError("locked timed-speech source is invalid") from error
    if type(value) is not dict or set(value) != {"format", "profiles"} or value["format"] != _PROFILE_FORMAT:  # noqa: E721
        raise LockedRegistrySourceError("locked timed-speech source schema is invalid")
    profiles = value["profiles"]
    if type(profiles) is not list or not profiles:  # noqa: E721
        raise LockedRegistrySourceError("locked timed-speech profiles are invalid")
    matches: list[TimedSpeechProfileRegistryEntry] = []
    for candidate in cast(list[object], profiles):
        try:
            entry = decode_timed_speech_profile_registry_entry(candidate)
        except ValueError as error:
            raise LockedRegistrySourceError("locked timed-speech profile entry is invalid") from error
        if TimedSpeechProfileKey(entry.profile_id, entry.profile_version) == profile_key:
            matches.append(entry)
    if len(matches) != 1:
        raise LockedRegistrySourceError("lock must resolve exactly one timed-speech profile")
    return VerifiedTimedSpeechAuthorityContext(AuthorityRegistrySnapshot(registry_hash, profile_key), matches[0])
