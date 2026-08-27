"""Authority-owned immutable registry projections.

Runtime code imports resolvers from this package; it never receives registry
payloads or committed-member references from an external run request.
"""

from .authority_profiles import (
    AUTHORITY_PROFILE_SOURCE_INVALID,
    AuthorityProfileSourceError,
    LocalRunProfileSource,
    ShadowCalibrationProfileSource,
    Stage1NarrativeProfileSource,
    UnresolvedAuthorityProfileSourceSet,
    decode_authority_profile_source_grammar,
    decode_stage1_narrative_profile_source,
)
from .runtime_timed_speech import (
    RuntimeTimedMediaAuthoritySelector,
    RuntimeTimedSpeechCapabilityAdmission,
    RuntimeTimedSpeechProjection,
    RuntimeTimedSpeechProjectionError,
    project_runtime_timed_speech,
)
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
    "AUTHORITY_PROFILE_SOURCE_INVALID",
    "BOOTSTRAP_TIMED_SPEECH_PROFILE_REGISTRY_COMMAND",
    "AuthorityBootstrapIdentity",
    "AuthorityProfileSourceError",
    "AuthorityRegistrySnapshot",
    "BootstrapTimedSpeechProfileRegistryCommand",
    "BootstrapTimedSpeechProfileRegistryRequest",
    "BootstrappedTimedSpeechProfile",
    "LocalRunProfileSource",
    "RuntimeTimedMediaAuthoritySelector",
    "RuntimeTimedSpeechCapabilityAdmission",
    "RuntimeTimedSpeechProjection",
    "RuntimeTimedSpeechProjectionError",
    "ShadowCalibrationProfileSource",
    "Stage1NarrativeProfileSource",
    "StoreAnchoredTimedSpeechProfileResolver",
    "TimedSpeechProfileKey",
    "UnresolvedAuthorityProfileSourceSet",
    "VerifiedTimedSpeechAuthorityContext",
    "decode_authority_profile_source_grammar",
    "decode_stage1_narrative_profile_source",
    "project_runtime_timed_speech",
]
