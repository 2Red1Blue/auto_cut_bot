"""Strict source provenance records for manually transcribed contract inputs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .canonical import load_canonical_json_bytes, sha256_bytes
from .errors import SourceMetadataError

_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class ContractPath:
    """A stable, non-ambiguous pointer into a frozen authority Markdown file."""

    document: str
    anchor: str

    def __post_init__(self) -> None:
        _validate_relative_path(self.document, field="contract_path.document")
        if not self.document.endswith(".md"):
            raise SourceMetadataError("contract_path.document must name a Markdown authority document")
        if not self.anchor.startswith("#") or len(self.anchor) == 1 or any(char.isspace() for char in self.anchor):
            raise SourceMetadataError("contract_path.anchor must be a non-empty whitespace-free fragment")

    @classmethod
    def from_mapping(cls, value: object) -> "ContractPath":
        mapping = _exact_mapping(value, {"document", "anchor"}, field="contract_path")
        return cls(document=_required_text(mapping, "document"), anchor=_required_text(mapping, "anchor"))

    def to_mapping(self) -> dict[str, str]:
        return {"document": self.document, "anchor": self.anchor}


@dataclass(frozen=True, slots=True)
class SourceMetadata:
    """Provenance required for every editable machine-source record."""

    contract_path: ContractPath
    source_document_sha256: str
    reviewer: str

    def __post_init__(self) -> None:
        if not _SHA256.fullmatch(self.source_document_sha256):
            raise SourceMetadataError("source_document_sha256 must be a lowercase sha256: digest")
        if not self.reviewer or self.reviewer != self.reviewer.strip() or any(
            char in self.reviewer for char in "\r\n"
        ):
            raise SourceMetadataError("reviewer must be a non-empty single-line identifier")

    @classmethod
    def from_mapping(cls, value: object) -> "SourceMetadata":
        mapping = _exact_mapping(
            value,
            {"contract_path", "source_document_sha256", "reviewer"},
            field="source_metadata",
        )
        return cls(
            contract_path=ContractPath.from_mapping(mapping["contract_path"]),
            source_document_sha256=_required_text(mapping, "source_document_sha256"),
            reviewer=_required_text(mapping, "reviewer"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "contract_path": self.contract_path.to_mapping(),
            "source_document_sha256": self.source_document_sha256,
            "reviewer": self.reviewer,
        }


@dataclass(frozen=True, slots=True)
class SourceInput:
    """Canonical source bytes and the authority provenance that licensed them."""

    path: str
    canonical_json: bytes
    metadata: SourceMetadata

    def __post_init__(self) -> None:
        _validate_relative_path(self.path, field="source.path")
        if not self.path.endswith(".json"):
            raise SourceMetadataError("source.path must name a JSON machine-source file")
        # Reparse to prove callers cannot construct a SourceInput from arbitrary bytes.
        _, canonical = load_canonical_json_bytes(self.canonical_json, origin=self.path)
        if canonical != self.canonical_json:
            raise SourceMetadataError("source.canonical_json must already be canonical JSON bytes")

    @property
    def sha256(self) -> str:
        return sha256_bytes(self.canonical_json)

    @classmethod
    def from_json_bytes(cls, *, path: str, raw: bytes, metadata: SourceMetadata) -> "SourceInput":
        _, canonical = load_canonical_json_bytes(raw, origin=path)
        return cls(path=path, canonical_json=canonical, metadata=metadata)


def load_json_source(path: Path, *, relative_path: str, metadata: SourceMetadata) -> SourceInput:
    """Load one JSON source without accepting YAML or implicit parser defaults.

    YAML support will be introduced only with a dependency-free, contract-specified
    YAML profile.  Treating a broad YAML parser as a foundation default would
    introduce unreviewed coercion and duplicate-key semantics.
    """

    if path.is_symlink() or not path.is_file():
        raise SourceMetadataError(f"{path}: source must be a regular non-symlink file")
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise SourceMetadataError(f"{path}: unable to read source") from error
    return SourceInput.from_json_bytes(path=relative_path, raw=raw, metadata=metadata)


def _exact_mapping(value: object, expected: set[str], *, field: str) -> dict[str, object]:
    if type(value) is not dict:  # noqa: E721 - subclasses can hide fields.
        raise SourceMetadataError(f"{field} must be an object")
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        details = []
        if missing:
            details.append(f"missing={missing}")
        if unknown:
            details.append(f"unknown={unknown}")
        raise SourceMetadataError(f"{field} must have exactly {sorted(expected)} ({', '.join(details)})")
    return value


def _required_text(mapping: dict[str, object], field: str) -> str:
    value = mapping[field]
    if type(value) is not str or not value:  # noqa: E721 - reject string subclasses and empty text.
        raise SourceMetadataError(f"{field} must be a non-empty string")
    return value


def _validate_relative_path(value: str, *, field: str) -> None:
    if not value or "\\" in value:
        raise SourceMetadataError(f"{field} must be a non-empty POSIX relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise SourceMetadataError(f"{field} must not contain absolute or traversal segments")
