"""Structural-only coverage for the B2a ExternalJobRequest payload source."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
COMMON_SOURCE = (
    REPOSITORY_ROOT
    / "packages"
    / "autocut-kernel"
    / "src"
    / "autocut_kernel"
    / "contracts"
    / "source"
    / "2_1_3"
    / "common"
)
BOOTSTRAP_SOURCE = COMMON_SOURCE / "schemas" / "bootstrap"
PRIMITIVES_SOURCE = COMMON_SOURCE / "schemas" / "primitives"
SCHEMA_FILENAME = "external-job-request.schema.json"
SCHEMA_ID = "https://autocut.invalid/contracts/2.1.3/common/schemas/bootstrap/" + SCHEMA_FILENAME
ARTIFACT_REF_ID = "https://autocut.invalid/contracts/2.1.3/common/schemas/primitives/artifact-ref.schema.json"
AUTHORITY_HASH = "sha256:9418faf746c2a710219b060a9c5ec020bedad1927e6516975eb2938943d54707"


def _schema(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _validator() -> Draft202012Validator:
    schema = _schema(BOOTSTRAP_SOURCE / SCHEMA_FILENAME)
    artifact_ref = _schema(PRIMITIVES_SOURCE / "artifact-ref.schema.json")
    registry = Registry().with_resources(
        (resource["$id"], Resource.from_contents(resource)) for resource in (schema, artifact_ref)
    )
    return Draft202012Validator(schema, registry=registry, format_checker=FormatChecker())


def _valid_payload() -> dict[str, object]:
    return {
        "external_job_request_id": "external-request-001",
        "external_request_source": "trusted-ingress-001",
        "external_request_id": "request-001",
        "job_id": "job-001",
        "requested_root_input_id": "root-input-001",
        "root_prerequisite_refs": [
            {"artifact_id": "artifact-001", "content_hash": "sha256:" + "a" * 64}
        ],
        "received_at": "2026-08-23T12:34:56.789Z",
    }


def _assert_invalid(value: object) -> None:
    assert list(_validator().iter_errors(value)), value


def test_external_job_request_schema_is_closed_payload_only_and_authority_bound() -> None:
    schema = _schema(BOOTSTRAP_SOURCE / SCHEMA_FILENAME)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"] == SCHEMA_ID
    assert AUTHORITY_HASH in schema["$comment"]
    assert "payload-only" in schema["$comment"]
    assert set(schema["properties"]) == {
        "external_job_request_id",
        "external_request_source",
        "external_request_id",
        "job_id",
        "requested_root_input_id",
        "root_prerequisite_refs",
        "received_at",
    }
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["additionalProperties"] is False
    assert schema["properties"]["root_prerequisite_refs"]["items"]["$ref"] == ARTIFACT_REF_ID


def test_external_job_request_accepts_complete_closed_payload() -> None:
    assert _validator().is_valid(_valid_payload())


@pytest.mark.parametrize(
    "payload",
    [
        {key: value for key, value in _valid_payload().items() if key != "job_id"},
        {**_valid_payload(), "unexpected": True},
        {**_valid_payload(), "external_request_source": ""},
        {**_valid_payload(), "root_prerequisite_refs": []},
        {**_valid_payload(), "root_prerequisite_refs": [{"artifact_id": "artifact-001", "content_hash": "sha256:" + "A" * 64}]},
        {**_valid_payload(), "root_prerequisite_refs": ["artifact-001"]},
        {**_valid_payload(), "received_at": "2026-08-23T12:34:56.789+00:00"},
        {**_valid_payload(), "received_at": "2026-08-23T12:34:56.78Z"},
        {**_valid_payload(), "received_at": "2026-02-30T12:34:56.789Z"},
        {**_valid_payload(), "requested_root_input_id": None},
        {**_valid_payload(), "root_input_set_ref": None},
        {**_valid_payload(), "job_policy_ref": {"artifact_id": "policy", "content_hash": "sha256:" + "b" * 64}},
    ],
)
def test_external_job_request_rejects_nonstructural_or_future_payload_fields(payload: object) -> None:
    _assert_invalid(payload)


def test_external_job_request_schema_does_not_claim_deferred_semantics() -> None:
    """Ordering, duplicate checks, scope, Envelope inputs, commitment, and writers are later gates."""

    payload = _valid_payload()
    ref = payload["root_prerequisite_refs"][0]
    payload["root_prerequisite_refs"] = [ref, ref]
    assert _validator().is_valid(payload)
