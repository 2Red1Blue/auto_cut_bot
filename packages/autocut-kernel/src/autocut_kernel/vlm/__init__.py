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
    ProviderIndeterminate,
    ProviderPending,
    ProviderReconcileQuery,
    ProviderRequestIdCallback,
    ProviderResult,
    VlmProviderPort,
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
    "MappedSourceInterval",
    "ProxyTimelineMap",
    "ProxyTimelineSegment",
    "ProviderCompleted",
    "ProviderDispatchRequest",
    "ProviderFailed",
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
    "WindowFrameSample",
    "WindowManifest",
    "WindowManifestSet",
    "WindowProxyBlobRef",
    "parse_vlm_response",
    "select_core_owner",
]
