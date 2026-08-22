"""Stable public API for deterministic contract compilation."""

from .compiler.canonical import canonical_json_bytes, canonical_json_hash
from .compiler.errors import (
    ContractCompilerError,
    GeneratedTreeDriftError,
    GeneratedTreeOwnershipError,
)
from .compiler.generated import check_generated_tree, write_generated_tree
from .compiler.manifest import HashManifest
from .compiler.semantics import SourceClockBinding, validate_source_span_temporal_semantics
from .compiler.source import ContractPath, SourceInput, SourceMetadata, load_json_source

__all__ = [
    "ContractCompilerError",
    "ContractPath",
    "GeneratedTreeDriftError",
    "GeneratedTreeOwnershipError",
    "HashManifest",
    "SourceInput",
    "SourceClockBinding",
    "SourceMetadata",
    "canonical_json_bytes",
    "canonical_json_hash",
    "check_generated_tree",
    "load_json_source",
    "validate_source_span_temporal_semantics",
    "write_generated_tree",
]
