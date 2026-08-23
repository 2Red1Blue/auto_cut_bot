"""Typed outcomes for the local Pipeline persistence boundary."""


class RuntimeStoreError(Exception):
    """Base class for persistence errors that callers may handle deliberately."""


class StoreValidationError(RuntimeStoreError):
    """Raised before a malformed command can reach PostgreSQL."""


class StaleHeadError(RuntimeStoreError):
    """A competing command advanced or created the same logical artifact head."""


class CommandStateError(RuntimeStoreError):
    """The requested terminal transition is incompatible with the claimed command."""
