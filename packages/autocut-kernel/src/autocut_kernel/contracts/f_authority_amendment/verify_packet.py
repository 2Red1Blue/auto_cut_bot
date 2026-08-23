"""Fail-closed verifier for F1 proposal packets."""
# pyright: reportUnknownVariableType=none, reportUnknownArgumentType=none, reportUnknownMemberType=none, reportUnnecessaryComparison=none

from __future__ import annotations

import argparse
import hashlib
import subprocess
from pathlib import Path
from typing import Any

from autocut_kernel.contracts.compiler.canonical import load_canonical_json_bytes

from .model import (
    CONTROLLED_SLOTS,
    ERRATA_I1,
    ERRATA_I2,
    F0_ATTESTATION,
    F0_PRODUCER,
    PACKET_IDS,
    AmendmentError,
    reject_f1_forbidden,
    require_nonempty_strings,
)

F0_DIRECTORY = "packages/autocut-kernel/src/autocut_kernel/contracts/source/2_1_3/commands/f-authority-intake"
F0_HANDOFF = f"{F0_DIRECTORY}/f0-handoff.json"
F0_INPUT_MANIFEST = f"{F0_DIRECTORY}/f-authority-input.manifest.json"
F0_SPAN_MAP = f"{F0_DIRECTORY}/f-source-span-map.json"
AUTHORITY_ERRATA_ROOT = "原理/authority-inputs/trellis-errata/v1"
AUTHORITY_IMPORT_MANIFEST = f"{AUTHORITY_ERRATA_ROOT}/import-manifest.json"
PREFLIGHT_FORMAT = "autocut.f-authority-amendment-preflight/v1"
F0_HANDOFF_BLOB = "e26e930b655a51efd8e8c1e7771241d2c507144c"
F0_INPUT_MANIFEST_BLOB = "87e708b8d4f803034e0c8553f967d645b46cfcb2"
ERRATA_IMPORT_MANIFEST_BLOB = "672fe533b190b56678dbc023cbb7d4d2925d54db"


