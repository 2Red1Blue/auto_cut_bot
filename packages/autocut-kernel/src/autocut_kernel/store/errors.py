"""Typed outcomes for the local Pipeline persistence boundary."""


class RuntimeStoreError(Exception):
    """Base class for persistence errors that callers may handle deliberately."""


class StoreValidationError(RuntimeStoreError):
    """Raised before a malformed command can reach PostgreSQL."""


class IdempotencyConflictError(RuntimeStoreError):
    """An idempotency key was reused for a different command intent."""


class StaleHeadError(RuntimeStoreError):
    """A competing command advanced or created the same logical artifact head."""


class PersistenceConflictError(RuntimeStoreError):
    """A durable uniqueness constraint rejected a conflicting persistence write."""


class StoreConcurrencyError(RuntimeStoreError):
    """PostgreSQL aborted the transaction due to a retryable concurrency conflict."""


class CommandStateError(RuntimeStoreError):
    """The requested terminal transition is incompatible with the claimed command."""


class BlobIntegrityError(RuntimeStoreError):
    """Blob bytes, digest, length, media type, or durable claim do not agree."""


class GenerationAttemptStateError(CommandStateError):
    """A provider attempt transition is stale, invalid, or would dispatch twice."""


class JobProfileMismatchError(RuntimeStoreError):
    """A durable Job exists, but belongs to a different runtime profile."""


class VlmObservationUnavailableError(RuntimeStoreError):
    """No exact committed VLM observation set is available for a child Command."""


class VlmObservationIntegrityError(RuntimeStoreError):
    """A committed VLM observation set violates its immutable provenance contract."""


class SemanticInputUnavailableError(RuntimeStoreError):
    """An exact committed Source/Window/VLM input identity is unavailable."""


class SemanticInputIntegrityError(RuntimeStoreError):
    """Committed semantic inputs fail member, BlobRef, or owner-join validation."""


class RecipeUnavailableError(RuntimeStoreError):
    """The exact persisted Recipe identity is not available to this Job."""


class RecipeIntegrityError(RuntimeStoreError):
    """A persisted Recipe row does not satisfy its immutable provenance contract."""


class MediaEvidenceUnavailableError(RuntimeStoreError):
    """The exact persisted MediaEvidence identity is not available to this Job."""


class MediaEvidenceIntegrityError(RuntimeStoreError):
    """A MediaEvidence row does not satisfy its immutable provenance contract."""


class MediaOutputsUnavailableError(RuntimeStoreError):
    """No exact succeeded LocalMediaCommand output pair is available for a Job."""


class MediaOutputsIntegrityError(RuntimeStoreError):
    """A succeeded media-output pair violates its shared immutable provenance."""


class SemanticResolutionProofUnavailableError(RuntimeStoreError):
    """No exact succeeded semantic-resolution proof is available for a Job."""


class SemanticResolutionProofIntegrityError(RuntimeStoreError):
    """A semantic-resolution proof violates its immutable ArtifactSet contract."""
