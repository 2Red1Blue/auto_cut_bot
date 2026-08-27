"""Read-only startup binding for content loaded from the controlled installed wheel.

A typed resource is content, not an authority capability. Production composition
must use the fixed installed loader and a real Store; neither this resolver nor a
fake Store establishes source provenance or database acceptance.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..contracts.compiler.canonical import canonical_json_bytes
from ..media.runtime_measurement_identity import RuntimeMeasurementIdentity
from ..media.types import sha256_prefixed
from ..store.models import PersistedRuntimeCalibrationCapability
from .authority_profiles import (
    RuntimeCalibrationCapabilityPolicy,
    RuntimeCalibrationPolicySource,
    decode_runtime_calibration_policy_source,
)
from .calibration_binding import (
    CalibrationRecordAnchorReader,
    RuntimeCalibrationCapabilityReader,
    bind_profile_calibration,
    bind_runtime_calibration_capability,
)
from .installed_local_run import LocalRunResource, load_installed_local_run_resource
from .runtime_timed_speech import (
    RuntimeTimedMediaAuthoritySelector,
    RuntimeTimedSpeechProjection,
)
from .timed_speech import (
    AuthorityRegistrySnapshot,
    AuthorityRegistryStore,
    BootstrappedTimedSpeechProfile,
    StoreAnchoredTimedSpeechProfileResolver,
    TimedSpeechProfileKey,
)


class InstalledLocalRunError(ValueError):
    """Installed content differs from its exact persisted runtime profile."""


class InstalledLocalRunAuthorityStore(
    CalibrationRecordAnchorReader, AuthorityRegistryStore, Protocol
):
    """Only the two authoritative reads needed for installed startup."""


class InstalledRuntimeCapabilityStore(RuntimeCalibrationCapabilityReader, Protocol):
    """The normal-runtime admission read; it has no writer or service credential."""


@dataclass(frozen=True, slots=True)
class InstalledRuntimeCapabilityResolver:
    """Resolve the static policy against a fresh live measurement at request time."""

    policy: RuntimeCalibrationPolicySource

    def __post_init__(self) -> None:
        if type(self.policy) is not RuntimeCalibrationPolicySource:  # noqa: E721
            raise InstalledLocalRunError(
                "runtime resolver requires an exact static calibration policy"
            )

    def resolve(
        self,
        store: InstalledRuntimeCapabilityStore,
        measurement_identity: RuntimeMeasurementIdentity,
    ) -> PersistedRuntimeCalibrationCapability:
        return bind_runtime_calibration_capability(
            policy=self.policy,
            measurement_identity=measurement_identity,
            store=store,
        )


@dataclass(frozen=True, slots=True)
class InstalledRuntimeTimedSpeechAuthorityResolver:
    """Re-read accepted PC capability, then derive its only request projection.

    A ``RuntimeTimedSpeechProjection`` is deliberately not an externally
    supplied command value.  This resolver reconstructs it immediately before
    native evidence work from the authenticated live measurement, the static
    installed policy and the Store's immutable accepted capability.
    """

    capability_resolver: InstalledRuntimeCapabilityResolver
    selector: RuntimeTimedMediaAuthoritySelector
    static_operation_policy_sha256: str

    def __post_init__(self) -> None:
        if type(self.capability_resolver) is not InstalledRuntimeCapabilityResolver:  # noqa: E721
            raise InstalledLocalRunError(
                "runtime timed-speech resolver requires exact capability resolver"
            )
        if type(self.selector) is not RuntimeTimedMediaAuthoritySelector:  # noqa: E721
            raise InstalledLocalRunError(
                "runtime timed-speech resolver requires exact authority selector"
            )
        try:
            sha256_prefixed(
                self.static_operation_policy_sha256,
                "runtime timed-speech static operation policy",
            )
        except ValueError as error:
            raise InstalledLocalRunError(
                "runtime timed-speech resolver requires a static operation policy hash"
            ) from error

    def resolve(
        self,
        store: InstalledRuntimeCapabilityStore,
        measurement_identity: RuntimeMeasurementIdentity,
    ) -> RuntimeTimedSpeechProjection:
        capability = self.capability_resolver.resolve(store, measurement_identity)
        return self.selector.select(capability, measurement_identity)


@dataclass(frozen=True, slots=True)
class InstalledLocalRunProfileResolver:
    resource: LocalRunResource

    def __post_init__(self) -> None:
        if type(self.resource) is not LocalRunResource:  # noqa: E721
            raise InstalledLocalRunError("requires an exact decoded installed local-run resource")

    @property
    def snapshot(self) -> AuthorityRegistrySnapshot:
        entry = self.resource.local_run.timed_speech_registry_entry
        return AuthorityRegistrySnapshot(
            self.resource.current_registry_sha256,
            TimedSpeechProfileKey(entry.profile_id, entry.profile_version),
        )

    def resolve(self, store: InstalledLocalRunAuthorityStore) -> BootstrappedTimedSpeechProfile:
        bind_profile_calibration(
            local_run=self.resource.local_run,
            shadow=self.resource.shadow,
            predecessor_registry_sha256=self.resource.predecessor_registry_sha256,
            store=store,
        )
        resolved = StoreAnchoredTimedSpeechProfileResolver(self.snapshot).resolve(store)
        if resolved.entry != self.resource.local_run.timed_speech_registry_entry:
            raise InstalledLocalRunError(
                "bootstrapped profile differs from installed local-run entry"
            )
        return resolved


def load_installed_local_run_resolver() -> InstalledLocalRunProfileResolver:
    """Load only the fixed package resource; no caller snapshot or path override."""
    return InstalledLocalRunProfileResolver(load_installed_local_run_resource())


def runtime_calibration_policy_for_installed_resource(
    resource: LocalRunResource,
) -> RuntimeCalibrationPolicySource:
    """Derive the closed v2 capability policy from the installed authority chain.

    The generated bytes are deliberately a function of the already verified
    wheel resource only.  They are not a configuration seam: a deployment
    cannot add a device family, change the source lineage, or select a policy
    path through its environment.  Accepted records remain a fresh Store read.
    """
    if type(resource) is not LocalRunResource:  # noqa: E721
        raise InstalledLocalRunError(
            "runtime calibration policy requires an exact installed resource"
        )
    return decode_runtime_calibration_policy_source(
        canonical_json_bytes(
            {
                "schema_version": "autocut-runtime-calibration-policy-v1",
                "profile_source_sha256": resource.shadow.source_sha256,
                "registry_snapshot_sha256": resource.predecessor_registry_sha256,
                "capabilities": [
                    RuntimeCalibrationCapabilityPolicy("mac_cpu", "cpu").to_mapping(),
                    RuntimeCalibrationCapabilityPolicy("pc_cuda", "cuda").to_mapping(),
                ],
            }
        )
    )
