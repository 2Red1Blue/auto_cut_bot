"""Content-bound semantic member/object identities, without commit authority.

These values work before persistence and contain no database-generated IDs.
Projecting a committed reference discards Receipt/ArtifactSet provenance; it is
not a verification operation. Consumers must separately establish commitment,
object existence, canonical ownership, and any required admission.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import cast

from ..contracts.compiler.canonical import canonical_json_hash
from ..store.errors import StoreValidationError
from ..store.models import (
    ArtifactMember,
    ArtifactScope,
    CommittedArtifactMemberReference,
    canonical_payload_hash,
)

_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MAX_SAFE_INTEGER = 2**53 - 1


class SemanticReferenceError(ValueError):
    """A semantic identity is malformed or does not bind its supplied payload."""


def _text(value: object) -> str:
    if type(value) is not str or not value.strip():  # noqa: E721
        raise SemanticReferenceError("semantic reference text must be non-empty UTF-8")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise SemanticReferenceError("semantic reference text must be non-empty UTF-8") from error
    return value


def _closed(value: object, keys: frozenset[str]) -> dict[str, object]:
    if type(value) is not dict:  # noqa: E721
        raise SemanticReferenceError("semantic reference must be a closed object")
    mapping = cast(dict[str, object], value)
    if any(type(key) is not str for key in mapping) or frozenset(mapping) != keys:  # noqa: E721
        raise SemanticReferenceError("semantic reference has missing or unknown fields")
    return mapping


@dataclass(frozen=True, slots=True)
class SemanticMemberIdentity:
    artifact_type: str
    logical_id: str
    revision: int
    scope: ArtifactScope
    content_hash: str

    def __post_init__(self) -> None:
        _text(self.artifact_type)
        _text(self.logical_id)
        if type(self.revision) is not int or not 1 <= self.revision <= _MAX_SAFE_INTEGER:  # noqa: E721
            raise SemanticReferenceError("semantic member revision must be a positive safe integer")
        if type(self.scope) is not ArtifactScope:  # noqa: E721
            raise SemanticReferenceError("semantic member scope must be an exact ArtifactScope")
        for value in (self.scope.namespace, self.scope.kind, self.scope.key):
            _text(value)
        if _SHA256.fullmatch(_text(self.content_hash)) is None:
            raise SemanticReferenceError("semantic member content_hash must be lowercase sha256")

    @classmethod
    def from_mapping(cls, value: object) -> SemanticMemberIdentity:
        mapping = _closed(
            value, frozenset({"artifact_type", "logical_id", "revision", "scope", "content_hash"})
        )
        scope = _closed(mapping["scope"], frozenset({"namespace", "kind", "key"}))
        # Cast only transports untrusted fields to the constructor; __post_init__
        # checks their actual runtime types before they can become valid values.
        return cls(
            cast(str, mapping["artifact_type"]),
            cast(str, mapping["logical_id"]),
            cast(int, mapping["revision"]),
            ArtifactScope(_text(scope["namespace"]), _text(scope["kind"]), _text(scope["key"])),
            cast(str, mapping["content_hash"]),
        )

    @classmethod
    def from_artifact_member(cls, member: ArtifactMember) -> SemanticMemberIdentity:
        """Project a member after checking Store-owned canonical payload hashing."""
        if type(member) is not ArtifactMember:  # noqa: E721
            raise SemanticReferenceError(
                "semantic member projection requires an exact ArtifactMember"
            )
        identity = cls(
            member.artifact_type,
            member.logical_id,
            member.revision,
            member.scope,
            member.content_hash,
        )
        try:
            payload_hash = canonical_payload_hash(member.payload_json)
        except (StoreValidationError, ValueError, RecursionError) as error:
            raise SemanticReferenceError(
                "semantic member payload cannot be canonically hashed"
            ) from error
        if payload_hash != identity.content_hash:
            raise SemanticReferenceError("semantic member payload does not match content_hash")
        return identity

    @classmethod
    def from_committed_member_reference(
        cls, reference: CommittedArtifactMemberReference
    ) -> SemanticMemberIdentity:
        """Value projection only: does not read or verify persisted commitment."""
        if type(reference) is not CommittedArtifactMemberReference:  # noqa: E721
            raise SemanticReferenceError(
                "semantic member projection requires an exact committed member reference"
            )
        return cls(
            reference.artifact_type,
            reference.logical_id,
            reference.revision,
            reference.scope,
            reference.content_hash,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "artifact_type": self.artifact_type,
            "logical_id": self.logical_id,
            "revision": self.revision,
            "content_hash": self.content_hash,
            "scope": {
                "namespace": self.scope.namespace,
                "kind": self.scope.kind,
                "key": self.scope.key,
            },
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())


@dataclass(frozen=True, slots=True)
class SemanticObjectRef:
    member_ref: SemanticMemberIdentity
    object_type: str
    object_id: str

    def __post_init__(self) -> None:
        if type(self.member_ref) is not SemanticMemberIdentity:  # noqa: E721
            raise SemanticReferenceError(
                "semantic object owner must be an exact SemanticMemberIdentity"
            )
        _text(self.object_type)
        _text(self.object_id)

    @classmethod
    def from_mapping(cls, value: object) -> SemanticObjectRef:
        mapping = _closed(value, frozenset({"member_ref", "object_type", "object_id"}))
        return cls(
            SemanticMemberIdentity.from_mapping(mapping["member_ref"]),
            cast(str, mapping["object_type"]),
            cast(str, mapping["object_id"]),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "member_ref": self.member_ref.to_mapping(),
            "object_type": self.object_type,
            "object_id": self.object_id,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())
