"""Structural-only coverage for the B2b JobStartSlot payload source."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
COMMON_SOURCE = REPOSITORY_ROOT / "packages" / "autocut-kernel" / "src" / "autocut_kernel" / "contracts" / "source" / "2_1_3" / "common"
BOOTSTRAP_SOURCE = COMMON_SOURCE / "schemas" / "bootstrap"
PRIMITIVES_SOURCE = COMMON_SOURCE / "schemas" / "primitives"
SCHEMA_FILENAME = "job-start-slot.schema.json"
SCHEMA_ID = "https://autocut.invalid/contracts/2.1.3/common/schemas/bootstrap/" + SCHEMA_FILENAME
ARTIFACT_REF_ID = "https://autocut.invalid/contracts/2.1.3/common/schemas/primitives/artifact-ref.schema.json"
ARTIFACT_SET_REF_ID = "https://autocut.invalid/contracts/2.1.3/common/schemas/primitives/artifact-set-ref.schema.json"
AUTHORITY_HASH = "sha256:c34af7451919ad9a895644b40136062834b7ba9e857139f10b61f7dc51be67e9"
COMMON_FIELDS = {
    "job_start_slot_id",
    "job_execution_id",
    "state",
    "external_job_request_ref",
    "root_input_validation_result_ref",
    "root_input_set_ref",
    "root_job_policy_ref",
    "updated_at",
}


def _schema(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def validator(source_schema_validator):
    resources = [_schema(BOOTSTRAP_SOURCE / SCHEMA_FILENAME)]
    resources.extend(
        _schema(PRIMITIVES_SOURCE / filename)
        for filename in ("artifact-ref.schema.json", "artifact-set-ref.schema.json")
    )
    return source_schema_validator(resources[0], resources[1:])


def _artifact_ref(name: str) -> dict[str, str]:
    return {"artifact_id": name, "content_hash": "sha256:" + "a" * 64}


def _reserved_payload() -> dict[str, object]:
    return {
        "job_start_slot_id": "jobstartslot-001",
        "job_execution_id": "jobexec-001",
        "state": "reserved",
        "external_job_request_ref": _artifact_ref("external-request"),
        "root_input_validation_result_ref": _artifact_ref("validation-result"),
        "root_input_set_ref": {"artifact_set_id": "root-input-set", "set_hash": "sha256:" + "c" * 64},
        "root_job_policy_ref": _artifact_ref("policy"),
        "updated_at": "2026-08-23T12:34:56.789Z",
    }


def _active_payload() -> dict[str, object]:
    payload = _reserved_payload()
    payload.update(
        {
            "state": "active",
            "prior_slot_ref": _artifact_ref("reserved-slot"),
            "root_run_manifest_ref": _artifact_ref("root-run"),
            "authority_run_manifest_ref": _artifact_ref("root-run"),
            "run_lineage_id": "runline-001",
            "publication_lineage_id": "publine-001",
            "derived_run_manifest_refs": [],
        }
    )
    return payload


def _assert_invalid(validator, value: object) -> None:
    assert list(validator.iter_errors(value)), value


def test_job_start_slot_schema_is_closed_payload_only_and_authority_bound() -> None:
    schema = _schema(BOOTSTRAP_SOURCE / SCHEMA_FILENAME)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"] == SCHEMA_ID
    assert AUTHORITY_HASH in schema["$comment"]
    assert "payload-only" in schema["$comment"]
    assert "explicitly deferred" in schema["$comment"]
    assert len(schema["oneOf"]) == 2
    branches = {branch["properties"]["state"]["const"]: branch for branch in schema["oneOf"]}
    assert set(branches) == {"reserved", "active"}
    assert set(branches["reserved"]["required"]) == COMMON_FIELDS
    assert set(branches["active"]["required"]) == COMMON_FIELDS | {
        "prior_slot_ref", "root_run_manifest_ref", "authority_run_manifest_ref", "run_lineage_id",
        "publication_lineage_id", "derived_run_manifest_refs",
    }
    assert all(branch["additionalProperties"] is False for branch in branches.values())
    assert branches["active"]["properties"]["prior_slot_ref"]["$ref"] == ARTIFACT_REF_ID
    assert branches["active"]["properties"]["root_input_set_ref"]["$ref"] == ARTIFACT_SET_REF_ID


def test_job_start_slot_accepts_reserved_and_first_active_payload_shapes(validator) -> None:
    assert validator.is_valid(_reserved_payload())
    assert validator.is_valid(_active_payload())


@pytest.mark.parametrize(
    "field",
    ["prior_slot_ref", "root_run_manifest_ref", "authority_run_manifest_ref", "run_lineage_id", "publication_lineage_id", "derived_run_manifest_refs"],
)
def test_job_start_slot_reserved_rejects_every_active_only_field(validator, field: str) -> None:
    payload = _reserved_payload()
    payload[field] = [] if field == "derived_run_manifest_refs" else _artifact_ref("future")
    _assert_invalid(validator, payload)


@pytest.mark.parametrize(
    "field",
    ["prior_slot_ref", "root_run_manifest_ref", "authority_run_manifest_ref", "run_lineage_id", "publication_lineage_id", "derived_run_manifest_refs"],
)
def test_job_start_slot_active_requires_every_active_only_field(validator, field: str) -> None:
    payload = _active_payload()
    del payload[field]
    _assert_invalid(validator, payload)


@pytest.mark.parametrize(
    "payload",
    [
        {key: value for key, value in _reserved_payload().items() if key != "job_execution_id"},
        {**_reserved_payload(), "unexpected": True},
        {**_active_payload(), "prior_slot_ref": None},
        {**_active_payload(), "root_run_manifest_ref": "root-run"},
        {**_active_payload(), "authority_run_manifest_ref": {"artifact_id": "root-run"}},
        {**_active_payload(), "root_input_set_ref": _artifact_ref("not-a-set")},
        {**_active_payload(), "derived_run_manifest_refs": ["derived-run"]},
        {**_active_payload(), "derived_run_manifest_refs": [None]},
        {**_active_payload(), "run_lineage_id": None},
        {**_active_payload(), "updated_at": "2026-08-23T12:34:56.789+00:00"},
        {**_active_payload(), "updated_at": "2026-08-23T12:34:56.78Z"},
        {**_active_payload(), "updated_at": "2026-02-30T12:34:56.789Z"},
        {**_active_payload(), "state": "pending"},
    ],
)
def test_job_start_slot_rejects_missing_mixed_unknown_null_or_malformed_values(validator, payload: object) -> None:
    _assert_invalid(validator, payload)


def test_job_start_slot_schema_does_not_claim_deferred_cross_revision_semantics(validator) -> None:
    """Duplicate, prefix, and cross-ref equality checks belong to later semantic gates."""

    payload = _active_payload()
    payload["authority_run_manifest_ref"] = _artifact_ref("different-authority-run")
    derived = _artifact_ref("derived-run")
    payload["derived_run_manifest_refs"] = [derived, deepcopy(derived)]
    assert validator.is_valid(payload)


@pytest.mark.parametrize("payload_builder", [_reserved_payload, _active_payload])
def test_job_start_slot_rejects_calendar_invalid_updated_at_in_both_branches(validator, payload_builder) -> None:
    payload = {**payload_builder(), "updated_at": "2026-02-30T12:34:56.789Z"}
    _assert_invalid(validator, payload)
