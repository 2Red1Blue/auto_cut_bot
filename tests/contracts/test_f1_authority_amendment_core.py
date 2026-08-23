from __future__ import annotations

import json
import os
from pathlib import Path

import jsonschema
import pytest
from autocut_kernel.contracts.f_authority_amendment.model import PACKET_IDS, AmendmentError
from autocut_kernel.contracts.f_authority_amendment.verify_packet import (
    _load_historic_immutable_jcs_bytes,
    _load_jcs_bytes,
    verify_packet,
)

ROOT = Path(__file__).parents[2]
SOURCE = (
    ROOT
    / "packages/autocut-kernel/src/autocut_kernel/contracts/source/2_1_3/commands/f-authority-amendment"
)
F0_SPANS = (
    ROOT
    / "packages/autocut-kernel/src/autocut_kernel/contracts/source/2_1_3/commands/f-authority-intake/f-source-span-map.json"
)
try:
    AUTHORITY = Path(os.environ["AUTOCUT_AUTHORITY_REPOSITORY"])
except KeyError as error:
    raise RuntimeError(
        "AUTOCUT_AUTHORITY_REPOSITORY must name the immutable authority Git repository"
    ) from error


def _packet() -> dict[str, object]:
    return json.loads((SOURCE / "f1-authority-amendment-packet.json").read_text())


def _write(tmp_path: Path, name: str, payload: object) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return path


def test_packaged_f1_packet_is_closed_jcs_and_proposal_only() -> None:
    packet_path = SOURCE / "f1-authority-amendment-packet.json"
    schema = json.loads((SOURCE / "f1-authority-amendment-packet.schema.json").read_text())
    payload = _packet()
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(payload, schema)
    verify_packet(packet_path, F0_SPANS, ROOT, AUTHORITY)
    assert {packet["packet_id"] for packet in payload["packets"]} == PACKET_IDS
    assert {packet["disposition"] for packet in payload["packets"]} == {"proposed", "deferred"}
    assert all(len(packet) == 12 for packet in payload["packets"])


def test_rejects_invalid_fields_and_false_acceptance(tmp_path: Path) -> None:
    payload = _packet()
    payload["registry_rows"] = "invented"
    with pytest.raises(AmendmentError):
        verify_packet(_write(tmp_path, "invalid-field.json", payload), F0_SPANS, ROOT, AUTHORITY)
    payload = _packet()
    payload["packets"][0]["disposition"] = "accepted"
    with pytest.raises(AmendmentError):
        verify_packet(_write(tmp_path, "false-acceptance.json", payload), F0_SPANS, ROOT, AUTHORITY)


def test_rejects_packet_census_and_f0_errata_topology_drift(tmp_path: Path) -> None:
    payload = _packet()
    payload["packets"].pop()
    with pytest.raises(AmendmentError):
        verify_packet(_write(tmp_path, "missing-packet.json", payload), F0_SPANS, ROOT, AUTHORITY)
    payload = _packet()
    payload["accepted_f0_chain"]["topology"]["attestation_parent"] = "0" * 40
    with pytest.raises(AmendmentError):
        verify_packet(_write(tmp_path, "f0-topology.json", payload), F0_SPANS, ROOT, AUTHORITY)
    payload = _packet()
    payload["errata_import"]["I2"]["parent_git_commit"] = "0" * 40
    with pytest.raises(AmendmentError):
        verify_packet(_write(tmp_path, "errata-topology.json", payload), F0_SPANS, ROOT, AUTHORITY)


def test_rejects_unpinned_span_and_premature_owner_choice(tmp_path: Path) -> None:
    payload = _packet()
    payload["packets"][0]["controlling_spans"][0]["source_span"]["end_line"] = 1
    with pytest.raises(AmendmentError):
        verify_packet(_write(tmp_path, "span-drift.json", payload), F0_SPANS, ROOT, AUTHORITY)
    payload = _packet()
    packet = next(item for item in payload["packets"] if item["packet_id"] == "AC-B-008")
    packet["affected_handoffs"] = ["immutable_C2_handoff", "immutable_D_handoff", "owner_C2"]
    with pytest.raises(AmendmentError):
        verify_packet(_write(tmp_path, "owner-choice.json", payload), F0_SPANS, ROOT, AUTHORITY)