def _raw_hash(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _git(repository: Path, *args: str) -> bytes:
    result = subprocess.run(
        ("git", "-C", str(repository), *args),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        raise AmendmentError(result.stderr.decode("utf-8", "replace").strip() or "Git object read failed")
    return result.stdout


def _git_object(repository: Path, commit: str, path: str) -> bytes:
    return _git(repository, "show", f"{commit}:{path}")


def _require_blob(repository: Path, commit: str, path: str, expected: str) -> bytes:
    actual = _git(repository, "rev-parse", f"{commit}:{path}").decode().strip()
    if actual != expected:
        raise AmendmentError("immutable Git object identity does not match the accepted evidence")
    return _git_object(repository, commit, path)


def _repository_root(packet_path: Path) -> Path:
    for ancestor in (packet_path.parent, *packet_path.parents):
        if (ancestor / ".git").exists():
            return ancestor
    raise AmendmentError("F1 packet must be verified from a Git worktree")


def _accepted_f0_spans(path: Path, repository: Path) -> dict[str, dict[str, Any]]:
    expected_raw = _git_object(repository, F0_PRODUCER, F0_SPAN_MAP)
    if path.read_bytes() != expected_raw:
        raise AmendmentError("controlling span map is not the immutable F0 producer object")
    payload = _load_jcs(path)
    if set(payload) != {"format", "contract_version", "mappings"} or payload["format"] != "autocut.f-source-span-map/v1":
        raise AmendmentError("accepted F0 source-span map is invalid")
    mappings = payload["mappings"]
    if not isinstance(mappings, list):
        raise AmendmentError("accepted F0 source-span map has no mappings")
    result: dict[str, dict[str, Any]] = {}
    for mapping in mappings:
        if not isinstance(mapping, dict) or set(mapping) != {"slot_id", "pin_id", "source_span", "use"}:
            raise AmendmentError("accepted F0 source-span map is not closed")
        slot_id = mapping["slot_id"]
        if not isinstance(slot_id, str) or slot_id in result:
            raise AmendmentError("accepted F0 source-span slots are invalid")
        result[slot_id] = mapping
    return result


def _load_jcs(path: Path) -> dict[str, Any]:
    """Load a duplicate-free JCS object without inheriting F0's vocabulary."""

    try:
        raw = path.read_bytes()
    except OSError as error:
        raise AmendmentError(str(error)) from error
    return _load_jcs_bytes(raw, origin=str(path))


def _load_jcs_bytes(raw: bytes, *, origin: str) -> dict[str, Any]:
    try:
        value, canonical = load_canonical_json_bytes(raw, origin=origin)
    except ValueError as error:
        raise AmendmentError(str(error)) from error
    if not isinstance(value, dict) or raw != canonical:
        raise AmendmentError(f"{origin}: must be a duplicate-free JCS JSON object")
    return value


def _load_historic_immutable_jcs_bytes(raw: bytes, *, origin: str) -> dict[str, Any]:
    """Read an accepted legacy Git object with its sole permitted LF framing."""

    if raw.endswith(b"\r\n") or raw.endswith(b"\n\n"):
        raise AmendmentError(f"{origin}: historic immutable evidence has invalid line framing")
    canonical_raw = raw[:-1] if raw.endswith(b"\n") else raw
    try:
        value, canonical = load_canonical_json_bytes(canonical_raw, origin=origin)
    except ValueError as error:
        raise AmendmentError(str(error)) from error
    if not isinstance(value, dict) or canonical_raw != canonical:
        raise AmendmentError(f"{origin}: historic immutable evidence must be canonical JCS")
    return value


def _verify_authority_import(authority_repository: Path) -> None:
    if _git(authority_repository, "rev-parse", "--is-inside-work-tree").decode().strip() != "true":
        raise AmendmentError("authority repository is not a Git worktree")
    i2_parents = _git(authority_repository, "rev-list", "--parents", "-n", "1", ERRATA_I2).decode().split()[1:]
    if i2_parents != [ERRATA_I1]:
        raise AmendmentError("I2 must be a direct non-merge child of I1")
    if len(_git(authority_repository, "rev-list", "--parents", "-n", "1", ERRATA_I1).decode().split()[1:]) != 1:
        raise AmendmentError("I1 must be a non-merge import commit")
    raw_manifest = _require_blob(
        authority_repository, ERRATA_I2, AUTHORITY_IMPORT_MANIFEST, ERRATA_IMPORT_MANIFEST_BLOB
    )
    manifest = _load_historic_immutable_jcs_bytes(
        raw_manifest, origin=f"{ERRATA_I2}:{AUTHORITY_IMPORT_MANIFEST}"
    )
    if manifest.get("format") != "trellis-errata-import-manifest/v1" or manifest.get("import_commit") != ERRATA_I1:
        raise AmendmentError("I2 import manifest does not bind I1")
    imported = manifest.get("imported_files")
    if not isinstance(imported, list) or len(imported) != 15:
        raise AmendmentError("I2 import inventory is incomplete")
    seen_paths: set[str] = set()
    for item in imported:
        if not isinstance(item, dict) or set(item) != {"byte_length", "git_blob_oid", "path", "sha256"}:
            raise AmendmentError("I2 import inventory is not closed")
        path = item["path"]
        if not isinstance(path, str) or path in seen_paths:
            raise AmendmentError("I2 import inventory paths are invalid")
        seen_paths.add(path)
        imported_path = f"{AUTHORITY_ERRATA_ROOT}/{path}"
        raw = _git_object(authority_repository, ERRATA_I1, imported_path)
        oid = _git(authority_repository, "rev-parse", f"{ERRATA_I1}:{imported_path}").decode().strip()
        if item.get("byte_length") != len(raw) or item.get("sha256") != _raw_hash(raw) or item.get("git_blob_oid") != oid:
            raise AmendmentError("I2 import inventory does not match immutable I1 blobs")


def _verify_immutable_f0(repository: Path, authority_repository: Path) -> None:
    parents = _git(repository, "rev-list", "--parents", "-n", "1", F0_ATTESTATION).decode().split()[1:]
    if parents != [F0_PRODUCER]:
        raise AmendmentError("accepted F0 attestation must be a direct non-merge child")
    if len(_git(repository, "rev-list", "--parents", "-n", "1", F0_PRODUCER).decode().split()[1:]) != 1:
        raise AmendmentError("accepted F0 producer must be non-merge")
    handoff_raw = _require_blob(repository, F0_ATTESTATION, F0_HANDOFF, F0_HANDOFF_BLOB)
    handoff = _load_historic_immutable_jcs_bytes(
        handoff_raw, origin=f"{F0_ATTESTATION}:{F0_HANDOFF}"
    )
    manifest_raw = _require_blob(repository, F0_PRODUCER, F0_INPUT_MANIFEST, F0_INPUT_MANIFEST_BLOB)
    span_raw = _git_object(repository, F0_PRODUCER, F0_SPAN_MAP)
    if (
        handoff.get("producer_git_commit") != F0_PRODUCER
        or handoff.get("input_manifest_raw_sha256") != _raw_hash(manifest_raw)
        or handoff.get("source_span_map_raw_sha256") != _raw_hash(span_raw)
        or handoff.get("external_authority_imports")
        != {"I1": {"producer_git_commit": ERRATA_I1}, "I2": {"attestation_git_commit": ERRATA_I2}}
    ):
        raise AmendmentError("accepted F0 handoff does not bind its producer evidence or I1/I2 imports")
    manifest = _load_historic_immutable_jcs_bytes(
        manifest_raw, origin=f"{F0_PRODUCER}:{F0_INPUT_MANIFEST}"
    )
    inputs = manifest.get("inputs")
    if not isinstance(inputs, list):
        raise AmendmentError("accepted F0 input manifest has no inputs")
    errata = {item.get("pin_id"): item for item in inputs if isinstance(item, dict)}
    for pin_id in ("errata.execution", "errata.recovery"):
        pin = errata.get(pin_id)
        if not isinstance(pin, dict) or (pin.get("producer_git_commit"), pin.get("attestation_git_commit")) != (ERRATA_I1, ERRATA_I2):
            raise AmendmentError("accepted F0 input manifest does not pin the immutable I1/I2 errata evidence")
    _verify_authority_import(authority_repository)


def _verify_f0_chain(chain: object, imports: object) -> None:
    if chain != {
        "attestation_git_commit": F0_ATTESTATION,
        "producer_git_commit": F0_PRODUCER,
        "topology": {"attestation_parent": F0_PRODUCER, "no_merge": True},
    }:
        raise AmendmentError("F1 must pin the accepted non-merge F0 producer-to-attestation chain")
    if imports != {
        "I1": {"producer_git_commit": ERRATA_I1},
        "I2": {"attestation_git_commit": ERRATA_I2, "parent_git_commit": ERRATA_I1},
    }:
        raise AmendmentError("F1 must pin the accepted I1-to-I2 errata import chain")


def _verify_packet(packet: object, accepted_spans: dict[str, dict[str, Any]]) -> str:
    fields = {
        "format",
        "contract_version",
        "packet_id",
        "decision_area",
        "source_preconditions",
        "disposition",
        "controlling_spans",
        "normative_delta",
        "data_shape_closure",
        "rejection_vectors",
        "affected_handoffs",
        "version_and_migration_effect",
    }
    if not isinstance(packet, dict) or set(packet) != fields:
        raise AmendmentError("packet is not a closed F1 proposal record")
    packet_id = packet["packet_id"]
    if not isinstance(packet_id, str) or packet_id not in PACKET_IDS:
        raise AmendmentError("packet ID is outside the accepted F1 census")
    if packet["format"] != PREFLIGHT_FORMAT or packet["contract_version"] != "2.1.3":
        raise AmendmentError("packet must repeat the F1 format and contract version")
    if packet["disposition"] not in {"proposed", "deferred"}:
        raise AmendmentError("only proposed or deferred dispositions are allowed before attestation")
    for field in ("decision_area", "normative_delta", "data_shape_closure"):
        if not isinstance(packet[field], str) or not packet[field]:
            raise AmendmentError(f"{field} must be a non-empty proposal description")
    if require_nonempty_strings(packet["source_preconditions"], field="source_preconditions") != [
        "accepted_f0_chain",
        "accepted_errata_import",
    ]:
        raise AmendmentError("packet source preconditions must name the accepted F0 and errata chains")
    spans = packet["controlling_spans"]
    if not isinstance(spans, list) or not spans:
        raise AmendmentError("packet must name exact controlling spans")
    seen_slots: set[str] = set()
    for span in spans:
        if not isinstance(span, dict) or set(span) != {"slot_id", "pin_id", "source_span", "use"}:
            raise AmendmentError("controlling span is not a closed F0 mapping")
        slot_id = span["slot_id"]
        if not isinstance(slot_id, str) or slot_id in seen_slots or accepted_spans.get(slot_id) != span:
            raise AmendmentError("controlling span is not exactly pinned by accepted F0")
        seen_slots.add(slot_id)
    if seen_slots != CONTROLLED_SLOTS[packet_id]:
        raise AmendmentError("packet controlling-span census does not match its proposal")
    require_nonempty_strings(packet["affected_handoffs"], field="affected_handoffs")
    require_nonempty_strings(packet["rejection_vectors"], field="rejection_vectors")
    successor = packet["version_and_migration_effect"]
    if successor != {"decision_state": "required", "precondition": "attested_authority_amendment"}:
        raise AmendmentError("a successor version requires an attested authority amendment")
    if packet_id == "AC-B-008":
        if packet["disposition"] != "deferred":
            raise AmendmentError("AC-B-008 remains deferred pending its owner handoff")
        blockers = set(packet["affected_handoffs"])
        required = {"single_owner_option", "immutable_C2_handoff", "immutable_D_handoff"}
        if blockers != required:
            raise AmendmentError("AC-B-008 must retain the single-owner and immutable C2/D blockers")
        if (
            packet["decision_area"] != "deferred_owner_handoff_only"
            or packet["normative_delta"] != "no_normative_delta"
            or packet["data_shape_closure"] != "no_data_shape_delta"
            or packet["rejection_vectors"]
            != ["owner_selection_before_handoff", "successor_version_before_attestation"]
        ):
            raise AmendmentError("AC-B-008 deferred fields must use the closed prerequisite sentinels")
    return packet_id


def verify_preflight(
    packet_path: Path,
    f0_span_map_path: Path,
    repository_root: Path | None,
    authority_repository: Path,
) -> dict[str, Any]:
    """Verify a canonical proposal preflight against immutable F0 and errata evidence."""

    payload = _load_jcs(packet_path)
    if (
        set(payload) != {"format", "contract_version", "accepted_f0_chain", "errata_import", "packets"}
        or payload["format"] != PREFLIGHT_FORMAT
        or payload["contract_version"] != "2.1.3"
    ):
        raise AmendmentError("F1 packet envelope header is invalid")
    repository = repository_root or _repository_root(packet_path)
    _verify_immutable_f0(repository, authority_repository)
    _verify_f0_chain(payload["accepted_f0_chain"], payload["errata_import"])
    accepted_spans = _accepted_f0_spans(f0_span_map_path, repository)
    packets = payload["packets"]
    if not isinstance(packets, list) or len(packets) != len(PACKET_IDS):
        raise AmendmentError("F1 packet census must be exact and duplicate-free")
    packet_ids = {_verify_packet(packet, accepted_spans) for packet in packets}
    if packet_ids != PACKET_IDS:
        raise AmendmentError("F1 packet census must be exact and duplicate-free")
    reject_f1_forbidden(payload)
    return payload


verify_packet = verify_preflight


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("preflight", type=Path)
    parser.add_argument("f0_span_map", type=Path)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--authority-repository", type=Path, required=True)
    args = parser.parse_args()
    try:
        verify_preflight(
            args.preflight, args.f0_span_map, args.repository_root, args.authority_repository
        )
    except (AmendmentError, OSError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
