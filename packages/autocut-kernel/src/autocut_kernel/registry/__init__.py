"""Authority-owned immutable registry projections.

Runtime code imports resolvers from this package; it never receives registry
payloads or committed-member references from an external run request.
"""

from .timed_speech import (
    AUTHORITY_BOOTSTRAP_CAPABILITY,
    AUTHORITY_BOOTSTRAP_PRINCIPAL,
    BOOTSTRAP_TIMED_SPEECH_PROFILE_REGISTRY_COMMAND,
    AuthorityBootstrapIdentity,
    AuthorityRegistrySnapshot,
    BootstrappedTimedSpeechProfile,
    BootstrapTimedSpeechProfileRegistryCommand,
    BootstrapTimedSpeechProfileRegistryRequest,
    StoreAnchoredTimedSpeechProfileResolver,
    TimedSpeechProfileKey,
    VerifiedTimedSpeechAuthorityContext,
)

__all__ = [
    "AUTHORITY_BOOTSTRAP_CAPABILITY",
    "AUTHORITY_BOOTSTRAP_PRINCIPAL",
    "BOOTSTRAP_TIMED_SPEECH_PROFILE_REGISTRY_COMMAND",
    "AuthorityBootstrapIdentity",
    "AuthorityRegistrySnapshot",
    "BootstrapTimedSpeechProfileRegistryCommand",
    "BootstrapTimedSpeechProfileRegistryRequest",
    "BootstrappedTimedSpeechProfile",
    "StoreAnchoredTimedSpeechProfileResolver",
    "TimedSpeechProfileKey",
    "VerifiedTimedSpeechAuthorityContext",
]
