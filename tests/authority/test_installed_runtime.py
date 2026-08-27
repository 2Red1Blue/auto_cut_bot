"""Synthetic installed content + explicitly fake Store; not native acceptance."""

from __future__ import annotations

import socket
import subprocess
from dataclasses import FrozenInstanceError, replace
from uuid import UUID

import pytest
from authority.local_run_resource import emit_locked_local_run_resource
from autocut_kernel.contracts.compiler.canonical import sha256_bytes
from autocut_kernel.registry import installed_runtime
from autocut_kernel.registry.authority_profiles import (
    RuntimeCalibrationCapabilityPolicy,
    RuntimeCalibrationPolicySource,
)
from autocut_kernel.registry.calibration_binding import CalibrationBindingError
from autocut_kernel.registry.installed_local_run import (
    LocalRunResourceError,
    decode_local_run_resource,
)
from autocut_kernel.registry.installed_runtime import (
    InstalledLocalRunError,
    InstalledLocalRunProfileResolver,
    InstalledRuntimeCapabilityResolver,
    load_installed_local_run_resolver,
    runtime_calibration_policy_for_installed_resource,
)
from autocut_kernel.registry.timed_speech import (
    TIMED_SPEECH_PROFILE_REGISTRY_ARTIFACT_TYPE,
    TIMED_SPEECH_PROFILE_REGISTRY_SCOPE,
    AuthorityRegistrySnapshot,
    BootstrappedTimedSpeechProfile,
    TimedSpeechProfileKey,
    TimedSpeechRegistryError,
)
from autocut_kernel.store.models import (
    CommittedArtifactMemberReference,
    PersistedRuntimeCalibrationCapability,
)

from tests.authority.test_local_run_calibration import FakeAcceptedAnchorReader
from tests.authority.test_local_run_resource import _synthetic_accepted_sources
from tests.media.test_calibration_record_persistence import _runtime_measurement


@pytest.fixture(scope="module")
def installed_fixture(tmp_path_factory: pytest.TempPathFactory):
    sources, anchor = _synthetic_accepted_sources(tmp_path_factory.mktemp("synthetic-installed-startup"))
    raw = emit_locked_local_run_resource(**sources.options, store=FakeAcceptedAnchorReader(anchor))
    return decode_local_run_resource(raw, expected_sha256=sha256_bytes(raw)), anchor


def _bootstrapped(resource, *, entry=None, snapshot=None):
    entry = entry or resource.local_run.timed_speech_registry_entry
    snapshot = snapshot or InstalledLocalRunProfileResolver(resource).snapshot
    reference = CommittedArtifactMemberReference(
        UUID(int=10), UUID(int=11), 0, TIMED_SPEECH_PROFILE_REGISTRY_SCOPE,
        TIMED_SPEECH_PROFILE_REGISTRY_ARTIFACT_TYPE,
        snapshot.enabled_profile.logical_id, 1, entry.canonical_hash,
    )
    return BootstrappedTimedSpeechProfile(snapshot, reference, entry)


class FakeInstalledReadStore(FakeAcceptedAnchorReader):
    """Exactly two read methods: no bootstrap, provider or generic write seam."""

    def __init__(self, anchor, profile, *, calibration_error=None, profile_error=None):
        super().__init__(anchor)
        self.profile = profile
        self.calibration_error = calibration_error
        self.profile_error = profile_error
        self.events = []
        self.snapshots = []

    def read_calibration_record_anchor(self, *args, **kwargs):
        self.events.append("calibration")
        if self.calibration_error is not None:
            raise self.calibration_error
        return super().read_calibration_record_anchor(*args, **kwargs)

    def read_bootstrapped_timed_speech_profile(self, snapshot):
        self.events.append("profile")
        self.snapshots.append(snapshot)
        if self.profile_error is not None:
            raise self.profile_error
        return self.profile


class FakeRuntimeCapabilityStore:
    def __init__(self, capability: PersistedRuntimeCalibrationCapability) -> None:
        self.capability = capability

    def read_runtime_calibration_capability(self, **_kwargs) -> PersistedRuntimeCalibrationCapability:
        return self.capability


