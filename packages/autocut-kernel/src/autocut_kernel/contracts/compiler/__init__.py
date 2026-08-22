"""Implementation modules for the dependency-free contract compiler."""

from .authority import verify_source_authority
from .canonical import canonical_json_bytes, canonical_json_hash
from .generated import check_generated_tree, write_generated_tree
from .manifest import HashManifest
from .scope import ScopeIdentity, scope_identity
from .semantics import SourceClockBinding, validate_source_span_temporal_semantics
from .source import ContractPath, SourceInput, SourceMetadata, load_json_source

__all__ = [
    "ContractPath",
    "HashManifest",
    "SourceClockBinding",
    "SourceInput",
    "SourceMetadata",
    "ScopeIdentity",
    "canonical_json_bytes",
    "canonical_json_hash",
    "check_generated_tree",
    "load_json_source",
    "scope_identity",
    "validate_source_span_temporal_semantics",
    "verify_source_authority",
    "write_generated_tree",
]
