"""Hash manifests binding canonical compiler inputs to generated output."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .canonical import canonical_json_bytes, canonical_json_hash, sha256_bytes
from .errors import ContractCompilerError
from .source import SourceInput

MANIFEST_FORMAT = "autocut-contract-manifest-v1"


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    """One path-addressed digest in a deterministic manifest."""

    path: str
    sha256: str

    def to_mapping(self) -> dict[str, str]:
        return {"path": self.path, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class HashManifest:
    """The reproducible evidence produced for one compiler invocation."""

    compiler_version: str
    sources: tuple[SourceInput, ...]
    generated: tuple[ManifestEntry, ...]

    @classmethod
    def build(
        cls,
        *,
        compiler_version: str,
        sources: tuple[SourceInput, ...],
        generated_files: Mapping[str, bytes],
    ) -> "HashManifest":
        if not compiler_version or compiler_version != compiler_version.strip():
            raise ContractCompilerError("compiler_version must be a non-empty normalized identifier")
        sorted_sources = tuple(sorted(sources, key=lambda source: source.path))
        _ensure_unique_paths((source.path for source in sorted_sources), label="source")
        generated = tuple(
            ManifestEntry(path=path, sha256=sha256_bytes(content))
            for path, content in sorted(generated_files.items())
        )
        _ensure_unique_paths((entry.path for entry in generated), label="generated")
        return cls(compiler_version=compiler_version, sources=sorted_sources, generated=generated)

    @property
    def sha256(self) -> str:
        return canonical_json_hash(self.to_mapping())

    def to_mapping(self) -> dict[str, object]:
        return {
            "compiler_version": self.compiler_version,
            "format": MANIFEST_FORMAT,
            "generated": [entry.to_mapping() for entry in self.generated],
            "sources": [
                {
                    "metadata": source.metadata.to_mapping(),
                    "path": source.path,
                    "sha256": source.sha256,
                }
                for source in self.sources
            ],
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_mapping())


def _ensure_unique_paths(paths: object, *, label: str) -> None:
    seen: set[str] = set()
    for path in paths:  # type: ignore[union-attr]
        if path in seen:
            raise ContractCompilerError(f"duplicate {label} path {path!r}")
        seen.add(path)
