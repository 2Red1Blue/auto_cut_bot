"""Implementation modules for the dependency-free contract compiler."""

from .authority import verify_source_authority
from .canonical import canonical_json_bytes, canonical_json_hash
from .commands import CommandRequest
from .generated import check_generated_tree, write_generated_tree
from .manifest import HashManifest
from .refs import (
    ArtifactRef,
    ArtifactSetRef,
    DomainRef,
    ImmutableBlobRef,
    verify_immutable_blob_bytes,
)
from .registry import CommandContractProfile, ContractTrace, PartialRegistrySet, RegistrySet
from .scope import ScopeIdentity, scope_identity
from .semantics import SourceClockBinding, validate_source_span_temporal_semantics
from .source import ContractPath, SourceInput, SourceMetadata, load_json_source

__all__ = [
    "ContractPath",
    "CommandContractProfile",
    "CommandRequest",
    "ContractTrace",
    "ArtifactRef",
    "ArtifactSetRef",
    "DomainRef",
    "HashManifest",
    "ImmutableBlobRef",
    "PartialRegistrySet",
    "RegistrySet",
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
    "verify_immutable_blob_bytes",
    "verify_source_authority",
    "write_generated_tree",
]
