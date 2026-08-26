"""Explicit build staging for synthetic local-run authority resources only."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from authority.errors import GateViolation
from authority.local_run_packaging import prepare_locked_local_run_package
from autocut_kernel.contracts.compiler.canonical import sha256_bytes
from autocut_kernel.registry.installed_local_run import decode_local_run_resource

from tests.authority.test_local_run_calibration import FakeAcceptedAnchorReader
from tests.authority.test_local_run_resource import _synthetic_accepted_sources


def _destination(tmp_path: Path) -> Path:
    destination = tmp_path / "build" / "src" / "autocut_kernel"
    destination.mkdir(parents=True)
    return destination


def test_packager_emits_only_fixed_resource_and_digest_into_controlled_staging(tmp_path: Path) -> None:
    sources, anchor = _synthetic_accepted_sources(tmp_path)
    destination = _destination(tmp_path)
    output = prepare_locked_local_run_package(
        **sources.options,
        store=FakeAcceptedAnchorReader(anchor),
        destination_kernel_package=destination,
    )
    raw = (output / "local-run.json").read_bytes()
    assert output == destination / "_authority"
    assert {path.name for path in output.iterdir()} == {"local-run.json", "local-run.sha256"}
    assert (output / "local-run.sha256").read_bytes() == sha256_bytes(raw).encode("ascii") + b"\n"
    assert decode_local_run_resource(raw, expected_sha256=sha256_bytes(raw)).local_run.profile_version == "1"
    assert not (destination / ".local-run-authority-publish.lock").exists()
    assert not list(destination.glob(".local-run-authority-*"))


@pytest.mark.parametrize("kind", ("relative", "missing", "symlink"))
def test_packager_rejects_uncontrolled_destinations_before_emission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    sources, anchor = _synthetic_accepted_sources(tmp_path)
    destination = _destination(tmp_path)
    if kind == "relative":
        candidate = Path("relative-kernel-package")
    elif kind == "missing":
        candidate = tmp_path / "missing-kernel-package"
    else:
        candidate = tmp_path / "symlink-kernel-package"
        candidate.symlink_to(destination, target_is_directory=True)
    called = False

    def never_emit(**kwargs):
        nonlocal called
        called = True
        raise AssertionError("emitter must not run for an invalid destination")

    monkeypatch.setattr("authority.local_run_packaging.emit_locked_local_run_resource", never_emit)
    with pytest.raises(GateViolation, match="AUTH-PACKAGE-DESTINATION"):
        prepare_locked_local_run_package(
            **sources.options,
            store=FakeAcceptedAnchorReader(anchor),
            destination_kernel_package=candidate,
        )
    assert not called


def test_packager_refuses_existing_output_without_emission_or_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources, anchor = _synthetic_accepted_sources(tmp_path)
    destination = _destination(tmp_path)
    output = destination / "_authority"
    output.mkdir()
    sentinel = output / "keep"
    sentinel.write_bytes(b"existing build authority")
    monkeypatch.setattr(
        "authority.local_run_packaging.emit_locked_local_run_resource",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("emitter must not run")),
    )
    with pytest.raises(GateViolation, match="AUTH-PACKAGE-EXISTS"):
        prepare_locked_local_run_package(
            **sources.options,
            store=FakeAcceptedAnchorReader(anchor),
            destination_kernel_package=destination,
        )
    assert sentinel.read_bytes() == b"existing build authority"


def test_packager_keeps_no_partial_resource_when_emission_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources, anchor = _synthetic_accepted_sources(tmp_path)
    destination = _destination(tmp_path)
    failure = LookupError("synthetic accepted record unavailable")
    monkeypatch.setattr(
        "authority.local_run_packaging.emit_locked_local_run_resource",
        lambda **kwargs: (_ for _ in ()).throw(failure),
    )
    with pytest.raises(LookupError) as caught:
        prepare_locked_local_run_package(
            **sources.options,
            store=FakeAcceptedAnchorReader(anchor),
            destination_kernel_package=destination,
        )
    assert caught.value is failure
    assert not os.path.lexists(destination / "_authority")
    assert not list(destination.glob(".local-run-authority-*"))
    assert not os.path.lexists(destination / ".local-run-authority-publish.lock")


def test_packager_rechecks_output_after_emission_before_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources, anchor = _synthetic_accepted_sources(tmp_path)
    destination = _destination(tmp_path)

    def competing_emit(**kwargs) -> bytes:
        (destination / "_authority").mkdir()
        return b"synthetic bytes"

    monkeypatch.setattr("authority.local_run_packaging.emit_locked_local_run_resource", competing_emit)
    with pytest.raises(GateViolation, match="AUTH-PACKAGE-EXISTS"):
        prepare_locked_local_run_package(
            **sources.options,
            store=FakeAcceptedAnchorReader(anchor),
            destination_kernel_package=destination,
        )
    assert (destination / "_authority").is_dir()
    assert not list(destination.glob(".local-run-authority-*"))
    assert not os.path.lexists(destination / ".local-run-authority-publish.lock")
