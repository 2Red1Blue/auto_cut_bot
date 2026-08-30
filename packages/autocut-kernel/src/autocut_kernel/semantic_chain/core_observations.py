"""Exact V3/V4 observation seam used by Stage 1 semantic projections.

V4 is never converted into a V3 value.  Consumers receive an exact union and
may read only fields that the two frozen observation contracts share.  Timing
and confidence come from the already-verified support derivation owned by the
respective parser version.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TypeAlias, cast

from ..store.models import (
    CommittedVlmSemanticInput,
    PersistedVlmSemanticPack,
    PersistedVlmSemanticPackV4,
)
from ..vlm.models import (
    MappedSourceInterval,
    VlmContinuity,
    VlmEntity,
    VlmEvent,
    VlmFact,
    VlmSemanticPack,
    VlmSemanticSupport,
    VlmWindowSummary,
)
from ..vlm.semantic_pack_v4 import (
    VlmContinuityV4,
    VlmEntityV4,
    VlmEventV4,
    VlmFactV4,
    VlmSemanticPackV4,
)
from ..vlm.semantic_support_v4 import (
    FrameAnchoredObservationSupportV4,
    VideoObservationSupportV4,
)

CoreSemanticPack: TypeAlias = VlmSemanticPack | VlmSemanticPackV4
CoreEntity: TypeAlias = VlmEntity | VlmEntityV4
CoreFact: TypeAlias = VlmFact | VlmFactV4
CoreEvent: TypeAlias = VlmEvent | VlmEventV4
CoreContinuity: TypeAlias = VlmContinuity | VlmContinuityV4
CoreObservation: TypeAlias = VlmWindowSummary | CoreEntity | CoreFact | CoreEvent
SupportedObservation: TypeAlias = CoreEntity | CoreFact | CoreEvent
ObservationSupport: TypeAlias = (
    VlmSemanticSupport | VideoObservationSupportV4 | FrameAnchoredObservationSupportV4
)


class CoreObservationError(ValueError):
    """A Stage 1 input crossed the exact persisted V3/V4 observation seam."""


def semantic_pack(item: CommittedVlmSemanticInput) -> CoreSemanticPack:
    """Return the exact verified semantic pack without cross-version coercion."""

    if type(item) is not CommittedVlmSemanticInput:  # noqa: E721
        raise CoreObservationError("core observation input must be exact")
    persisted = item.semantic_pack
    if type(persisted) is PersistedVlmSemanticPack:  # noqa: E721
        if type(persisted.semantic_pack) is not VlmSemanticPack:  # noqa: E721
            raise CoreObservationError("persisted V3 observation type is invalid")
        return persisted.semantic_pack
    if type(persisted) is PersistedVlmSemanticPackV4:  # noqa: E721
        if type(persisted.semantic_pack) is not VlmSemanticPackV4:  # noqa: E721
            raise CoreObservationError("persisted V4 observation type is invalid")
        return persisted.semantic_pack
    raise CoreObservationError("persisted observation version is not registered")


def _observation_support(value: SupportedObservation) -> ObservationSupport:
    if type(value) is VlmEntity:  # noqa: E721
        support = value.support
        if type(support) is not VlmSemanticSupport:  # noqa: E721
            raise CoreObservationError("V3 observation support type is invalid")
        return support
    if type(value) is VlmFact:  # noqa: E721
        support = value.support
        if type(support) is not VlmSemanticSupport:  # noqa: E721
            raise CoreObservationError("V3 observation support type is invalid")
        return support
    if type(value) is VlmEvent:  # noqa: E721
        support = value.support
        if type(support) is not VlmSemanticSupport:  # noqa: E721
            raise CoreObservationError("V3 observation support type is invalid")
        return support
    if type(value) is VlmEntityV4:  # noqa: E721
        support_v4 = value.support
    elif type(value) is VlmFactV4:  # noqa: E721
        support_v4 = value.support
    elif type(value) is VlmEventV4:  # noqa: E721
        support_v4 = value.support
    else:
        raise CoreObservationError("support is unavailable for this observation type")
    if type(support_v4) not in (  # noqa: E721
        VideoObservationSupportV4,
        FrameAnchoredObservationSupportV4,
    ):
        raise CoreObservationError("V4 observation support type is invalid")
    return support_v4


def observation_confidence(value: CoreObservation) -> Decimal:
    """Read the parser-verified confidence shared by V3 and V4 observations."""

    if type(value) is VlmWindowSummary:  # noqa: E721
        return value.confidence
    return _observation_support(cast(SupportedObservation, value)).confidence


def observation_source_interval(value: SupportedObservation) -> MappedSourceInterval:
    """Read the exact source interval derived by the owning parser version."""

    # Exact support validation is centralized with confidence so Stage 1 never
    # invents a V4 frame anchor or re-derives millisecond timing.
    interval = _observation_support(value).source_interval
    if type(interval) is not MappedSourceInterval:  # noqa: E721
        raise CoreObservationError("observation source interval is invalid")
    return interval
