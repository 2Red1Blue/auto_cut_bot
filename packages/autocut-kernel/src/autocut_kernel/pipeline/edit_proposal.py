"""Closed, auditable proposals for a future guarded Recipe edit command.

This module deliberately has no Store, compiler, renderer, or command-side
effects.  It describes only the one edit operation that can currently be
proposed: selecting a stable member of a previously committed
``SpanVariantSet``.  In particular, a proposal cannot smuggle in source ticks,
spans, FFmpeg arguments, or an uncommitted logical reference.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final, Literal, Mapping, cast
from uuid import UUID

from ..media.types import MediaValidationError, canonical_sha256, sha256_prefixed
from ..store.errors import StoreValidationError
from ..store.models import CommittedArtifactMemberReference

EDIT_PROPOSAL_SCHEMA_VERSION: Final = "edit-proposal-v1"
SELECT_VARIANT_OPERATION: Final = "select_variant"
MAX_EDIT_PROPOSAL_PAYLOAD_BYTES: Final = 64 * 1024

_MAX_TEXT_BYTES: Final = 4_096
_SAFE_ACTOR_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}\Z")
_ACTOR_KINDS: Final = frozenset({"human", "agent", "system"})


class EditProposalError(ValueError):
    """An edit proposal is malformed, non-canonical, or outside R4B authority."""


def _object(value: object, fields: tuple[str, ...], label: str) -> Mapping[str, object]:
    if type(value) is not dict:  # noqa: E721
        raise EditProposalError(f"{label} must be a closed object")
    raw = cast(dict[object, object], value)
    if any(type(key) is not str for key in raw) or set(raw) != set(fields):  # noqa: E721
        raise EditProposalError(f"{label} has missing or unknown fields")
    return cast(Mapping[str, object], raw)


def _array(value: object, label: str) -> list[object]:
    if type(value) is not list:  # noqa: E721
        raise EditProposalError(f"{label} must be an array")
    return cast(list[object], value)


def _text(value: object, label: str, *, maximum: int = _MAX_TEXT_BYTES) -> str:
    if type(value) is not str or not value or value != value.strip():  # noqa: E721
        raise EditProposalError(f"{label} must be non-empty canonical text")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise EditProposalError(f"{label} must be valid UTF-8") from error
    if len(encoded) > maximum:
        raise EditProposalError(f"{label} exceeds its byte bound")
    return value


def _hash(value: object, label: str) -> str:
    try:
        result = sha256_prefixed(value, label)
    except MediaValidationError as error:
        raise EditProposalError(str(error)) from error
    if result == "sha256:" + "0" * 64:
        raise EditProposalError(f"{label} must not be the all-zero digest")
    return result


def _revision(value: object, label: str) -> int:
    if type(value) is not int or not 1 <= value <= 2**53 - 1:  # noqa: E721
        raise EditProposalError(f"{label} must be a positive portable integer")
    return value


def _timestamp(value: object) -> str:
    text = _text(value, "created_at", maximum=32)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise EditProposalError("created_at must be an RFC 3339 UTC timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise EditProposalError("created_at must use the UTC Z suffix")
    if parsed.microsecond or parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != text:
        raise EditProposalError("created_at must be canonical UTC whole seconds")
    return text


def _reference(value: object, label: str) -> CommittedArtifactMemberReference:
    try:
        return CommittedArtifactMemberReference.from_mapping(value)
    except (StoreValidationError, TypeError, ValueError) as error:
        raise EditProposalError(f"{label} is not an exact committed member reference") from error


@dataclass(frozen=True, slots=True)
class EditProposalActor:
    """A non-secret actor identity kept separate from the requested edit."""

    kind: Literal["human", "agent", "system"]
    actor_id: str

    def __post_init__(self) -> None:
        if self.kind not in _ACTOR_KINDS:
            raise EditProposalError("created_by.kind is unsupported")
        if type(self.actor_id) is not str or _SAFE_ACTOR_ID.fullmatch(self.actor_id) is None:  # noqa: E721
            raise EditProposalError("created_by.actor_id must be a bounded safe identifier")

    def to_mapping(self) -> dict[str, str]:
        return {"kind": self.kind, "actor_id": self.actor_id}


@dataclass(frozen=True, slots=True)
class SelectVariantOperation:
    """Select one stable variant from one exact committed variant-set member."""

    span_variant_set_ref: CommittedArtifactMemberReference
    variant_id: str
    operation: Literal["select_variant"] = SELECT_VARIANT_OPERATION

    def __post_init__(self) -> None:
        if self.operation != SELECT_VARIANT_OPERATION:
            raise EditProposalError("only select_variant operations are supported")
        if type(self.span_variant_set_ref) is not CommittedArtifactMemberReference:  # noqa: E721
            raise EditProposalError("select_variant requires an exact committed SpanVariantSet member")
        reference = self.span_variant_set_ref
        if (
            reference.member_ordinal != 0
            or reference.artifact_type != "span_variant_set"
            or reference.logical_id != "span_variant_set"
        ):
            raise EditProposalError("select_variant requires the canonical SpanVariantSet member")
        _hash(reference.content_hash, "span_variant_set_ref.content_hash")
        _hash(self.variant_id, "variant_id")

    @property
    def span_variant_set_content_hash(self) -> str:
        """The immutable content identity is part of the full member reference."""
        return self.span_variant_set_ref.content_hash

    def to_mapping(self) -> dict[str, object]:
        return {
            "operation": self.operation,
            "span_variant_set_ref": self.span_variant_set_ref.to_mapping(),
            "variant_id": self.variant_id,
        }


@dataclass(frozen=True, slots=True)
class EditProposal:
    """A closed proposal, not an authorization to mutate a Recipe.

    ``created_at`` is audit metadata only.  It is intentionally excluded from
    :attr:`content_identity_mapping` and :attr:`canonical_hash`, so retrying an
    identical proposal at a different time cannot alter its business identity.
    ``proposal_id`` itself is the idempotency-safe identity; future commands
    must still bind it to this content hash before performing CAS.
    """

    proposal_id: UUID
    base_recipe_ref: CommittedArtifactMemberReference
    base_revision: int
    base_recipe_set_hash: str
    edit_ops: tuple[SelectVariantOperation, ...]
    reason: str
    created_by: EditProposalActor
    created_at: str
    schema_version: str = EDIT_PROPOSAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.proposal_id, UUID):
            raise EditProposalError("proposal_id must be a UUID")
        if self.schema_version != EDIT_PROPOSAL_SCHEMA_VERSION:
            raise EditProposalError("edit proposal schema is unsupported")
        if type(self.base_recipe_ref) is not CommittedArtifactMemberReference:  # noqa: E721
            raise EditProposalError("proposal requires an exact committed base Recipe reference")
        if (
            self.base_recipe_ref.artifact_type != "recipe"
            or not self.base_recipe_ref.logical_id.startswith("production_recipe@")
        ):
            raise EditProposalError("base_recipe_ref must identify a committed production Recipe")
        if _revision(self.base_revision, "base_revision") != self.base_recipe_ref.revision:
            raise EditProposalError("base_revision differs from base_recipe_ref.revision")
        _hash(self.base_recipe_set_hash, "base_recipe_set_hash")
        if (
            type(self.edit_ops) is not tuple  # noqa: E721
            or len(self.edit_ops) != 1
            or type(self.edit_ops[0]) is not SelectVariantOperation  # noqa: E721
        ):
            raise EditProposalError("edit_ops must contain exactly one select_variant operation")
        _text(self.reason, "reason")
        if type(self.created_by) is not EditProposalActor:  # noqa: E721
            raise EditProposalError("created_by must be an exact EditProposalActor")
        _timestamp(self.created_at)

    @property
    def idempotency_key(self) -> str:
        """A deterministic, non-secret command identity derived from proposal_id."""
        return f"edit-proposal:{self.proposal_id}"

    def content_identity_mapping(self) -> dict[str, object]:
        """The semantic proposal mapping, deliberately excluding audit time."""
        return {
            "schema_version": self.schema_version,
            "proposal_id": str(self.proposal_id),
            "base_recipe_ref": self.base_recipe_ref.to_mapping(),
            "base_revision": self.base_revision,
            "base_recipe_set_hash": self.base_recipe_set_hash,
            "edit_ops": [item.to_mapping() for item in self.edit_ops],
            "reason": self.reason,
            "created_by": self.created_by.to_mapping(),
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.content_identity_mapping())

    def to_mapping(self) -> dict[str, object]:
        return {**self.content_identity_mapping(), "created_at": self.created_at}


def _decode_actor(value: object) -> EditProposalActor:
    raw = _object(value, ("kind", "actor_id"), "created_by")
    kind = _text(raw["kind"], "created_by.kind", maximum=16)
    return EditProposalActor(cast(Literal["human", "agent", "system"], kind), _text(raw["actor_id"], "created_by.actor_id", maximum=128))


def _decode_select_variant(value: object) -> SelectVariantOperation:
    raw = _object(value, ("operation", "span_variant_set_ref", "variant_id"), "edit operation")
    if raw["operation"] != SELECT_VARIANT_OPERATION:
        raise EditProposalError("only select_variant operations are supported")
    return SelectVariantOperation(
        _reference(raw["span_variant_set_ref"], "span_variant_set_ref"),
        _hash(raw["variant_id"], "variant_id"),
    )


def decode_edit_proposal(value: object) -> EditProposal:
    """Decode one strict in-memory ``EditProposal/v1`` mapping."""
    raw = _object(
        value,
        (
            "schema_version",
            "proposal_id",
            "base_recipe_ref",
            "base_revision",
            "base_recipe_set_hash",
            "edit_ops",
            "reason",
            "created_by",
            "created_at",
        ),
        "edit proposal",
    )
    if raw["schema_version"] != EDIT_PROPOSAL_SCHEMA_VERSION:
        raise EditProposalError("edit proposal schema is unsupported")
    if type(raw["proposal_id"]) is not str:  # noqa: E721
        raise EditProposalError("proposal_id must be a UUID")
    try:
        proposal_id = UUID(raw["proposal_id"])
    except ValueError as error:
        raise EditProposalError("proposal_id must be a UUID") from error
    return EditProposal(
        proposal_id,
        _reference(raw["base_recipe_ref"], "base_recipe_ref"),
        _revision(raw["base_revision"], "base_revision"),
        _hash(raw["base_recipe_set_hash"], "base_recipe_set_hash"),
        tuple(_decode_select_variant(item) for item in _array(raw["edit_ops"], "edit_ops")),
        _text(raw["reason"], "reason"),
        _decode_actor(raw["created_by"]),
        _timestamp(raw["created_at"]),
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise EditProposalError("duplicate JSON key")
        result[key] = value
    return result


def _reject_number(value: str) -> object:
    raise EditProposalError(f"unsupported JSON number {value}")


def _parse_integer(value: str) -> int:
    if len(value) > 17:
        raise EditProposalError("JSON integer exceeds the portable digit bound")
    return int(value)


def decode_edit_proposal_json(
    raw: bytes,
    *,
    max_bytes: int = MAX_EDIT_PROPOSAL_PAYLOAD_BYTES,
) -> EditProposal:
    """Decode bounded strict UTF-8 JSON with no duplicate keys or floats."""
    if type(raw) is not bytes:  # noqa: E721
        raise EditProposalError("edit proposal payload must be exact bytes")
    if type(max_bytes) is not int or max_bytes < 1:  # noqa: E721
        raise EditProposalError("max_bytes must be a positive integer")
    if not raw or len(raw) > max_bytes:
        raise EditProposalError("edit proposal payload exceeds its explicit byte bound")
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_int=_parse_integer,
            parse_float=_reject_number,
            parse_constant=_reject_number,
        )
    except EditProposalError:
        raise
    except (ValueError, UnicodeError, RecursionError) as error:
        raise EditProposalError("edit proposal must be bounded strict UTF-8 JSON") from error
    return decode_edit_proposal(value)


def encode_edit_proposal_json(value: EditProposal) -> bytes:
    """Encode a proposal as deterministic, strict canonical JSON bytes."""
    if type(value) is not EditProposal:  # noqa: E721
        raise EditProposalError("only an exact EditProposal can be encoded")
    try:
        encoded = json.dumps(
            value.to_mapping(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise EditProposalError("edit proposal is not canonical finite JSON") from error
    if len(encoded) > MAX_EDIT_PROPOSAL_PAYLOAD_BYTES:
        raise EditProposalError("edit proposal canonical payload exceeds its byte bound")
    return encoded


__all__ = (
    "EDIT_PROPOSAL_SCHEMA_VERSION",
    "MAX_EDIT_PROPOSAL_PAYLOAD_BYTES",
    "SELECT_VARIANT_OPERATION",
    "EditProposal",
    "EditProposalActor",
    "EditProposalError",
    "SelectVariantOperation",
    "decode_edit_proposal",
    "decode_edit_proposal_json",
    "encode_edit_proposal_json",
)
