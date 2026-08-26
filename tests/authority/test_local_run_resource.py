"""Synthetic Git + fake accepted Store tests, never production calibration.

Both A/B/C chains are real temporary Git histories. The explicitly fake reader
provides internally closed record fixtures, not proof of database acceptance.
"""

from __future__ import annotations

import base64
import json
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from authority.errors import GateViolation
from authority.local_run_resource import emit_locked_local_run_resource
from authority.shadow_context import build_locked_shadow_context
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes, sha256_bytes
from autocut_kernel.registry.installed_local_run import (
    compute_local_profile_registry_sha256,
    decode_local_run_resource,
)
from autocut_kernel.store.models import PersistedCalibrationRecordAnchor

from tests.authority.test_local_run_calibration import (
    FakeAcceptedAnchorReader,
    _fixture_anchor,
    _fixture_record,
    _project,
)
from tests.authority.test_local_run_context import _local_sources
from tests.authority.test_lock_and_schema import _git
from tests.authority.test_shadow_context import Sources


def _synthetic_accepted_sources(root: Path, mutation: str = ""):
    captured: list[PersistedCalibrationRecordAnchor] = []

    def customize(run: dict[str, Any], old: Sources) -> None:
        anchor = _fixture_anchor(_fixture_record(build_locked_shadow_context(**old.options)))
        captured.append(anchor)
        _project(run, anchor)

    sources = _local_sources(root, mutation, customize_run=customize)
    return sources, captured[0]


@pytest.fixture(scope="module")
def synthetic_accepted_sources(tmp_path_factory: pytest.TempPathFactory):
    return _synthetic_accepted_sources(tmp_path_factory.mktemp("synthetic-resource-emission"))


