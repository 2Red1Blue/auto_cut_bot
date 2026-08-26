"""Bounded deployment transport for a controlled installed wheel.

The sibling digest detects byte drift, not an arbitrary replacement wheel.
Decoded content is neither calibration acceptance nor a runtime capability.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from importlib import resources
from typing import cast

from ..contracts.compiler.canonical import (
    canonical_json_hash,
    load_canonical_json_bytes,
    sha256_bytes,
)
from ..contracts.compiler.errors import CanonicalizationError
from .authority_profiles import (
    AuthorityProfileSourceError,
    LocalRunProfileSource,
    ShadowCalibrationProfileSource,
    Stage1NarrativeProfileSource,
    decode_local_run_profile_source,
    decode_shadow_calibration_profile_source,
    decode_stage1_narrative_profile_source,
)
from .timed_speech_contract import TimedSpeechContractError, timed_speech_registry_contract_sha256

_MAX_RESOURCE_BYTES = 16 * 1024 * 1024
_MAX_SOURCE_BYTES = 4 * 1024 * 1024
_HASH = re.compile(r"sha256:[0-9a-f]{64}\Z")
_CHAIN_FIELDS = frozenset({
    "registry_set_sha256", "authority_lock_sha256", "narrative_raw_base64",
    "profile_raw_base64", "schema_raw_base64",
})


class LocalRunResourceError(ValueError):
    """Installed content is absent, malformed, oversized or inconsistent."""


@dataclass(frozen=True, slots=True)
class LocalRunResource:
    current_registry_sha256: str
    current_lock_sha256: str
    predecessor_registry_sha256: str
    predecessor_lock_sha256: str
    narrative: Stage1NarrativeProfileSource
    shadow: ShadowCalibrationProfileSource
    local_run: LocalRunProfileSource


def _source_bound(raw: object) -> bytes:
    if type(raw) is not bytes or not 0 < len(raw) <= _MAX_SOURCE_BYTES:  # noqa: E721
        raise LocalRunResourceError("source bytes are empty, invalid or exceed the resource bound")
    return raw


def compute_local_profile_registry_sha256(
    *, profile_kind: str, narrative_raw: bytes, profile_raw: bytes, schema_raw: bytes,
) -> str:
    """Identify three exact sources; grammar/provenance belong to the caller."""
    if type(profile_kind) is not str or profile_kind not in ("shadow_calibration_v1", "local_run_v1"):  # noqa: E721
        raise LocalRunResourceError("unsupported local profile kind")
    return canonical_json_hash({
        "schema_version": "local-profile-registry-v1",
        "profile_kind": profile_kind,
        "sources": [
            {"role": role, "sha256": sha256_bytes(_source_bound(raw))}
            for role, raw in (("narrative", narrative_raw), ("profile", profile_raw), ("profile_schema", schema_raw))
        ],
    })


def _hash(value: object) -> str:
    if type(value) is not str or _HASH.fullmatch(value) is None or value == "sha256:" + "0" * 64:  # noqa: E721
        raise LocalRunResourceError("resource identity must be a nonzero lowercase SHA-256")
    return value


def _object(value: object, fields: frozenset[str]) -> dict[str, object]:
    if type(value) is not dict or frozenset(cast(dict[str, object], value)) != fields:  # noqa: E721
        raise LocalRunResourceError("resource object does not match its closed fields")
    return cast(dict[str, object], value)


def _source(value: object) -> bytes:
    if type(value) is not str or not value or len(value) > 4 * ((_MAX_SOURCE_BYTES + 2) // 3):  # noqa: E721
        raise LocalRunResourceError("encoded source is empty, invalid or exceeds the resource bound")
    try:
        raw = base64.b64decode(value, validate=True)
        if base64.b64encode(raw).decode("ascii") != value:
            raise LocalRunResourceError("source base64 is not canonical")
        raw = _source_bound(raw)
        load_canonical_json_bytes(raw, origin="installed source")
    except (ValueError, binascii.Error, UnicodeError, RecursionError):
        raise LocalRunResourceError("source must be bounded canonical base64 of strict UTF-8 JSON") from None
    return raw


def _chain(value: object, kind: str) -> tuple[str, str, bytes, bytes, bytes]:
    chain = _object(value, _CHAIN_FIELDS)
    registry_hash, lock_hash = _hash(chain["registry_set_sha256"]), _hash(chain["authority_lock_sha256"])
    narrative, profile, schema = (_source(chain[field]) for field in (
        "narrative_raw_base64", "profile_raw_base64", "schema_raw_base64",
    ))
    if registry_hash != compute_local_profile_registry_sha256(
        profile_kind=kind, narrative_raw=narrative, profile_raw=profile, schema_raw=schema,
    ):
        raise LocalRunResourceError("local profile Registry identity does not match its raw sources")
    return registry_hash, lock_hash, narrative, profile, schema


def decode_local_run_resource(raw: bytes, *, expected_sha256: str) -> LocalRunResource:
    """Decode content only; callers cannot use this as build or Store evidence."""
    if type(raw) is not bytes or not 0 < len(raw) <= _MAX_RESOURCE_BYTES:  # noqa: E721
        raise LocalRunResourceError("resource bytes are empty, invalid or exceed the resource bound")
    if sha256_bytes(raw) != _hash(expected_sha256):
        raise LocalRunResourceError("installed resource digest mismatch")
    try:
        value, _ = load_canonical_json_bytes(raw, origin="installed local-run resource")
    except (CanonicalizationError, RecursionError):
        raise LocalRunResourceError("resource must be strict canonical-compatible UTF-8 JSON") from None
    document = _object(value, frozenset({"schema_version", "current", "predecessor"}))
    if document["schema_version"] != "installed-local-run-authority-v1":
        raise LocalRunResourceError("unsupported installed resource schema")
    current_hash, current_lock, narrative_raw, local_raw, local_schema = _chain(document["current"], "local_run_v1")
    old_hash, old_lock, old_narrative_raw, shadow_raw, shadow_schema = _chain(document["predecessor"], "shadow_calibration_v1")
    try:
        old_narrative = decode_stage1_narrative_profile_source(old_narrative_raw)
        shadow = decode_shadow_calibration_profile_source(
            shadow_raw, narrative=old_narrative, expected_profile_contract_sha256=sha256_bytes(shadow_schema),
        )
        narrative = decode_stage1_narrative_profile_source(narrative_raw)
        local_run = decode_local_run_profile_source(
            local_raw, narrative=narrative, shadow=shadow,
            expected_profile_contract_sha256=sha256_bytes(local_schema),
        )
        contract_hash = timed_speech_registry_contract_sha256(local_schema)
    except (AuthorityProfileSourceError, TimedSpeechContractError, RecursionError):
        raise LocalRunResourceError("installed profile source closure is invalid") from None
    reference = local_run.predecessor_shadow_profile
    if (reference.profile_version, reference.source_sha256, reference.registry_set_sha256, reference.authority_lock_sha256) != (
        shadow.profile_version, shadow.source_sha256, old_hash, old_lock,
    ) or local_run.timed_speech_registry_entry.registry_contract_sha256 != contract_hash:
        raise LocalRunResourceError("installed predecessor or timed-speech contract identity does not close")
    return LocalRunResource(current_hash, current_lock, old_hash, old_lock, narrative, shadow, local_run)


def load_installed_local_run_resource() -> LocalRunResource:
    """Read only the two fixed package resources; never accept a path or selector."""
    try:
        root = resources.files("autocut_kernel").joinpath("_authority")
        with root.joinpath("local-run.sha256").open("rb") as stream:
            digest_raw = stream.read(73)
        if len(digest_raw) != 72 or not digest_raw.endswith(b"\n"):
            raise LocalRunResourceError("installed resource digest file has invalid framing")
        expected = _hash(digest_raw[:-1].decode("ascii"))
        with root.joinpath("local-run.json").open("rb") as stream:
            raw = stream.read(_MAX_RESOURCE_BYTES + 1)
    except (OSError, UnicodeError, ModuleNotFoundError):
        raise LocalRunResourceError("installed local-run resource is unavailable or unreadable") from None
    return decode_local_run_resource(raw, expected_sha256=expected)
