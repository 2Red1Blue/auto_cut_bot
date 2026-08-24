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
    "VlmContractError",
    "VlmObservation",
    "VlmObservationKind",
    "VlmObservationSet",
    "VlmParsePolicy",
    "VlmRequestIdentity",
    "VlmResponseIndeterminate",
    "VlmResponseRejected",
    "VlmValidationError",
    "WindowFrameSample",
    "WindowManifest",
    "WindowManifestSet",
    "WindowProxyBlobRef",
    "parse_vlm_response",
    "select_core_owner",
]
