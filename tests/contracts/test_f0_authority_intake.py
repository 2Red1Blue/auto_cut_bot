from __future__ import annotations

import json
import os
from pathlib import Path

import jsonschema
import pytest
from autocut_kernel.contracts.f_authority_intake.verify_inputs import verify_input_manifest
from autocut_kernel.contracts.f_authority_intake.verify_ledger import verify_ledger


def _authority_repository() -> Path:
    value = os.environ.get("AUTOCUT_AUTHORITY_REPOSITORY")
    if not value or not (Path(value) / ".git").exists():
        pytest.skip(
            "set AUTOCUT_AUTHORITY_REPOSITORY to run authority intake tests"
        )
    return Path(value)


def test_packaged_f0_authority_intake_is_git_pinned_and_closed() -> None:
    root = Path(__file__).parents[2]
    source = (
        root
        / "packages/autocut-kernel/src/autocut_kernel/contracts/source/2_1_3/commands/f-authority-intake"
    )
    manifest = source / "f-authority-input.manifest.json"
    payload = json.loads(manifest.read_text())
    assert payload["profiles"] == [
        {
            "profile_id": "authority_intake_v1",
            "required_pin_ids": sorted(pin["pin_id"] for pin in payload["inputs"]),
        }
    ]
    counts = {
        pin["pin_id"]: len(pin["source_blobs"])
        for pin in payload["inputs"]
        if pin["kind"] != "availability_record"
    }
    assert counts == {
        "A": 1,
        "B": 10,
        "C1": 1,
        "C2": 1,
        "C3": 1,
        "C4": 1,
        "C5": 1,
        "authority": 10,
        "errata.execution": 7,
        "errata.recovery": 7,
    }
    assert [pin for pin in payload["inputs"] if pin["kind"] == "availability_record"] == [
        {
            "availability": "not_supplied",
            "kind": "availability_record",
            "pin_id": "D",
            "reason_code": "owner_handoff_absent",
            "repository_id": "kernel",
        },
        {
            "availability": "not_supplied",
            "kind": "availability_record",
            "pin_id": "E",
            "reason_code": "owner_handoff_absent",
            "repository_id": "kernel",
        },
    ]
    verify_input_manifest(
        manifest, {"kernel": root, "authority": _authority_repository()}
    )
    verify_ledger(
        source / "f-unresolved-authority-ledger.json",
        source / "f-source-span-map.json",
        source / "f-authority-change-request-index.json",
        manifest,
    )


def test_packaged_f0_records_validate_against_draft_2020_12_schemas() -> None:
    root = Path(__file__).parents[2]
    source = (
        root
        / "packages/autocut-kernel/src/autocut_kernel/contracts/source/2_1_3/commands/f-authority-intake"
    )
    pairs = (
        ("f-authority-input-manifest.schema.json", "f-authority-input.manifest.json"),
        ("f-unresolved-authority-ledger.schema.json", "f-unresolved-authority-ledger.json"),
        ("f-source-span-map.schema.json", "f-source-span-map.json"),
        ("f-authority-change-request-index.schema.json", "f-authority-change-request-index.json"),
    )
    for schema_name, record_name in pairs:
        schema = json.loads((source / schema_name).read_text())
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.validate(json.loads((source / record_name).read_text()), schema)
