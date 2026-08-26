"""Succeeded predecessor transport preserves the Job, not just equality fields."""

from dataclasses import replace
from uuid import UUID

import pytest
from autocut_kernel.pipeline.committed_outcome import (
    succeeded_outcome_from_mapping,
    succeeded_outcome_mapping,
)
from autocut_kernel.store.models import CommandOutcome


def outcome():
    return CommandOutcome(UUID(int=1), "succeeded", receipt_id=UUID(int=2),
                          artifact_set_id=UUID(int=3), job_id=UUID(int=4))


def test_identity_roundtrip_includes_job_and_excludes_freshness():
    original = outcome()
    wire = succeeded_outcome_mapping(original)
    restored = succeeded_outcome_from_mapping(wire)
    assert restored == original and restored.job_id == original.job_id
    assert succeeded_outcome_mapping(replace(original, is_fresh_claim=True)) == wire
    foreign = replace(original, job_id=UUID(int=5))
    assert foreign == original  # Dataclass equality alone is insufficient.
    assert succeeded_outcome_mapping(foreign) != wire


@pytest.mark.parametrize("change", [
    {"state": "failed"}, {"state": "running"}, {"job_id": None},
    {"receipt_id": None}, {"artifact_set_id": None}, {"command_slot_id": "not-a-uuid"},
    {"is_fresh_claim": 1}, {"failure_code": "error"}, {"failure_detail_json": "{}"},
])
def test_incomplete_or_mistyped_outcome_rejected(change):
    with pytest.raises(ValueError):
        succeeded_outcome_mapping(replace(outcome(), **change))


@pytest.mark.parametrize("change", [
    {"state": "failed"}, {"job_id": None}, {"job_id": UUID(int=1)},
    {"receipt_id": "{00000000-0000-0000-0000-000000000002}"},
    {"artifact_set_id": "00000000000000000000000000000003"}, {"pass": True},
])
def test_wire_is_closed_and_uuid_spelling_exact(change):
    with pytest.raises(ValueError):
        succeeded_outcome_from_mapping({**succeeded_outcome_mapping(outcome()), **change})
