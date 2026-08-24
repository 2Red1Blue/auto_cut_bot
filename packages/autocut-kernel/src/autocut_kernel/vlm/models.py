"""Closed, immutable values for provider-independent VLM evidence.

The records in this module carry coarse semantic observations only.  They do
not model source paths, edit endpoints, recipes, admission, or publication
decisions.  Provider adapters are expected to persist their raw bytes outside
this pure kernel package and pass those exact bytes to :mod:`autocut_kernel.vlm.parser`.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING

from ..media.types import (
    TickRange,
    TimeBase,
    canonical_sha256,
    require_pts,
    sha256_prefixed,
)

if TYPE_CHECKING:
    from .window import WindowManifest, WindowManifestSet


class VlmContractError(ValueError):
    """Base error for a fail-closed VLM kernel contract."""


class VlmValidationError(VlmContractError):
    """Raised when an immutable VLM value violates its closed contract."""


def _closed_text(value: object, field_name: str, *, maximum_length: int = 256) -> str:
    if type(value) is not str or not value or value.isspace() or len(value) > maximum_length:  # noqa: E721
        raise VlmValidationError(f"{field_name} must be a non-empty string of at most {maximum_length} characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise VlmValidationError(f"{field_name} must not contain control characters")
    return value


def _non_negative(value: object, field_name: str) -> int:
    result = require_pts(value, field_name)
    if result < 0:
        raise VlmValidationError(f"{field_name} must be non-negative")
    return result


class VlmObservationKind(str, Enum):
    """The only semantic claims accepted from a VLM response."""

    OBSERVATION = "observation"
    CHANGE = "change"
    RELATION = "relation"


@dataclass(frozen=True, slots=True)
class VlmParsePolicy:
    """Frozen limits used by the strict response parser.

    Confidence is represented as :class:`~decimal.Decimal`; binary floating
    point is never part of a request identity or admission comparison.
    """

    minimum_confidence: Decimal
    max_response_bytes: int
    max_observations: int
    max_summary_characters: int
    max_total_summary_characters: int

    def __post_init__(self) -> None:
        if type(self.minimum_confidence) is not Decimal:  # noqa: E721
            raise VlmValidationError("parse_policy.minimum_confidence must be a Decimal")
        if not self.minimum_confidence.is_finite() or not Decimal("0") <= self.minimum_confidence <= Decimal("1"):
            raise VlmValidationError("parse_policy.minimum_confidence must be between 0 and 1")
        for field_name in (
            "max_response_bytes",
            "max_observations",
            "max_summary_characters",
            "max_total_summary_characters",
        ):
            value = require_pts(getattr(self, field_name), f"parse_policy.{field_name}")
            if value <= 0:
                raise VlmValidationError(f"parse_policy.{field_name} must be positive")
        if self.max_summary_characters > self.max_total_summary_characters:
            raise VlmValidationError("per-observation summary budget cannot exceed total summary budget")

    def to_mapping(self) -> dict[str, object]:
        return {
            "max_observations": self.max_observations,
            "max_response_bytes": self.max_response_bytes,
            "max_summary_characters": self.max_summary_characters,
            "max_total_summary_characters": self.max_total_summary_characters,
            "minimum_confidence": format(self.minimum_confidence, "f"),
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class VlmRequestIdentity:
    """Canonical identity of one provider invocation request.

    The window hash transitively binds source bytes, stream, sampled frame
    PTS/hashes, proxy bytes, preprocessing, and timeline mapping.
    """

    window_manifest_sha256: str
    source_id: str
    source_clock_id: str
    source_sha256: str
    frame_samples_sha256: str
    frame_pts_index_set_sha256: str
    window_manifest_set_sha256: str
    proxy_blob_ref_sha256: str
    preprocess_policy_sha256: str
    window_sampling_policy_sha256: str
    prompt_template_sha256: str
    prompt_version: str
    response_schema_sha256: str
    model_id: str
    provider_id: str
    request_parameters_sha256: str
    request_payload_sha256: str
    parse_policy_sha256: str

    def __post_init__(self) -> None:
        for field_name in (
            "window_manifest_sha256",
            "source_sha256",
            "frame_samples_sha256",
            "frame_pts_index_set_sha256",
            "window_manifest_set_sha256",
            "proxy_blob_ref_sha256",
            "preprocess_policy_sha256",
            "window_sampling_policy_sha256",
            "prompt_template_sha256",
            "response_schema_sha256",
            "request_parameters_sha256",
            "request_payload_sha256",
            "parse_policy_sha256",
        ):
            sha256_prefixed(getattr(self, field_name), f"request_identity.{field_name}")
        _closed_text(self.source_id, "request_identity.source_id")
        _closed_text(self.source_clock_id, "request_identity.source_clock_id")
        _closed_text(self.prompt_version, "request_identity.prompt_version")
        _closed_text(self.model_id, "request_identity.model_id")
        _closed_text(self.provider_id, "request_identity.provider_id")

    @classmethod
    def from_manifest(
        cls,
        manifest: WindowManifest,
        manifest_set: WindowManifestSet,
        *,
        prompt_template_sha256: str,
        prompt_version: str,
        response_schema_sha256: str,
        model_id: str,
        provider_id: str,
        request_parameters_sha256: str,
        request_payload_sha256: str,
        parse_policy: VlmParsePolicy,
    ) -> VlmRequestIdentity:
        """Build an identity from Kernel-owned values, never provider claims."""

        # Runtime import avoids a models/window import cycle while retaining an
        # exact type check at the trust boundary.
        from .window import WindowManifest, WindowManifestSet

        if type(manifest) is not WindowManifest:  # noqa: E721
            raise VlmValidationError("request manifest must be a WindowManifest")
        if type(manifest_set) is not WindowManifestSet:  # noqa: E721
            raise VlmValidationError("request manifest_set must be a WindowManifestSet")
        manifest_set.require_member(manifest)
        if type(parse_policy) is not VlmParsePolicy:  # noqa: E721
            raise VlmValidationError("parse_policy must be a VlmParsePolicy")
        return cls(
            window_manifest_sha256=manifest.canonical_hash,
            source_id=manifest.source_id,
            source_clock_id=manifest.source_clock_id,
            source_sha256=manifest.source_sha256,
            frame_samples_sha256=manifest.frame_samples_sha256,
            frame_pts_index_set_sha256=manifest.frame_pts_index_set_sha256,
            window_manifest_set_sha256=manifest_set.canonical_hash,
            proxy_blob_ref_sha256=manifest.proxy_blob_ref.canonical_hash,
            preprocess_policy_sha256=manifest.preprocess_policy_sha256,
            window_sampling_policy_sha256=manifest.window_sampling_policy_sha256,
            prompt_template_sha256=prompt_template_sha256,
            prompt_version=prompt_version,
            response_schema_sha256=response_schema_sha256,
            model_id=model_id,
            provider_id=provider_id,
            request_parameters_sha256=request_parameters_sha256,
            request_payload_sha256=request_payload_sha256,
            parse_policy_sha256=parse_policy.canonical_hash,
        )

    def assert_manifest_binding(
        self,
        manifest: WindowManifest,
        manifest_set: WindowManifestSet,
    ) -> None:
        """Reject a directly-constructed identity that forges transitive fields."""

        from .window import WindowManifest, WindowManifestSet

        if type(manifest) is not WindowManifest:  # noqa: E721
            raise VlmValidationError("request manifest must be a WindowManifest")
        if type(manifest_set) is not WindowManifestSet:  # noqa: E721
            raise VlmValidationError("request manifest_set must be a WindowManifestSet")
        manifest_set.require_member(manifest)
        expected: dict[str, object] = {
            "window_manifest_sha256": manifest.canonical_hash,
            "source_id": manifest.source_id,
            "source_clock_id": manifest.source_clock_id,
            "source_sha256": manifest.source_sha256,
            "frame_samples_sha256": manifest.frame_samples_sha256,
            "frame_pts_index_set_sha256": manifest.frame_pts_index_set_sha256,
            "window_manifest_set_sha256": manifest_set.canonical_hash,
            "proxy_blob_ref_sha256": manifest.proxy_blob_ref.canonical_hash,
            "preprocess_policy_sha256": manifest.preprocess_policy_sha256,
            "window_sampling_policy_sha256": manifest.window_sampling_policy_sha256,
        }
        mismatches = tuple(
            field_name
            for field_name, expected_value in expected.items()
            if getattr(self, field_name) != expected_value
        )
        if mismatches:
            raise VlmValidationError(
                f"request identity manifest binding mismatch: {', '.join(mismatches)}"
            )

    def to_mapping(self) -> dict[str, object]:
        return {
            "frame_samples_sha256": self.frame_samples_sha256,
            "frame_pts_index_set_sha256": self.frame_pts_index_set_sha256,
            "model_id": self.model_id,
            "parse_policy_sha256": self.parse_policy_sha256,
            "prompt_template_sha256": self.prompt_template_sha256,
            "prompt_version": self.prompt_version,
            "provider_id": self.provider_id,
            "proxy_blob_ref_sha256": self.proxy_blob_ref_sha256,
            "preprocess_policy_sha256": self.preprocess_policy_sha256,
            "request_parameters_sha256": self.request_parameters_sha256,
            "request_payload_sha256": self.request_payload_sha256,
            "response_schema_sha256": self.response_schema_sha256,
            "source_clock_id": self.source_clock_id,
            "source_id": self.source_id,
            "source_sha256": self.source_sha256,
            "window_sampling_policy_sha256": self.window_sampling_policy_sha256,
            "window_manifest_sha256": self.window_manifest_sha256,
            "window_manifest_set_sha256": self.window_manifest_set_sha256,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class MappedSourceInterval:
    """A conservative coarse Source interval, never a physical endpoint proof."""

    coarse_range: TickRange
    mapping_error_bound_source_pts: int
    source_time_base: TimeBase
    provider_uncertainty_proxy_pts: int
    proxy_time_base: TimeBase

    def __post_init__(self) -> None:
        if type(self.coarse_range) is not TickRange:  # noqa: E721
            raise VlmValidationError("mapped interval must contain a TickRange")
        _non_negative(
            self.mapping_error_bound_source_pts,
            "mapped_interval.mapping_error_bound_source_pts",
        )
        _non_negative(
            self.provider_uncertainty_proxy_pts,
            "mapped_interval.provider_uncertainty_proxy_pts",
        )
        if type(self.source_time_base) is not TimeBase or type(self.proxy_time_base) is not TimeBase:  # noqa: E721
            raise VlmValidationError("mapped interval time bases must be TimeBase values")

    def to_mapping(self) -> dict[str, object]:
        return {
            "coarse_range": {
                "end_pts": self.coarse_range.end_pts,
                "start_pts": self.coarse_range.start_pts,
                "time_base": {
                    "denominator": self.source_time_base.denominator,
                    "numerator": self.source_time_base.numerator,
                },
            },
            "mapping_error_bound": {
                "clock": "source",
                "tick": self.mapping_error_bound_source_pts,
                "time_base": {
                    "denominator": self.source_time_base.denominator,
                    "numerator": self.source_time_base.numerator,
                },
            },
            "provider_uncertainty": {
                "clock": "proxy",
                "tick": self.provider_uncertainty_proxy_pts,
                "time_base": {
                    "denominator": self.proxy_time_base.denominator,
                    "numerator": self.proxy_time_base.numerator,
                },
            },
            "semantic_precision": "coarse_only",
        }


@dataclass(frozen=True, slots=True)
class VlmObservation:
    """One parsed semantic observation with Kernel-derived provenance."""

    observation_id: str
    kind: VlmObservationKind
    summary: str
    confidence: Decimal
    supporting_frame_ids: tuple[str, ...]
    source_interval: MappedSourceInterval
    request_identity_sha256: str
    window_manifest_sha256: str
    core_owned: bool

    def __post_init__(self) -> None:
        sha256_prefixed(self.observation_id, "observation.observation_id")
        if type(self.kind) is not VlmObservationKind:  # noqa: E721
            raise VlmValidationError("observation.kind must be a VlmObservationKind")
        _closed_text(self.summary, "observation.summary", maximum_length=16_384)
        if type(self.confidence) is not Decimal or not self.confidence.is_finite():  # noqa: E721
            raise VlmValidationError("observation.confidence must be a finite Decimal")
        if not Decimal("0") <= self.confidence <= Decimal("1"):
            raise VlmValidationError("observation.confidence must be between 0 and 1")
        frames = tuple(self.supporting_frame_ids)
        if not frames or len(frames) != len(set(frames)):
            raise VlmValidationError("observation.supporting_frame_ids must be non-empty and unique")
        for frame_id in frames:
            sha256_prefixed(frame_id, "observation.supporting_frame_ids[]")
        if type(self.source_interval) is not MappedSourceInterval:  # noqa: E721
            raise VlmValidationError("observation.source_interval must be a MappedSourceInterval")
        sha256_prefixed(self.request_identity_sha256, "observation.request_identity_sha256")
        sha256_prefixed(self.window_manifest_sha256, "observation.window_manifest_sha256")
        if type(self.core_owned) is not bool:  # noqa: E721
            raise VlmValidationError("observation.core_owned must be a boolean derived by the Kernel")
        object.__setattr__(self, "supporting_frame_ids", frames)

    def to_mapping(self) -> dict[str, object]:
        return {
            "confidence": format(self.confidence, "f"),
            "core_owned": self.core_owned,
            "kind": self.kind.value,
            "observation_id": self.observation_id,
            "provenance": {
                "request_identity_sha256": self.request_identity_sha256,
                "window_manifest_sha256": self.window_manifest_sha256,
            },
            "source_interval": self.source_interval.to_mapping(),
            "summary": self.summary,
            "supporting_frame_ids": list(self.supporting_frame_ids),
        }


@dataclass(frozen=True, slots=True)
class VlmObservationSet:
    """A complete accepted response; rejected/unknown responses never become empty sets."""

    request_identity_sha256: str
    window_manifest_sha256: str
    raw_response_sha256: str
    observations: tuple[VlmObservation, ...]

    def __post_init__(self) -> None:
        sha256_prefixed(self.request_identity_sha256, "observation_set.request_identity_sha256")
        sha256_prefixed(self.window_manifest_sha256, "observation_set.window_manifest_sha256")
        sha256_prefixed(self.raw_response_sha256, "observation_set.raw_response_sha256")
        observations = tuple(self.observations)
        if not observations:
            raise VlmValidationError("an accepted observation set must not be empty")
        if any(type(item) is not VlmObservation for item in observations):  # noqa: E721
            raise VlmValidationError("observation_set.observations must contain VlmObservation values")
        ids = tuple(item.observation_id for item in observations)
        if len(ids) != len(set(ids)):
            raise VlmValidationError("observation_set.observations must be unique")
        if any(item.request_identity_sha256 != self.request_identity_sha256 for item in observations):
            raise VlmValidationError("every observation must bind the exact request identity")
        if any(item.window_manifest_sha256 != self.window_manifest_sha256 for item in observations):
            raise VlmValidationError("every observation must bind the exact window manifest")
        object.__setattr__(self, "observations", tuple(sorted(observations, key=lambda item: item.observation_id)))

    def to_mapping(self) -> dict[str, object]:
        return {
            "observations": [item.to_mapping() for item in self.observations],
            "provenance": {
                "raw_response_sha256": self.raw_response_sha256,
                "request_identity_sha256": self.request_identity_sha256,
                "window_manifest_sha256": self.window_manifest_sha256,
            },
            "schema_version": 1,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())