def _foreign_anchor(anchor):
    return replace(
        anchor,
        aggregate=replace(anchor.aggregate, reference=replace(anchor.aggregate.reference, receipt_id=UUID(int=99))),
        validation=replace(anchor.validation, reference=replace(anchor.validation.reference, receipt_id=UUID(int=99))),
    )


def _forbid_native(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("installed binding attempted native/process/network access")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)


def test_fake_store_startup_reads_calibration_then_exact_profile_without_native(installed_fixture, monkeypatch):
    resource, anchor = installed_fixture
    profile = _bootstrapped(resource)
    store = FakeInstalledReadStore(anchor, profile)
    resolver = InstalledLocalRunProfileResolver(resource)
    _forbid_native(monkeypatch)

    assert resolver.resolve(store) is profile
    store.events.append("success")
    assert store.events == ["calibration", "profile", "success"]
    entry = resource.local_run.timed_speech_registry_entry
    assert resolver.snapshot == AuthorityRegistrySnapshot(
        resource.current_registry_sha256, TimedSpeechProfileKey(entry.profile_id, entry.profile_version),
    )
    assert store.snapshots == [resolver.snapshot]
    assert store.calls == [(resource.local_run.calibration.record_ref,
                            resource.local_run.calibration.validation_receipt_ref,
                            resource.shadow.source_sha256, resource.predecessor_registry_sha256)]
    assert resolver.resolve(store) is profile  # No cached acceptance across startup checks.
    assert store.events[-2:] == ["calibration", "profile"]


def test_runtime_capability_resolution_is_separate_from_legacy_startup_anchor(installed_fixture) -> None:
    resource, anchor = installed_fixture
    identity = _runtime_measurement()
    resolver = InstalledRuntimeCapabilityResolver(
        RuntimeCalibrationPolicySource(
            resource.shadow.source_sha256,
            resource.predecessor_registry_sha256,
            "sha256:" + "a" * 64,
            "sha256:" + "b" * 64,
            (RuntimeCalibrationCapabilityPolicy("pc_cuda", "cuda"),),
        )
    )
    assert resolver.resolve(FakeRuntimeCapabilityStore(PersistedRuntimeCalibrationCapability(identity, anchor)), identity).anchor == anchor


def test_runtime_capability_keeps_audit_only_rebuild_compatible(installed_fixture) -> None:
    """The capability identity intentionally excludes a harmless service build audit."""
    resource, anchor = installed_fixture
    measured_at_calibration = _runtime_measurement()
    rebuilt = replace(
        measured_at_calibration,
        timing_compatibility=replace(
            measured_at_calibration.timing_compatibility,
            build_audit_sha256="sha256:" + "c" * 64,
        ),
    )
    assert rebuilt.canonical_sha256 == measured_at_calibration.canonical_sha256
    resolver = InstalledRuntimeCapabilityResolver(
        RuntimeCalibrationPolicySource(
            resource.shadow.source_sha256,
            resource.predecessor_registry_sha256,
            "sha256:" + "c" * 64,
            "sha256:" + "d" * 64,
            (RuntimeCalibrationCapabilityPolicy("pc_cuda", "cuda"),),
        )
    )
    capability = PersistedRuntimeCalibrationCapability(measured_at_calibration, anchor)
    assert resolver.resolve(FakeRuntimeCapabilityStore(capability), rebuilt) is capability


def test_installed_resource_derives_closed_runtime_capability_policy(installed_fixture) -> None:
    resource, _ = installed_fixture
    policy = runtime_calibration_policy_for_installed_resource(resource)

    assert policy.profile_source_sha256 == resource.shadow.source_sha256
    assert policy.registry_snapshot_sha256 == resource.predecessor_registry_sha256
    assert tuple(item.to_mapping() for item in policy.capabilities) == (
        {"runtime_capability_id": "mac_cpu", "device_class": "cpu"},
        {"runtime_capability_id": "pc_cuda", "device_class": "cuda"},
    )
    assert policy == runtime_calibration_policy_for_installed_resource(resource)