def _files(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*") if path.is_file() and ".git" not in path.relative_to(root).parts}


def test_real_synthetic_chains_and_fake_store_emit_exact_raw_sources(synthetic_accepted_sources) -> None:
    sources, anchor = synthetic_accepted_sources
    reader = FakeAcceptedAnchorReader(anchor)
    before = _files(sources.root)
    raw = emit_locked_local_run_resource(**sources.options, store=reader)
    assert raw == canonical_json_bytes(json.loads(raw))
    decoded = decode_local_run_resource(raw, expected_sha256=sha256_bytes(raw))
    document = json.loads(raw)
    assert set(document) == {"schema_version", "current", "predecessor"}
    assert document["schema_version"] == "installed-local-run-authority-v1"
    assert len(sources.lock["entries"]) == len(sources.old.lock["entries"]) == 3
    for name, kind, narrative, profile, schema, lock in (
        ("current", "local_run_v1", sources.old.narrative_raw, sources.local_run_raw, sources.schema_raw, sources.lock),
        ("predecessor", "shadow_calibration_v1", sources.old.narrative_raw, sources.old.shadow_raw, sources.old.schema_raw, sources.old.lock),
    ):
        chain = document[name]
        assert set(chain) == {"registry_set_sha256", "authority_lock_sha256", "narrative_raw_base64", "profile_raw_base64", "schema_raw_base64"}
        for field, original in (("narrative_raw_base64", narrative), ("profile_raw_base64", profile), ("schema_raw_base64", schema)):
            assert base64.b64decode(chain[field], validate=True) == original
            assert chain[field] == base64.b64encode(original).decode("ascii")
        assert chain["registry_set_sha256"] == compute_local_profile_registry_sha256(
            profile_kind=kind, narrative_raw=narrative, profile_raw=profile, schema_raw=schema,
        )
        assert chain["authority_lock_sha256"] == lock["bundle_hash"]
    assert decoded.local_run.calibration.record_ref == anchor.aggregate.reference
    assert decoded.local_run.calibration.validation_receipt_ref == anchor.validation.reference
    assert (anchor.aggregate.reference.member_ordinal, anchor.validation.reference.member_ordinal) == (0, 3)
    assert reader.calls == [(anchor.aggregate.reference, anchor.validation.reference,
                             sha256_bytes(sources.old.shadow_raw), decoded.predecessor_registry_sha256)]
    assert _files(sources.root) == before
    assert _git(sources.root, "status", "--porcelain") == ""


def test_repeat_emission_is_deterministic_but_rechecks_fake_store(synthetic_accepted_sources) -> None:
    sources, anchor = synthetic_accepted_sources
    reader = FakeAcceptedAnchorReader(anchor)
    first = emit_locked_local_run_resource(**sources.options, store=reader)
    assert emit_locked_local_run_resource(**sources.options, store=reader) == first
    assert len(reader.calls) == 2 and reader.calls[0] == reader.calls[1]


def test_dirty_checkout_and_index_do_not_enter_emitted_resource(tmp_path: Path) -> None:
    sources, anchor = _synthetic_accepted_sources(tmp_path)
    reader = FakeAcceptedAnchorReader(anchor)
    original = emit_locked_local_run_resource(**sources.options, store=reader)
    for entry in (*sources.lock["entries"], *sources.old.lock["entries"]):
        (sources.root / entry["path"]).write_bytes(b"dirty checkout source, not authority")
    (sources.root / "authority-lock.yaml").write_bytes(b"dirty lock")
    _git(sources.root, "add", "-A")
    before = _files(sources.root)
    assert emit_locked_local_run_resource(**sources.options, store=reader) == original
    assert _files(sources.root) == before


def test_unavailable_fake_accepted_store_propagates_once_without_output_or_native(
    synthetic_accepted_sources, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources, _ = synthetic_accepted_sources
    before = _files(sources.root)
    failure = LookupError("synthetic accepted anchor unavailable")

    class FakeUnavailableAcceptedStore:
        calls = 0

        def read_calibration_record_anchor(self, *args, **kwargs):
            self.calls += 1
            raise failure

    # Git is the only subprocess allowed while the real source builders run.
    # Inference tools cannot be silently used to repair missing acceptance.
    original_run = subprocess.run
    git_calls = []

    def git_only(command, *args, **kwargs):
        assert command[0] == "git", "resource emitter attempted a non-Git/native process"
        git_calls.append(command)
        return original_run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", git_only)
    reader = FakeUnavailableAcceptedStore()
    with pytest.raises(LookupError) as caught:
        emit_locked_local_run_resource(**sources.options, store=reader)
    assert caught.value is failure and reader.calls == 1
    assert git_calls and _files(sources.root) == before


def test_foreign_fake_accepted_anchor_cannot_be_packaged(synthetic_accepted_sources) -> None:
    sources, anchor = synthetic_accepted_sources
    foreign = replace(anchor,
        aggregate=replace(anchor.aggregate, reference=replace(anchor.aggregate.reference, receipt_id=UUID(int=99))),
        validation=replace(anchor.validation, reference=replace(anchor.validation.reference, receipt_id=UUID(int=99))),
    )
    reader = FakeAcceptedAnchorReader(foreign)
    before = _files(sources.root)
    with pytest.raises(GateViolation, match="exact local-run references"):
        emit_locked_local_run_resource(**sources.options, store=reader)
    assert len(reader.calls) == 1 and _files(sources.root) == before


@pytest.mark.parametrize("mutation", ("registry-contract", "class-local-run", "current-lock-drift"))
def test_invalid_synthetic_source_is_rejected_before_any_accepted_store_read(tmp_path: Path, mutation: str) -> None:
    sources, anchor = _synthetic_accepted_sources(tmp_path, mutation)
    reader = FakeAcceptedAnchorReader(anchor)
    before = _files(sources.root)
    with pytest.raises(GateViolation):
        emit_locked_local_run_resource(**sources.options, store=reader)
    assert reader.calls == [] and _files(sources.root) == before


@pytest.mark.parametrize("argument", ("context", "snapshot", "authority_snapshot"))
def test_emitter_has_no_caller_context_or_snapshot_escape_hatch(synthetic_accepted_sources, argument: str) -> None:
    sources, anchor = synthetic_accepted_sources
    reader = FakeAcceptedAnchorReader(anchor)
    with pytest.raises(TypeError):
        emit_locked_local_run_resource(**sources.options, store=reader, **{argument: object()})
    assert reader.calls == []
