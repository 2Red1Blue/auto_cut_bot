"""Errors raised when deterministic contract compilation cannot proceed."""


class ContractCompilerError(ValueError):
    """Base error for invalid compiler inputs or malformed compiler output."""


class CanonicalizationError(ContractCompilerError):
    """Raised when a value cannot be represented by the canonical JSON subset."""


class SourceMetadataError(ContractCompilerError):
    """Raised when source provenance metadata is missing or ambiguous."""


class AuthorityIntegrityError(ContractCompilerError):
    """Raised when a machine source no longer matches its Markdown authority."""


class GeneratedTreeOwnershipError(ContractCompilerError):
    """Raised when a compiler would overwrite a tree it does not own."""


class GeneratedTreeDriftError(ContractCompilerError):
    """Raised when generated files do not exactly match the expected snapshot."""
