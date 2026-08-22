"""Stable public API for deterministic contract compilation."""

from .compiler.authority import verify_source_authority
from .compiler.canonical import canonical_json_bytes, canonical_json_hash
from .compiler.commands import CommandRequest
from .compiler.errors import (
    AuthorityIntegrityError,
    CommandValidationError,
    ContractCompilerError,
    GeneratedTreeDriftError,
    GeneratedTreeOwnershipError,
    ReferenceValidationError,
    RegistryValidationError,
)
from .compiler.generated import check_generated_tree, write_generated_tree
from .compiler.manifest import HashManifest
from .compiler.refs import (
    ArtifactRef,
    ArtifactSetRef,
    DomainRef,
    ImmutableBlobRef,
    verify_immutable_blob_bytes,
)
from .compiler.registry import (
    CommandContractProfile,
    ContractTrace,
    PartialRegistrySet,
    RegistrySet,
)
from .compiler.scope import ScopeIdentity, scope_identity
from .compiler.semantics import SourceClockBinding, validate_source_span_temporal_semantics
from .compiler.source import ContractPath, SourceInput, SourceMetadata, load_json_source

__all__ = [
    "ContractCompilerError",
    "AuthorityIntegrityError",
    "ArtifactRef",
    "ArtifactSetRef",
    "CommandContractProfile",
    "CommandRequest",
    "CommandValidationError",
    "ContractPath",
    "ContractTrace",
    "DomainRef",
    "GeneratedTreeDriftError",
    "GeneratedTreeOwnershipError",
    "HashManifest",
    "ImmutableBlobRef",
    "PartialRegistrySet",
    "RegistryValidationError",
    "RegistrySet",
    "ReferenceValidationError",
    "SourceInput",
    "SourceClockBinding",
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
