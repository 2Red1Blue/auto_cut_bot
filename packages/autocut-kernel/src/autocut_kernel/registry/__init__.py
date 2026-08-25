"""Authority-owned immutable registry projections.

Runtime code imports resolvers from this package; it never receives registry
payloads or committed-member references from an external run request.
"""

from .timed_speech import (
    AUTHORITY_BOOTSTRAP_CAPABILITY,
    AUTHORITY_BOOTSTRAP_PRINCIPAL,
    BOOTSTRAP_TIMED_SPEECH_PROFILE_REGISTRY_COMMAND,
    DEFAULT_TIMED_SPEECH_AUTHORITY_SNAPSHOT,
    AuthorityBootstrapIdentity,
    AuthorityRegistrySnapshot,
    BootstrappedTimedSpeechProfile,
    BootstrapTimedSpeechProfileRegistryCommand,
    BootstrapTimedSpeechProfileRegistryRequest,
    StoreAnchoredTimedSpeechProfileResolver,
    TimedSpeechProfileKey,
)

__all__ = [
    "AUTHORITY_BOOTSTRAP_CAPABILITY",
    "AUTHORITY_BOOTSTRAP_PRINCIPAL",
    "BOOTSTRAP_TIMED_SPEECH_PROFILE_REGISTRY_COMMAND",
    "DEFAULT_TIMED_SPEECH_AUTHORITY_SNAPSHOT",
    "AuthorityBootstrapIdentity",
    "AuthorityRegistrySnapshot",
    "BootstrapTimedSpeechProfileRegistryCommand",
    "BootstrapTimedSpeechProfileRegistryRequest",
    "BootstrappedTimedSpeechProfile",
    "StoreAnchoredTimedSpeechProfileResolver",
    "TimedSpeechProfileKey",
]