@pytest.mark.parametrize(
    ("field", "claim"),
    [
        ("normative_delta", "Registry rows are supplied"),
        ("data_shape_closure", "readiness claim is true"),
        ("normative_delta", "selected owner is C2"),
        ("data_shape_closure", "material owner is D"),
        ("normative_delta", "default selection applies"),
        ("data_shape_closure", "business ID is assigned"),
    ],
)
def test_rejects_material_claims_but_allows_boundary_discussion(
    tmp_path: Path, field: str, claim: str
) -> None:
    payload = _packet()
    payload["packets"][0][field] = claim
    with pytest.raises(AmendmentError):
        verify_packet(_write(tmp_path, f"forbidden-{field}.json", payload), F0_SPANS, ROOT, AUTHORITY)
    payload = _packet()
    payload["packets"][0]["normative_delta"] = "Registry/runtime boundary and handoff remain proposal-only"
    verify_packet(_write(tmp_path, "allowed-boundary.json", payload), F0_SPANS, ROOT, AUTHORITY)


def test_rejects_trailing_newline_and_substituted_f0_map(tmp_path: Path) -> None:
    raw = (SOURCE / "f1-authority-amendment-packet.json").read_bytes()
    newline_packet = tmp_path / "trailing-newline.json"
    newline_packet.write_bytes(raw + b"\n")
    with pytest.raises(AmendmentError):
        verify_packet(newline_packet, F0_SPANS, ROOT, AUTHORITY)
    altered_spans = json.loads(F0_SPANS.read_text())
    altered_spans["mappings"][0]["source_span"]["end_line"] = 1
    with pytest.raises(AmendmentError):
        verify_packet(
            SOURCE / "f1-authority-amendment-packet.json",
            _write(tmp_path, "f0-map.json", altered_spans),
            ROOT,
            AUTHORITY,
        )


def test_rejects_trailing_newline_in_immutable_object_decoder() -> None:
    with pytest.raises(AmendmentError):
        _load_jcs_bytes(b'{"immutable":true}\n', origin="immutable-git-object")


def test_historic_immutable_decoder_permits_only_one_final_lf() -> None:
    assert _load_historic_immutable_jcs_bytes(
        b'{"immutable":true}\n', origin="historic-git-object"
    ) == {"immutable": True}
    for raw in (b'{"immutable":true}\n\n', b'{"immutable":true}\r\n', b'{"immutable": true}\n'):
        with pytest.raises(AmendmentError):
            _load_historic_immutable_jcs_bytes(raw, origin="historic-git-object")


def test_requires_separate_immutable_authority_repository() -> None:
    with pytest.raises(AmendmentError):
        verify_packet(SOURCE / "f1-authority-amendment-packet.json", F0_SPANS, ROOT, ROOT)


def test_ac_b_008_has_only_closed_deferred_prerequisite_sentinels(tmp_path: Path) -> None:
    payload = _packet()
    packet = next(item for item in payload["packets"] if item["packet_id"] == "AC-B-008")
    packet["normative_delta"] = "SourceUsageLedger sole owner is Stage-04 in v2.1.4"
    with pytest.raises(AmendmentError):
        verify_packet(_write(tmp_path, "b008-owner-bypass.json", payload), F0_SPANS, ROOT, AUTHORITY)
    payload = _packet()
    packet = next(item for item in payload["packets"] if item["packet_id"] == "AC-B-008")
    packet["version_and_migration_effect"] = {
        "decision_state": "required",
        "precondition": "v2.1.4",
    }
    with pytest.raises(AmendmentError):
        verify_packet(_write(tmp_path, "b008-successor-bypass.json", payload), F0_SPANS, ROOT, AUTHORITY)
