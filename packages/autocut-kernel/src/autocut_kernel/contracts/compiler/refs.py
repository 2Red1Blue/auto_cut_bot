"""Typed, closed models for the unambiguous shared reference primitives."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

from .errors import ReferenceValidationError

_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_DECIMAL = re.compile(r"0|[1-9][0-9]*\Z")


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    artifact_id: str
    content_hash: str

    def __post_init__(self) -> None:
        _valid_text(self.artifact_id, "artifact_id")
        _valid_sha256(self.content_hash, "content_hash")

    @classmethod
    def from_mapping(cls, value: object) -> "ArtifactRef":
        mapping = _exact_mapping(value, {"artifact_id", "content_hash"}, label="artifact_ref")
        return cls(artifact_id=_text(mapping, "artifact_id"), content_hash=_sha256(mapping, "content_hash"))

    def to_mapping(self) -> dict[str, str]:
        return {"artifact_id": self.artifact_id, "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class ArtifactSetRef:
    artifact_set_id: str
    set_hash: str

    def __post_init__(self) -> None:
        _valid_text(self.artifact_set_id, "artifact_set_id")
        _valid_sha256(self.set_hash, "set_hash")

    @classmethod
    def from_mapping(cls, value: object) -> "ArtifactSetRef":
        mapping = _exact_mapping(value, {"artifact_set_id", "set_hash"}, label="artifact_set_ref")
        return cls(
            artifact_set_id=_text(mapping, "artifact_set_id"), set_hash=_sha256(mapping, "set_hash")
        )

    def to_mapping(self) -> dict[str, str]:
        return {"artifact_set_id": self.artifact_set_id, "set_hash": self.set_hash}


@dataclass(frozen=True, slots=True)
class DomainRef:
    artifact_ref: ArtifactRef
    object_type: str
    object_id: str

    def __post_init__(self) -> None:
        if type(self.artifact_ref) is not ArtifactRef:  # noqa: E721
            raise ReferenceValidationError("domain_ref.artifact_ref must be an ArtifactRef")
        _valid_text(self.object_type, "object_type")
        _valid_text(self.object_id, "object_id")

    @classmethod
    def from_mapping(cls, value: object) -> "DomainRef":
        mapping = _exact_mapping(value, {"artifact_ref", "object_type", "object_id"}, label="domain_ref")
        return cls(
            artifact_ref=ArtifactRef.from_mapping(mapping["artifact_ref"]),
            object_type=_text(mapping, "object_type"),
            object_id=_text(mapping, "object_id"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "artifact_ref": self.artifact_ref.to_mapping(),
            "object_type": self.object_type,
            "object_id": self.object_id,
        }


@dataclass(frozen=True, slots=True)
class ImmutableBlobRef:
    object_id: str
    sha256: str
    storage_locator: str
    media_type: str
    byte_length_decimal: str

    def __post_init__(self) -> None:
        _valid_text(self.object_id, "object_id")
        _valid_sha256(self.sha256, "sha256")
        _valid_text(self.storage_locator, "storage_locator")
        _valid_text(self.media_type, "media_type")
        _valid_text(self.byte_length_decimal, "byte_length_decimal")
        if not _DECIMAL.fullmatch(self.byte_length_decimal):
            raise ReferenceValidationError("byte_length_decimal must be canonical unsigned decimal")

    @classmethod
    def from_mapping(cls, value: object) -> "ImmutableBlobRef":
        mapping = _exact_mapping(
            value,
            {"object_id", "sha256", "storage_locator", "media_type", "byte_length_decimal"},
            label="immutable_blob_ref",
        )
        byte_length = _text(mapping, "byte_length_decimal")
        if not _DECIMAL.fullmatch(byte_length):
            raise ReferenceValidationError("byte_length_decimal must be canonical unsigned decimal")
        return cls(
            object_id=_text(mapping, "object_id"),
            sha256=_sha256(mapping, "sha256"),
            storage_locator=_text(mapping, "storage_locator"),
            media_type=_text(mapping, "media_type"),
            byte_length_decimal=byte_length,
        )

    def to_mapping(self) -> dict[str, str]:
        return {
            "object_id": self.object_id,
            "sha256": self.sha256,
            "storage_locator": self.storage_locator,
            "media_type": self.media_type,
            "byte_length_decimal": self.byte_length_decimal,
        }


def _exact_mapping(value: object, expected: set[str], *, label: str) -> Mapping[str, object]:
    if type(value) is not dict:  # noqa: E721 - mapping subclasses are not a closed wire object.
        raise ReferenceValidationError(f"{label} must be an object")
    if set(value) != expected:
        raise ReferenceValidationError(f"{label} must have exactly {sorted(expected)}")
    return value


def _text(mapping: Mapping[str, object], key: str) -> str:
    value = mapping[key]
    _valid_text(value, key)
    return value


def _sha256(mapping: Mapping[str, object], key: str) -> str:
    value = _text(mapping, key)
    _valid_sha256(value, key)
    return value


def _valid_text(value: object, key: str) -> None:
    if type(value) is not str or not value:  # noqa: E721
        raise ReferenceValidationError(f"{key} must be a non-empty UTF-8 string")
    try:
        value.encode("utf-8", "strict")
    except UnicodeEncodeError as error:
        raise ReferenceValidationError(f"{key} must be a non-empty UTF-8 string") from error


def _valid_sha256(value: object, key: str) -> None:
    _valid_text(value, key)
    if not _SHA256.fullmatch(value):
        raise ReferenceValidationError(f"{key} must be a lowercase sha256 digest")
