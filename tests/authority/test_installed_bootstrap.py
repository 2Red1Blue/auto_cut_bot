"""Admin tests use synthetic installed content and an explicitly fake Store."""

from __future__ import annotations

import pytest
from autocut_kernel.registry import installed_bootstrap
from autocut_kernel.registry.calibration_binding import CalibrationBindingError
from autocut_kernel.registry.installed_bootstrap import bootstrap_installed_local_run
from autocut_kernel.registry.installed_local_run import LocalRunResourceError
from autocut_kernel.registry.installed_runtime import InstalledLocalRunProfileResolver
from autocut_kernel.registry.timed_speech import (
    AUTHORITY_BOOTSTRAP_JOB,
    BOOTSTRAP_TIMED_SPEECH_PROFILE_REGISTRY_COMMAND,
    VerifiedTimedSpeechAuthorityContext,
)

from tests.authority.test_installed_runtime import (
    _bootstrapped,
    _forbid_native,
    _foreign_anchor,
)
from tests.authority.test_installed_runtime import installed_fixture as installed_fixture
from tests.authority.test_local_run_calibration import FakeAcceptedAnchorReader
from tests.store.test_timed_speech_authority_registry import _BootstrapStore


class FakeInstalledBootstrapStore(_BootstrapStore, FakeAcceptedAnchorReader):
    """Existing command fake plus an explicit accepted-anchor fixture reader."""

    def __init__(self, resource, anchor):
        _BootstrapStore.__init__(self)
        FakeAcceptedAnchorReader.__init__(self, anchor)
        self.events = []
        self.resolved = _bootstrapped(resource)

    def read_calibration_record_anchor(self, *args, **kwargs):
        self.events.append("calibration")
        return FakeAcceptedAnchorReader.read_calibration_record_anchor(self, *args, **kwargs)

    def claim_command(self, claim):
        self.events.append("claim")
        return super().claim_command(claim)

    def commit_timed_speech_profile_bootstrap(self, success, snapshot):
        self.events.append("commit")
        self.outcome = super().commit_timed_speech_profile_bootstrap(success, snapshot)
        return self.outcome

    def read_bootstrapped_timed_speech_profile(self, snapshot):
        self.events.append("profile")
        return super().read_bootstrapped_timed_speech_profile(snapshot)


def _fixed_loader(monkeypatch, resource, events):
    def load():
        events.append("load")
        return resource

    monkeypatch.setattr(installed_bootstrap, "load_installed_local_run_resource", load)


def test_admin_fake_store_bootstraps_once_then_delegates_successful_replay(installed_fixture, monkeypatch):
    resource, anchor = installed_fixture
    store = FakeInstalledBootstrapStore(resource, anchor)
    _fixed_loader(monkeypatch, resource, store.events)
    _forbid_native(monkeypatch)

    outcome = bootstrap_installed_local_run(store)
    assert outcome.state == "succeeded"
    assert store.events == ["load", "calibration", "claim", "commit"]
    request = VerifiedTimedSpeechAuthorityContext(
        InstalledLocalRunProfileResolver(resource).snapshot, resource.local_run.timed_speech_registry_entry,
    ).bootstrap_request()
    claim = store.claims[0]
    assert claim.job == AUTHORITY_BOOTSTRAP_JOB
    assert claim.command_name == BOOTSTRAP_TIMED_SPEECH_PROFILE_REGISTRY_COMMAND
    assert claim.request_hash == request.request_hash
    assert claim.idempotency_key == request.idempotency_key
    success, snapshot = store.commits[0]
    assert snapshot == request.snapshot and success.artifacts == (request.artifact(),)

    assert bootstrap_installed_local_run(store) is outcome
    assert store.events == ["load", "calibration", "claim", "commit", "load", "calibration", "claim", "profile"]
    assert len(store.commits) == 1 and store.claims == [claim, claim]
    assert len(store.calls) == 2 and store.rejections == []


def test_admin_missing_installed_resource_stops_before_any_store_call(installed_fixture, monkeypatch):
    resource, anchor = installed_fixture
    store = FakeInstalledBootstrapStore(resource, anchor)
    failure = LocalRunResourceError("installed resource unavailable")

    def unavailable():
        store.events.append("load")
        raise failure

    monkeypatch.setattr(installed_bootstrap, "load_installed_local_run_resource", unavailable)
    _forbid_native(monkeypatch)
    with pytest.raises(LocalRunResourceError) as caught:
        bootstrap_installed_local_run(store)
    assert caught.value is failure and store.events == ["load"]
    assert store.calls == store.claims == store.commits == store.rejections == []


def test_admin_foreign_calibration_stops_before_claim_without_native(installed_fixture, monkeypatch):
    resource, anchor = installed_fixture
    store = FakeInstalledBootstrapStore(resource, _foreign_anchor(anchor))
    _fixed_loader(monkeypatch, resource, store.events)
    _forbid_native(monkeypatch)
    with pytest.raises(CalibrationBindingError, match="exact local-run references"):
        bootstrap_installed_local_run(store)
    assert store.events == ["load", "calibration"]
    assert len(store.calls) == 1 and store.claims == store.commits == store.rejections == []


@pytest.mark.parametrize("stage", ("calibration", "claim", "commit", "profile"))
def test_admin_store_failures_propagate_once_without_retry(installed_fixture, monkeypatch, stage):
    resource, anchor = installed_fixture
    store = FakeInstalledBootstrapStore(resource, anchor)
    _fixed_loader(monkeypatch, resource, store.events)
    if stage == "profile":
        bootstrap_installed_local_run(store)
        store.events.clear()
    failure = LookupError("explicit fake Store failure")

    def unavailable(*args, **kwargs):
        store.events.append(stage)
        raise failure

    method = {
        "calibration": "read_calibration_record_anchor", "claim": "claim_command",
        "commit": "commit_timed_speech_profile_bootstrap", "profile": "read_bootstrapped_timed_speech_profile",
    }[stage]
    monkeypatch.setattr(store, method, unavailable)
    with pytest.raises(LookupError) as caught:
        bootstrap_installed_local_run(store)
    assert caught.value is failure
    assert store.events == ["load", "calibration"] + ([] if stage == "calibration" else ["claim"]) + (
        [stage] if stage in ("commit", "profile") else []
    )


@pytest.mark.parametrize("argument", ("resource", "snapshot", "profile", "path"))
def test_admin_exposes_no_caller_profile_or_resource_override(installed_fixture, monkeypatch, argument):
    resource, anchor = installed_fixture
    store = FakeInstalledBootstrapStore(resource, anchor)
    _fixed_loader(monkeypatch, resource, store.events)
    with pytest.raises(TypeError):
        bootstrap_installed_local_run(store, **{argument: object()})
    assert store.events == []
