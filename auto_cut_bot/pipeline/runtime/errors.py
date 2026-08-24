"""Closed failures exposed by the pipeline HTTP control plane."""


class PipelineRunError(Exception):
    """Base class for typed pipeline run failures."""


class PipelineRunValidationError(PipelineRunError, ValueError):
    """A request or persisted projection violates the closed contract."""


class IdempotencyConflictError(PipelineRunError):
    """An idempotency key is already bound to another canonical request."""


class PipelineRunNotFoundError(PipelineRunError):
    """The requested durable run does not exist."""


class ResumeNotAllowedError(PipelineRunError):
    """The run has no pending command that this slice may resume safely."""


class StaleRunVersionError(PipelineRunError):
    """A caller attempted a run transition with a stale expected version."""


class SourceDeniedError(PipelineRunError):
    """The configured source authority rejected the request."""