def test_valid_altered_guard_with_same_key_and_consistent_member_hash_is_rejected(installed_fixture):
    resource, anchor = installed_fixture
    entry = resource.local_run.timed_speech_registry_entry
    altered = replace(entry, guard_policy=replace(entry.guard_policy, pre_roll_tick=entry.guard_policy.pre_roll_tick + 1))
    profile = _bootstrapped(resource, entry=altered)
    assert (altered.profile_id, altered.profile_version) == (entry.profile_id, entry.profile_version)
    assert profile.reference.content_hash == altered.canonical_hash != entry.canonical_hash
    store = FakeInstalledReadStore(anchor, profile)
    with pytest.raises(InstalledLocalRunError, match="differs from installed"):
        InstalledLocalRunProfileResolver(resource).resolve(store)
    assert store.events == ["calibration", "profile"]


def test_foreign_accepted_anchor_fails_before_profile_read(installed_fixture):
    resource, anchor = installed_fixture
    store = FakeInstalledReadStore(_foreign_anchor(anchor), _bootstrapped(resource))
    with pytest.raises(CalibrationBindingError, match="exact local-run references"):
        InstalledLocalRunProfileResolver(resource).resolve(store)
    assert store.events == ["calibration"] and store.snapshots == []


@pytest.mark.parametrize("stage", ("calibration", "profile"))
def test_store_failure_propagates_once_without_fallback(installed_fixture, stage):
    resource, anchor = installed_fixture
    failure = LookupError("explicit fake Store failure")
    store = FakeInstalledReadStore(anchor, _bootstrapped(resource), **{f"{stage}_error": failure})
    with pytest.raises(LookupError) as caught:
        InstalledLocalRunProfileResolver(resource).resolve(store)
    assert caught.value is failure
    assert store.events == (["calibration"] if stage == "calibration" else ["calibration", "profile"])


@pytest.mark.parametrize("wrong", ("type", "snapshot"))
def test_existing_store_resolver_checks_are_preserved(installed_fixture, wrong):
    resource, anchor = installed_fixture
    snapshot = replace(InstalledLocalRunProfileResolver(resource).snapshot, registry_set_sha256="sha256:" + "f" * 64)
    profile = object() if wrong == "type" else _bootstrapped(resource, snapshot=snapshot)
    store = FakeInstalledReadStore(anchor, profile)
    with pytest.raises(TimedSpeechRegistryError):
        InstalledLocalRunProfileResolver(resource).resolve(store)
    assert store.events == ["calibration", "profile"]


def test_resolver_is_frozen_and_requires_exact_resource(installed_fixture):
    resource, _ = installed_fixture
    resolver = InstalledLocalRunProfileResolver(resource)
    with pytest.raises(FrozenInstanceError):
        resolver.resource = resource
    with pytest.raises(InstalledLocalRunError, match="exact decoded"):
        InstalledLocalRunProfileResolver(object())


def test_fixed_resolver_loader_takes_no_overrides(installed_fixture, monkeypatch):
    resource, _ = installed_fixture
    calls = []

    def fixed_loader():
        calls.append("fixed")
        return resource

    monkeypatch.setattr(installed_runtime, "load_installed_local_run_resource", fixed_loader)
    assert load_installed_local_run_resolver().resource is resource
    assert calls == ["fixed"]
    with pytest.raises(TypeError):
        load_installed_local_run_resolver(resource=resource)
    assert calls == ["fixed"]


def test_fixed_loader_failure_has_no_fallback(monkeypatch):
    failure = LocalRunResourceError("installed resource unavailable")
    calls = []

    def unavailable():
        calls.append("fixed")
        raise failure

    monkeypatch.setattr(installed_runtime, "load_installed_local_run_resource", unavailable)
    with pytest.raises(LocalRunResourceError) as caught:
        load_installed_local_run_resolver()
    assert caught.value is failure and calls == ["fixed"]
