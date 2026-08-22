"""Shared Command request shell validation from total contract §3.5."""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from .errors import CommandValidationError
from .refs import ArtifactRef
from .scope import ScopeIdentity, scope_identity

_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SEMVER = re.compile(r"0|[1-9][0-9]*\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
_FIELDS = frozenset(
    {
        "command_id",
        "command_name",
        "command_version",
        "run_manifest_ref",
        "scope",
        "input_refs",
        "policy_refs",
        "parameters",
        "invocation_id",
        "idempotency_key",
        "requested_capability",
    }
)
_CAPABILITIES = frozenset(
    {
        "media.prepare",
        "run.start",
        "runtime.execute_stage",
        "runtime.advance_usage",
        "runtime.execute_recovery",
        "release.evaluate_story_terminal",
        "release.evaluate_portfolio",
        "release.plan_batch",
        "release.prepare_batch",
        "release.commit_batch",
        "release.abort_batch",
        "release.reconcile_batch",
        "release.publish_independent",
        "run.finalize",
    }
)
_NO_RUN_MANIFEST = frozenset({"PrepareMediaEvidence", "StartRun"})


@dataclass(frozen=True, slots=True)
class CommandRequest:
    """A parsed, non-authorizing shared Command request.

    Profile resolution, caller authorization, idempotency-key derivation and
    all request/result role semantics belong to Dispatcher/Registry packs. This
    shell only establishes that a Runtime cannot smuggle unknown fields or
    ad-hoc parameter values across the shared command boundary.
    """

    command_id: str
    command_name: str
    command_version: str
    run_manifest_ref: ArtifactRef | None
    scope: Mapping[str, object]
    scope_identity: ScopeIdentity
    input_refs: tuple[ArtifactRef, ...]
    policy_refs: tuple[ArtifactRef, ...]
    parameters: Mapping[str, object]
    invocation_id: str
    idempotency_key: str
    requested_capability: str

    @classmethod
    def from_mapping(cls, value: object) -> "CommandRequest":
        mapping = _exact_mapping(value)
        command_name = _text(mapping, "command_name")
        command_version = _text(mapping, "command_version")
        if not _SEMVER.fullmatch(command_version):
            raise CommandValidationError("command_version must be SemVer")
        run_manifest_ref = _nullable_artifact_ref(mapping["run_manifest_ref"])
        raw_scope = mapping["scope"]
        identity = scope_identity(raw_scope)
        scope_kind = raw_scope["kind"]  # `scope_identity` has already proven the closed mapping shape.
        parameters = _parameters(mapping["parameters"], command_name=command_name)
        if command_name in _NO_RUN_MANIFEST:
            if run_manifest_ref is not None:
                raise CommandValidationError(f"{command_name} requires run_manifest_ref=null")
            allowed = {"root_input"} if command_name == "PrepareMediaEvidence" else {"root_input", "job"}
            if scope_kind not in allowed:
                raise CommandValidationError(f"{command_name} has an invalid bootstrap scope")
        elif run_manifest_ref is None:
            raise CommandValidationError("non-bootstrap command requires a run_manifest_ref")
        capability = _text(mapping, "requested_capability")
        if capability not in _CAPABILITIES:
            raise CommandValidationError("requested_capability is not a closed command capability")
        return cls(
            command_id=_text(mapping, "command_id"),
            command_name=command_name,
            command_version=command_version,
            run_manifest_ref=run_manifest_ref,
            scope=_frozen_mapping(raw_scope),
            scope_identity=identity,
            input_refs=_artifact_refs(mapping, "input_refs"),
            policy_refs=_artifact_refs(mapping, "policy_refs"),
            parameters=_frozen_mapping(parameters),
            invocation_id=_text(mapping, "invocation_id"),
            idempotency_key=_sha256(mapping, "idempotency_key"),
            requested_capability=capability,
        )


def _exact_mapping(value: object) -> Mapping[str, object]:
    if type(value) is not dict or set(value) != _FIELDS:  # noqa: E721
        raise CommandValidationError(f"command request must have exactly {sorted(_FIELDS)}")
    return value


def _text(mapping: Mapping[str, object], key: str) -> str:
    value = mapping[key]
    if type(value) is not str or not value:  # noqa: E721
        raise CommandValidationError(f"{key} must be a non-empty string")
    try:
        value.encode("utf-8", "strict")
    except UnicodeEncodeError as error:
        raise CommandValidationError(f"{key} must be a non-empty UTF-8 string") from error
    return value


def _sha256(mapping: Mapping[str, object], key: str) -> str:
    value = _text(mapping, key)
    if not _SHA256.fullmatch(value):
        raise CommandValidationError(f"{key} must be a lowercase sha256 digest")
    return value


def _nullable_artifact_ref(value: object) -> ArtifactRef | None:
    if value is None:
        return None
    try:
        return ArtifactRef.from_mapping(value)
    except ValueError as error:
        raise CommandValidationError("run_manifest_ref must be null or an ArtifactRef") from error


def _artifact_refs(mapping: Mapping[str, object], key: str) -> tuple[ArtifactRef, ...]:
    value = mapping[key]
    if type(value) is not list:  # noqa: E721
        raise CommandValidationError(f"{key} must be an array")
    try:
        return tuple(ArtifactRef.from_mapping(item) for item in value)
    except ValueError as error:
        raise CommandValidationError(f"{key} must contain ArtifactRef values") from error


def _parameters(value: object, *, command_name: str) -> Mapping[str, object]:
    if type(value) is not dict:  # noqa: E721
        raise CommandValidationError("parameters must be an object")
    if command_name not in {"RecoverScope", "MigrateLegacyArtifacts"} and value:
        raise CommandValidationError("v2.1.3 commands require closed empty parameters")
    expected = (
        {"kind", "strategy_id", "strategy_version", "recovery_reservation_ref", "strategy_parameters"}
        if command_name == "RecoverScope"
        else {"source_contract_version", "target_contract_version", "migration_policy_ref"}
        if command_name == "MigrateLegacyArtifacts"
        else set()
    )
    if command_name in {"RecoverScope", "MigrateLegacyArtifacts"} and set(value) != expected:
        raise CommandValidationError(f"{command_name}.parameters must have exactly {sorted(expected)}")
    return value


def _frozen_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType({key: _freeze_json(member) for key, member in value.items()})


def _freeze_json(value: object) -> object:
    if type(value) is dict:  # noqa: E721 - freeze the JSON wire value, not mapping subclasses.
        return _frozen_mapping(value)
    if type(value) is list:  # noqa: E721
        return tuple(_freeze_json(member) for member in value)
    return value
