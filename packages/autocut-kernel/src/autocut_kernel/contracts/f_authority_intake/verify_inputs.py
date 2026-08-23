"""Verify F0 input pins exclusively against immutable Git blobs."""
# pyright: reportUnknownVariableType=none, reportUnknownArgumentType=none, reportUnknownMemberType=none, reportUnnecessaryComparison=none

from __future__ import annotations

import argparse
import hashlib
import subprocess
from pathlib import Path
from typing import Any

from autocut_kernel.contracts.compiler.canonical import load_canonical_json_bytes

from .model import (
    B_SOURCE_PATHS,
    CONTENT_PIN_COUNTS,
    ERRATA_ATTESTATION,
    ERRATA_IMPORT,
    F0_PROFILE_ID,
    KERNEL_PIN_OIDS,
    PIN_IDS,
    IntakeError,
    reject_forbidden,
    require_hash,
    require_oid,
    require_path,
)


def _git(repository: Path, *args: str, raw: bool = False) -> str | bytes:
    result = subprocess.run(
        ("git", "-C", str(repository), *args),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        raise IntakeError(
            result.stderr.decode("utf-8", "replace").strip() or "Git object read failed"
        )
    return result.stdout if raw else result.stdout.decode().strip()


def _raw_hash(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def load_jcs(path: Path) -> dict[str, Any]:
    value, canonical = load_canonical_json_bytes(path.read_bytes(), origin=str(path))
    if not isinstance(value, dict) or path.read_bytes().rstrip(b"\n") != canonical:
        raise IntakeError(f"{path}: must be a duplicate-free JCS JSON object")
    reject_forbidden(value)
    return value


def _verify_span(raw: bytes, span: dict[str, Any], path: str) -> None:
    if set(span) != {"path", "raw_sha256", "start_line", "end_line", "anchor"}:
        raise IntakeError("source span is not closed")
    require_path(span["path"])
    require_hash(span["raw_sha256"])
    if span["path"] != path or span["raw_sha256"] != _raw_hash(raw):
        raise IntakeError("source span does not pin the content blob")
    if (
        not isinstance(span["anchor"], str)
        or not span["anchor"]
        or not isinstance(span["start_line"], int)
        or not isinstance(span["end_line"], int)
        or not 0 < span["start_line"] <= span["end_line"] <= raw.count(b"\n") + 1
    ):
        raise IntakeError("invalid source span")
    lines = raw.decode("utf-8").splitlines()
    if span["anchor"] not in lines[span["start_line"] - 1]:
        raise IntakeError("source span anchor is not present at its declared line")


def _verify_content(pin: dict[str, Any], repositories: dict[str, Path]) -> None:
    required = {
        "pin_id",
        "kind",
        "repository_id",
        "producer_git_commit",
        "attestation_git_commit",
        "source_blobs",
        "topology",
    }
    if set(pin) != required or pin["kind"] not in {
        "handoff",
        "authority_document",
        "erratum_document",
    }:
        raise IntakeError("content pin is not a closed allowed branch")
    producer, attestation = (
        require_oid(pin["producer_git_commit"]),
        require_oid(pin["attestation_git_commit"]),
    )
    pin_id = pin["pin_id"]
    if pin_id in KERNEL_PIN_OIDS:
        if pin["repository_id"] != "kernel" or (producer, attestation) != KERNEL_PIN_OIDS[pin_id]:
            raise IntakeError("fixed kernel input identity mismatch")
    elif pin_id == "authority":
        if (
            pin["repository_id"] != "authority"
            or producer != ERRATA_ATTESTATION
            or attestation != producer
        ):
            raise IntakeError("authority must use the designated I2 snapshot")
    elif pin_id.startswith("errata."):
        if pin["repository_id"] != "authority" or (producer, attestation) != (
            ERRATA_IMPORT,
            ERRATA_ATTESTATION,
        ):
            raise IntakeError("errata must use the designated immutable I1/I2 import")
    if not isinstance(pin["source_blobs"], list) or not pin["source_blobs"]:
        raise IntakeError("content pin needs byte length and spans")
    if (
        not isinstance(pin["repository_id"], str)
        or pin["repository_id"] not in repositories
        or set(pin["topology"]) != {"producer_parent", "attestation_parent", "no_merge"}
        or pin["topology"]["no_merge"] is not True
    ):
        raise IntakeError("unknown repository_id or invalid topology")
    repository = repositories[pin["repository_id"]]
    require_oid(pin["topology"]["producer_parent"])
    parents = _git(repository, "rev-list", "--parents", "-n", "1", producer).split()[1:]
    if parents != [pin["topology"]["producer_parent"]]:
        raise IntakeError("producer parent mismatch or merge")
    if pin["kind"] == "handoff" or pin_id.startswith("errata."):
        if _git(repository, "rev-list", "--parents", "-n", "1", attestation).split()[1:] != [
            producer
        ]:
            raise IntakeError("attestation must be a sole direct child of producer")
        if pin["topology"]["attestation_parent"] != producer:
            raise IntakeError("recorded attestation parent mismatch")
    elif (
        attestation != producer
        or pin["topology"]["attestation_parent"] != pin["topology"]["producer_parent"]
    ):
        raise IntakeError("authority documents must pin one immutable non-merge source commit")
    paths: set[str] = set()
    for blob in pin["source_blobs"]:
        if set(blob) != {"path", "raw_sha256", "byte_length", "source_spans"}:
            raise IntakeError("source blob is not closed")
        require_path(blob["path"])
        require_hash(blob["raw_sha256"])
        if (
            blob["path"] in paths
            or not isinstance(blob["byte_length"], int)
            or blob["byte_length"] < 1
            or not isinstance(blob["source_spans"], list)
            or not blob["source_spans"]
        ):
            raise IntakeError("source blob path, length, or spans are invalid")
        paths.add(blob["path"])
        # A/B handoff records are attestation-child bytes; C owner contribution
        # manifests are deliberately producer bytes, with the child proving their
        # independent review topology.
        blob_commit = (
            producer
            if pin["pin_id"] in {"B", "C1", "C2", "C3", "C4", "C5"}
            else attestation
            if pin["kind"] == "handoff"
            else producer
        )
        if pin_id.startswith("errata."):
            blob_commit = producer
        raw = _git(repository, "show", f"{blob_commit}:{blob['path']}", raw=True)
        assert isinstance(raw, bytes)
        if _raw_hash(raw) != blob["raw_sha256"] or len(raw) != blob["byte_length"]:
            raise IntakeError("Git blob raw hash or byte length mismatch")
        for span in blob["source_spans"]:
            _verify_span(raw, span, blob["path"])
    if len(paths) != CONTENT_PIN_COUNTS[pin_id]:
        raise IntakeError("content pin does not contain its complete fixed source census")
    if pin_id == "B" and paths != B_SOURCE_PATHS:
        raise IntakeError(
            "B source census must contain each selected source and its unresolved complement"
        )


def _verify_availability(pin: dict[str, Any], repositories: dict[str, Path]) -> None:
    if set(pin) != {"pin_id", "kind", "repository_id", "availability", "reason_code"}:
        raise IntakeError("availability record is not closed")
    if (
        pin["pin_id"] not in {"D", "E"}
        or pin["kind"] != "availability_record"
        or pin["repository_id"] != "kernel"
        or pin["repository_id"] not in repositories
        or pin["availability"] != "not_supplied"
        or pin["reason_code"] != "owner_handoff_absent"
    ):
        raise IntakeError("availability is permitted only for the fixed D/E owner absences")


def verify_input_manifest(manifest_path: Path, repositories: dict[str, Path]) -> dict[str, Any]:
    manifest = load_jcs(manifest_path)
    if (
        set(manifest) != {"format", "contract_version", "profiles", "inputs"}
        or manifest["format"] != "autocut.f-authority-input/v1"
        or manifest["contract_version"] != "2.1.3"
    ):
        raise IntakeError("input manifest header is invalid")
    profiles, inputs = manifest["profiles"], manifest["inputs"]
    if (
        not isinstance(profiles, list)
        or len(profiles) != 1
        or not isinstance(profiles[0], dict)
        or set(profiles[0]) != {"profile_id", "required_pin_ids"}
        or profiles[0]["profile_id"] != F0_PROFILE_ID
        or profiles[0]["required_pin_ids"] != sorted(PIN_IDS)
    ):
        raise IntakeError("manifest must select the single fixed complete F0 profile")
    if (
        not isinstance(inputs, list)
        or {item.get("pin_id") for item in inputs if isinstance(item, dict)} != PIN_IDS
        or len(inputs) != len(PIN_IDS)
    ):
        raise IntakeError("input pin set must be exact and duplicate-free")
    for pin in inputs:
        if not isinstance(pin, dict) or pin.get("pin_id") not in PIN_IDS:
            raise IntakeError("invalid input pin")
        if pin.get("kind") == "availability_record":
            _verify_availability(pin, repositories)
        else:
            _verify_content(pin, repositories)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--repository-root", action="append", default=[], metavar="ID=PATH")
    args = parser.parse_args()
    try:
        roots: dict[str, Path] = {}
        for item in args.repository_root:
            repository_id, separator, raw_path = item.partition("=")
            if not separator or not repository_id or not raw_path or repository_id in roots:
                raise IntakeError("repository roots must be unique repository_id=path entries")
            roots[repository_id] = Path(raw_path)
        verify_input_manifest(args.manifest, roots)
    except (IntakeError, OSError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
