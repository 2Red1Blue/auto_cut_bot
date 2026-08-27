"""Coarse video observations with exact playback-clock conversion, not frame proof."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from math import ceil, floor
from types import MappingProxyType
from typing import Literal, TypeAlias, cast

from ..media.types import TickRange, canonical_sha256
from .models import MappedSourceInterval, VlmProxyInterval, VlmValidationError
from .window import WindowManifest, WindowManifestSet, select_core_owner


def _rational(value: Fraction) -> dict[str, int]:
    return {"numerator": value.numerator, "denominator": value.denominator}


def _closed(value: object, keys: set[str], name: str) -> dict[str, object]:
    if type(value) is not dict:  # noqa: E721
        raise VlmValidationError(f"{name} must contain exactly {sorted(keys)}")
    result = cast(dict[str, object], value)
    if set(result) != keys:
        raise VlmValidationError(f"{name} must contain exactly {sorted(keys)}")
    return result


@dataclass(frozen=True, slots=True)
class IntervalMsV4:
    """Original provider milliseconds relative to the registered proxy origin."""

    start_ms: int
    end_ms: int
    uncertainty_ms: int

    def __post_init__(self) -> None:
        for name in ("start_ms", "end_ms", "uncertainty_ms"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:  # noqa: E721
                raise VlmValidationError(f"{name} must be a non-negative integer")
        if self.start_ms >= self.end_ms:
            raise VlmValidationError("interval_ms must be a non-empty half-open interval")

    def to_mapping(self) -> dict[str, object]:
        return {
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "uncertainty_ms": self.uncertainty_ms,
        }


@dataclass(frozen=True, slots=True)
class FrameAliasV4:
    alias: str
    frame_id: str
    frame_sha256: str
    proxy_pts: int
    source_pts: int
    relative_time_ms: Fraction

    def to_mapping(self) -> dict[str, object]:
        return {
            "alias": self.alias,
            "frame_id": self.frame_id,
            "frame_sha256": self.frame_sha256,
            "proxy_pts": self.proxy_pts,
            "source_pts": self.source_pts,
            "relative_time_ms": _rational(self.relative_time_ms),
        }


@dataclass(frozen=True, slots=True)
class FrameAliasTable:
    """Aliases derive only from immutable manifest sample order, never model output."""

    manifest: WindowManifest = field(repr=False)
    entries: tuple[FrameAliasV4, ...] = field(init=False)

    def __post_init__(self) -> None:
        if type(self.manifest) is not WindowManifest:  # noqa: E721
            raise VlmValidationError("frame aliases require a WindowManifest")
        timeline = self.manifest.timeline_map
        ms_per_tick = Fraction(
            timeline.proxy_time_base.numerator * 1_000, timeline.proxy_time_base.denominator
        )
        object.__setattr__(self, "entries", tuple(
            FrameAliasV4(
                alias=f"f{index:04d}",
                frame_id=sample.frame_id,
                frame_sha256=sample.frame_sha256,
                proxy_pts=sample.proxy_pts,
                source_pts=sample.source_pts,
                relative_time_ms=(sample.proxy_pts - timeline.proxy_range.start_pts) * ms_per_tick,
            )
            for index, sample in enumerate(self.manifest.frame_samples, start=1)
        ))

    @property
    def by_alias(self) -> Mapping[str, FrameAliasV4]:
        return MappingProxyType({entry.alias: entry for entry in self.entries})

    def to_mapping(self) -> dict[str, object]:
        return {
            "alias_strategy": "manifest-sample-order-f0001-v1",
            "window_manifest_sha256": self.manifest.canonical_hash,
            "proxy_blob_ref_sha256": self.manifest.proxy_blob_ref.canonical_hash,
            "entries": [entry.to_mapping() for entry in self.entries],
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


def frame_aliases(manifest: WindowManifest) -> FrameAliasTable:
    return FrameAliasTable(manifest)


@dataclass(frozen=True, slots=True)
class SupportDerivationV4:
    proxy_interval: VlmProxyInterval
    source_interval: MappedSourceInterval
    core_owner_window_manifest_sha256: str
    proxy_origin_pts: int
    playback_duration_ms: Fraction
    representable_duration_ms: int
    exact_start_proxy_pts: Fraction
    exact_end_proxy_pts: Fraction
    exact_uncertainty_proxy_pts: Fraction
    declared_uncertainty_proxy_pts: int
    start_quantization_error_proxy_pts: Fraction
    end_quantization_error_proxy_pts: Fraction

    def to_mapping(self) -> dict[str, object]:
        return {
            "conversion_strategy": "relative-ms-floor-start-ceil-end-v1",
            "proxy_interval": self.proxy_interval.to_mapping(),
            "source_interval": self.source_interval.to_mapping(),
            "core_owner_window_manifest_sha256": self.core_owner_window_manifest_sha256,
            "proxy_origin_pts": self.proxy_origin_pts,
            "playback_duration_ms": _rational(self.playback_duration_ms),
            "representable_duration_ms": self.representable_duration_ms,
            "unrepresentable_tail_ms": _rational(
                self.playback_duration_ms - self.representable_duration_ms
            ),
            "exact_start_proxy_pts": _rational(self.exact_start_proxy_pts),
            "exact_end_proxy_pts": _rational(self.exact_end_proxy_pts),
            "exact_uncertainty_proxy_pts": _rational(self.exact_uncertainty_proxy_pts),
            "declared_uncertainty_proxy_pts": self.declared_uncertainty_proxy_pts,
            "start_quantization_error_proxy_pts": _rational(self.start_quantization_error_proxy_pts),
            "end_quantization_error_proxy_pts": _rational(self.end_quantization_error_proxy_pts),
            "uncertainty_strategy": "ceil-exact-uncertainty-plus-max-boundary-error-v1",
            "semantic_precision": "coarse_only",
        }


def _derive(
    interval: IntervalMsV4, manifest: WindowManifest, manifest_set: WindowManifestSet
) -> SupportDerivationV4:
    timeline = manifest.timeline_map
    ticks_per_ms = Fraction(
        timeline.proxy_time_base.denominator, timeline.proxy_time_base.numerator * 1_000
    )
    duration_ms = timeline.proxy_range.duration_pts / ticks_per_ms
    representable_ms = floor(duration_ms)
    if representable_ms < 1:
        raise VlmValidationError("sub-millisecond windows do not support interval_ms")
    if interval.end_ms > representable_ms:
        raise VlmValidationError("interval_ms exceeds representable playback duration")
    origin = timeline.proxy_range.start_pts
    exact_start = origin + interval.start_ms * ticks_per_ms
    exact_end = origin + interval.end_ms * ticks_per_ms
    proxy_range = TickRange(floor(exact_start), ceil(exact_end))
    start_error = exact_start - proxy_range.start_pts
    end_error = proxy_range.end_pts - exact_end
    exact_uncertainty = interval.uncertainty_ms * ticks_per_ms
    # Round once after adding the actual error, not once per fractional component.
    effective_uncertainty = ceil(exact_uncertainty + max(start_error, end_error))
    mapped = timeline.map_interval(
        proxy_range, provider_uncertainty_proxy_pts=effective_uncertainty
    )
    owner = select_core_owner(manifest_set, mapped.coarse_range)
    return SupportDerivationV4(
        proxy_interval=VlmProxyInterval(proxy_range, effective_uncertainty),
        source_interval=mapped,
        core_owner_window_manifest_sha256=owner.canonical_hash,
        proxy_origin_pts=origin,
        playback_duration_ms=duration_ms,
        representable_duration_ms=representable_ms,
        exact_start_proxy_pts=exact_start,
        exact_end_proxy_pts=exact_end,
        exact_uncertainty_proxy_pts=exact_uncertainty,
        declared_uncertainty_proxy_pts=ceil(exact_uncertainty),
        start_quantization_error_proxy_pts=start_error,
        end_quantization_error_proxy_pts=end_error,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class _ObservationSupportV4:
    manifest: WindowManifest = field(repr=False)
    manifest_set: WindowManifestSet = field(repr=False)
    interval_ms: IntervalMsV4
    confidence: Decimal
    derived: SupportDerivationV4 = field(init=False)

    def __post_init__(self) -> None:
        if type(self.manifest_set) is not WindowManifestSet:  # noqa: E721
            raise VlmValidationError("support requires a WindowManifestSet")
        self.manifest_set.require_member(self.manifest)
        if type(self.interval_ms) is not IntervalMsV4:  # noqa: E721
            raise VlmValidationError("support requires IntervalMsV4")
        if (
            type(self.confidence) is not Decimal
            or not self.confidence.is_finite()
            or not Decimal(0) <= self.confidence <= Decimal(1)
            or self.confidence.is_signed()
        ):
            raise VlmValidationError("confidence must be a decimal between zero and one")
        object.__setattr__(self, "derived", _derive(self.interval_ms, self.manifest, self.manifest_set))

    @property
    def proxy_interval(self) -> VlmProxyInterval:
        return self.derived.proxy_interval

    @property
    def source_interval(self) -> MappedSourceInterval:
        return self.derived.source_interval

    @property
    def core_owner_window_manifest_sha256(self) -> str:
        return self.derived.core_owner_window_manifest_sha256

    def _wire(self, kind: str) -> dict[str, object]:
        return {
            "support_kind": kind,
            "interval_ms": self.interval_ms.to_mapping(),
            "confidence": format(self.confidence, "f"),
        }

    def _bound(self, wire: dict[str, object]) -> dict[str, object]:
        return {
            **wire,
            "binding": {
                "window_manifest_sha256": self.manifest.canonical_hash,
                "window_manifest_set_sha256": self.manifest_set.canonical_hash,
                "proxy_blob_ref": self.manifest.proxy_blob_ref.to_mapping(),
                "frame_aliases_sha256": frame_aliases(self.manifest).canonical_hash,
            },
            "derived": self.derived.to_mapping(),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class VideoObservationSupportV4(_ObservationSupportV4):
    """A model video observation; deliberately has no supporting-frame fields."""

    @property
    def support_kind(self) -> Literal["video_observation"]:
        return "video_observation"

    def to_wire_mapping(self) -> dict[str, object]:
        return self._wire(self.support_kind)

    def to_mapping(self) -> dict[str, object]:
        return self._bound(self.to_wire_mapping())


@dataclass(frozen=True, slots=True, kw_only=True)
class FrameAnchoredObservationSupportV4(_ObservationSupportV4):
    frame_refs: tuple[str, ...]
    frame_anchors: tuple[FrameAliasV4, ...] = field(init=False)

    def __post_init__(self) -> None:
        _ObservationSupportV4.__post_init__(self)
        if (
            type(self.frame_refs) is not tuple
            or not self.frame_refs
            or any(type(alias) is not str for alias in self.frame_refs)
            or len(set(self.frame_refs)) != len(self.frame_refs)
        ):
            raise VlmValidationError("frame_refs must be non-empty unique aliases")
        aliases = frame_aliases(self.manifest).by_alias
        if any(alias not in aliases for alias in self.frame_refs):
            raise VlmValidationError("frame_refs contains an unknown frame alias")
        anchors = tuple(aliases[alias] for alias in self.frame_refs)
        # Neither outward tick rounding nor provider uncertainty may create an anchor.
        if not any(
            self.derived.exact_start_proxy_pts <= anchor.proxy_pts < self.derived.exact_end_proxy_pts
            for anchor in anchors
        ):
            raise VlmValidationError("frame_refs requires a frame inside the declared interval_ms")
        object.__setattr__(self, "frame_anchors", anchors)

    @property
    def support_kind(self) -> Literal["frame_anchored_observation"]:
        return "frame_anchored_observation"

    def to_wire_mapping(self) -> dict[str, object]:
        return {**self._wire(self.support_kind), "frame_refs": list(self.frame_refs)}

    def to_mapping(self) -> dict[str, object]:
        return {
            **self._bound(self.to_wire_mapping()),
            "frame_anchors": [anchor.to_mapping() for anchor in self.frame_anchors],
        }


SemanticSupportV4: TypeAlias = VideoObservationSupportV4 | FrameAnchoredObservationSupportV4


def parse_support_v4(
    raw_mapping: object, manifest: WindowManifest, manifest_set: WindowManifestSet
) -> SemanticSupportV4:
    if type(raw_mapping) is not dict:  # noqa: E721
        raise VlmValidationError("support must be an object")
    raw = cast(dict[str, object], raw_mapping)
    kind = raw.get("support_kind")
    if type(kind) is not str or kind not in ("video_observation", "frame_anchored_observation"):  # noqa: E721
        raise VlmValidationError("support_kind is not registered")
    fields = {"support_kind", "interval_ms", "confidence"}
    if kind == "frame_anchored_observation":
        fields.add("frame_refs")
    _closed(raw, fields, "support")
    interval = _closed(raw["interval_ms"], {"start_ms", "end_ms", "uncertainty_ms"}, "interval_ms")
    confidence = raw["confidence"]
    if type(confidence) is not str or re.fullmatch(r"(?:0(?:\.[0-9]+)?|1(?:\.0+)?)", confidence) is None:  # noqa: E721
        raise VlmValidationError("confidence must be a canonical decimal string")
    timing = IntervalMsV4(
        cast(int, interval["start_ms"]),
        cast(int, interval["end_ms"]),
        cast(int, interval["uncertainty_ms"]),
    )
    if kind == "video_observation":
        return VideoObservationSupportV4(
            manifest=manifest, manifest_set=manifest_set, interval_ms=timing,
            confidence=Decimal(confidence),
        )
    refs = raw["frame_refs"]
    if type(refs) is not list:  # noqa: E721
        raise VlmValidationError("frame_refs must be an array")
    return FrameAnchoredObservationSupportV4(
        manifest=manifest, manifest_set=manifest_set, interval_ms=timing,
        confidence=Decimal(confidence), frame_refs=tuple(cast(list[str], refs)),
    )


def decode_support_v4(
    mapping: object, manifest: WindowManifest, manifest_set: WindowManifestSet
) -> SemanticSupportV4:
    """Re-derive persisted fields from original milliseconds and the exact context."""

    if type(mapping) is not dict:  # noqa: E721
        raise VlmValidationError("persisted support must be an object")
    raw = cast(dict[str, object], mapping)
    wire = {key: value for key, value in raw.items() if key not in {"binding", "derived", "frame_anchors"}}
    result = parse_support_v4(wire, manifest, manifest_set)
    try:
        encoded = json.dumps(raw, sort_keys=True, separators=(",", ":"), allow_nan=False)
        expected = json.dumps(result.to_mapping(), sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise VlmValidationError("persisted support is not canonical JSON") from error
    if encoded != expected:
        raise VlmValidationError("persisted support differs from its Kernel-derived binding or conversion")
    return result
