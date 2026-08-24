"""Provider-independent, fail-closed VLM evidence contracts."""

from .models import (
    MappedSourceInterval,
    VlmContractError,
    VlmObservation,
    VlmObservationKind,
    VlmObservationSet,
    VlmParsePolicy,
    VlmRequestIdentity,
    VlmValidationError,
)
from .parser import VlmResponseIndeterminate, VlmResponseRejected, parse_vlm_response
from .provider_port import (
    ProviderCompleted,
    ProviderDispatchRequest,
    ProviderFailed,
    ProviderFailureDisposition,
    ProviderIndeterminate,
    ProviderPending,
    ProviderReconcileQuery,
    ProviderRequestIdCallback,
    ProviderResult,
    VlmProviderPort,
)
from .retry_policy import (
    GENERATION_PROVIDER_LEASE_SECONDS,
    GENERATION_RETRY_STRATEGY_VERSION,
    GenerationRetryPolicy,
)
from .window import (
    ProxyTimelineMap,
    ProxyTimelineSegment,
    WindowFrameSample,
    WindowManifest,
    WindowManifestSet,
    WindowProxyBlobRef,
    select_core_owner,
)

__all__ = [
    "GENERATION_PROVIDER_LEASE_SECONDS",
    "MappedSourceInterval",
    "ProxyTimelineMap",
    "ProxyTimelineSegment",
    "ProviderCompleted",
    "ProviderDispatchRequest",
    "ProviderFailed",
    "ProviderFailureDisposition",
    "ProviderIndeterminate",
    "ProviderPending",
    "ProviderRequestIdCallback",
    "ProviderReconcileQuery",
    "ProviderResult",
    "VlmContractError",
    "VlmObservation",
    "VlmObservationKind",
    "VlmObservationSet",
    "VlmParsePolicy",
    "VlmRequestIdentity",
    "VlmResponseIndeterminate",
    "VlmResponseRejected",
    "VlmProviderPort",
    "VlmValidationError",
    "GENERATION_RETRY_STRATEGY_VERSION",
    "GenerationRetryPolicy",
    "WindowFrameSample",
    "WindowManifest",
    "WindowManifestSet",
    "WindowProxyBlobRef",
    "parse_vlm_response",
    "select_core_owner",
]
