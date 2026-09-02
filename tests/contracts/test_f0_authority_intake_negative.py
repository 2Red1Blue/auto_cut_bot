from __future__ import annotations

import json
import os
from pathlib import Path

import jsonschema
import pytest
from autocut_kernel.contracts.f_authority_intake.model import IntakeError, require_hash, require_oid
from autocut_kernel.contracts.f_authority_intake.verify_inputs import verify_input_manifest
from autocut_kernel.contracts.f_authority_intake.verify_ledger import verify_ledger

ROOT = Path(__file__).parents[2]
SOURCE = (
    ROOT
    / "packages/autocut-kernel/src/autocut_kernel/contracts/source/2_1_3/commands/f-authority-intake"
)


def _repositories() -> dict[str, Path]:
    value = os.environ.get("AUTOCUT_AUTHORITY_REPOSITORY")
    if not value or not (Path(value) / ".git").exists():
        pytest.skip(
            "set AUTOCUT_AUTHORITY_REPOSITORY to run authority intake tests"
        )
    return {"kernel": ROOT, "authority": Path(value)}


def _write(tmp_path: Path, name: str, payload: object) -> Path:
    path = tmp_path / name
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    return path


def _manifest() -> dict[str, object]:
    return json.loads((SOURCE / "f-authority-input.manifest.json").read_text())


@pytest.mark.parametrize("value", ["50f78ea0", "TBD", "0" * 40, "cmd_fake"])
def test_rejects_non_full_oid(value: str) -> None:
    with pytest.raises(IntakeError):
        require_oid(value)


@pytest.mark.parametrize("value", ["sha256:" + "0" * 64, "sha256:deadbeef", "sha256:" + "A" * 64])
def test_rejects_non_exact_hash(value: str) -> None:
    with pytest.raises(IntakeError):
        require_hash(value)


def test_rejects_missing_or_arbitrary_profile(tmp_path: Path) -> None:
    payload = _manifest()
    payload["profiles"] = []
    with pytest.raises(IntakeError):
        verify_input_manifest(
            _write(tmp_path, "manifest.json", payload),
            _repositories(),
        )


def test_accepts_only_d_and_e_owner_absence_records(tmp_path: Path) -> None:
    verify_input_manifest(
        _write(tmp_path, "accepted.json", _manifest()),
        _repositories(),
    )
    payload = _manifest()
    errata = next(pin for pin in payload["inputs"] if pin["pin_id"] == "errata.execution")
    errata.clear()
    errata.update(
        {
            "pin_id": "errata.execution",
            "kind": "availability_record",
            "repository_id": "kernel",
            "availability": "not_supplied",
            "reason_code": "owner_handoff_absent",
        }
    )
    with pytest.raises(IntakeError):
        verify_input_manifest(
            _write(tmp_path, "errata-availability.json", payload),
            _repositories(),
        )


def test_rejects_fixed_identity_and_errata_census_tampering(tmp_path: Path) -> None:
    payload = _manifest()
    authority = next(pin for pin in payload["inputs"] if pin["pin_id"] == "authority")
    authority["producer_git_commit"] = "a" * 40
    with pytest.raises(IntakeError):
        verify_input_manifest(
            _write(tmp_path, "identity.json", payload),
            _repositories(),
        )
    payload = _manifest()
    errata = next(pin for pin in payload["inputs"] if pin["pin_id"] == "errata.execution")
    errata["source_blobs"].pop()
    with pytest.raises(IntakeError):
        verify_input_manifest(
            _write(tmp_path, "errata.json", payload),
            _repositories(),
        )


@pytest.mark.parametrize("operation", ["remove", "add", "swap"])
def test_rejects_incomplete_or_substituted_b_source_census(tmp_path: Path, operation: str) -> None:
    payload = _manifest()
    b_pin = next(pin for pin in payload["inputs"] if pin["pin_id"] == "B")
    blobs = b_pin["source_blobs"]
    if operation == "remove":
        blobs.pop()
    elif operation == "add":
        blobs.append(dict(blobs[0]))
    else:
        blobs[0]["path"] = blobs[1]["path"]
    with pytest.raises(IntakeError):
        verify_input_manifest(
            _write(tmp_path, f"b-{operation}.json", payload),
            _repositories(),
        )


def test_rejects_unbound_span_and_broken_ac_relation(tmp_path: Path) -> None:
    spans = json.loads((SOURCE / "f-source-span-map.json").read_text())
    spans["mappings"][0]["source_span"]["end_line"] = 1
    with pytest.raises(IntakeError):
        verify_ledger(
            SOURCE / "f-unresolved-authority-ledger.json",
            _write(tmp_path, "spans.json", spans),
            SOURCE / "f-authority-change-request-index.json",
            SOURCE / "f-authority-input.manifest.json",
        )


def test_rejects_schema_extra_record_and_nonrequired_ac_state() -> None:
    manifest = _manifest()
    manifest["inputs"][0]["invented"] = True
    schema = json.loads((SOURCE / "f-authority-input-manifest.schema.json").read_text())
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(manifest, schema)
    ac = json.loads((SOURCE / "f-authority-change-request-index.json").read_text())
    ac["requests"][0]["request_state"] = "accepted"
    schema = json.loads((SOURCE / "f-authority-change-request-index.schema.json").read_text())
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(ac, schema)


def test_rejects_mapping_pin_not_named_by_slot_and_nonreverse_ac_census(tmp_path: Path) -> None:
    ledger = json.loads((SOURCE / "f-unresolved-authority-ledger.json").read_text())
    next(slot for slot in ledger["slots"] if slot["slot_id"] == "catalogue_envelope")[
        "input_pin_ids"
    ] = []
    with pytest.raises(IntakeError):
        verify_ledger(
            _write(tmp_path, "missing-pin-ledger.json", ledger),
            SOURCE / "f-source-span-map.json",
            SOURCE / "f-authority-change-request-index.json",
            SOURCE / "f-authority-input.manifest.json",
        )
    ac = json.loads((SOURCE / "f-authority-change-request-index.json").read_text())
    ac["requests"][0]["slot_ids"].append("components")
    with pytest.raises(IntakeError):
        verify_ledger(
            SOURCE / "f-unresolved-authority-ledger.json",
            SOURCE / "f-source-span-map.json",
            _write(tmp_path, "nonreverse-ac.json", ac),
            SOURCE / "f-authority-input.manifest.json",
        )
    ledger = json.loads((SOURCE / "f-unresolved-authority-ledger.json").read_text())
    next(slot for slot in ledger["slots"] if slot["slot_id"] == "receipt_dispatcher")[
        "authority_change_request_id"
    ] = "AC-B-004"
    with pytest.raises(IntakeError):
        verify_ledger(
            _write(tmp_path, "ledger.json", ledger),
            SOURCE / "f-source-span-map.json",
            SOURCE / "f-authority-change-request-index.json",
            SOURCE / "f-authority-input.manifest.json",
        )
