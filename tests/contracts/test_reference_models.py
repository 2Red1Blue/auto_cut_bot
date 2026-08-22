"""Typed model parity checks for the four closed v2.1.3 reference schemas."""

from __future__ import annotations

import hashlib
from importlib import import_module
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
KERNEL_SOURCE = REPOSITORY_ROOT / "packages" / "autocut-kernel" / "src"
SHA = "sha256:" + "a" * 64


@pytest.fixture(autouse=True)
def _load_kernel_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.syspath_prepend(str(KERNEL_SOURCE))


def test_closed_reference_models_round_trip() -> None:
    contracts = import_module("autocut_kernel.contracts")
    artifact = contracts.ArtifactRef.from_mapping({"artifact_id": "art_1", "content_hash": SHA})
    artifact_set = contracts.ArtifactSetRef.from_mapping({"artifact_set_id": "set_1", "set_hash": SHA})
    domain = contracts.DomainRef.from_mapping(
        {"artifact_ref": artifact.to_mapping(), "object_type": "event", "object_id": "event-1"}
    )
    blob = contracts.ImmutableBlobRef.from_mapping(
        {"object_id": "blob_1", "sha256": SHA, "storage_locator": "object-store://blob_1", "media_type": "application/octet-stream", "byte_length_decimal": "0"}
    )
    assert artifact.to_mapping()["artifact_id"] == "art_1"
    assert artifact_set.to_mapping()["artifact_set_id"] == "set_1"
    assert domain.to_mapping()["artifact_ref"] == artifact.to_mapping()
    assert blob.to_mapping()["byte_length_decimal"] == "0"


@pytest.mark.parametrize(
    ("model_name", "value"),
    [
        ("ArtifactRef", {"artifact_id": "art_1", "content_hash": "sha256:" + "A" * 64}),
        ("ArtifactSetRef", {"artifact_set_id": "set_1", "set_hash": SHA, "extra": True}),
        ("DomainRef", {"artifact_ref": "event:event-1", "object_type": "event", "object_id": "event-1"}),
        ("ImmutableBlobRef", {"object_id": "blob_1", "sha256": SHA, "storage_locator": "x", "media_type": "x", "byte_length_decimal": "01"}),
    ],
)
def test_reference_models_reject_unstructured_or_noncanonical_forms(model_name: str, value: object) -> None:
    contracts = import_module("autocut_kernel.contracts")
    model = getattr(contracts, model_name)
    with pytest.raises(contracts.ReferenceValidationError):
        model.from_mapping(value)


def test_direct_construction_cannot_bypass_reference_validation() -> None:
    contracts = import_module("autocut_kernel.contracts")
    with pytest.raises(contracts.ReferenceValidationError, match="content_hash"):
        contracts.ArtifactRef(artifact_id="art_1", content_hash="sha256:" + "A" * 64)
    with pytest.raises(contracts.ReferenceValidationError, match="UTF-8"):
        contracts.ImmutableBlobRef(
            object_id="blob_1",
            sha256=SHA,
            storage_locator="object-store://blob_1",
            media_type="application/octet-stream",
            byte_length_decimal="\ud800",
        )


def test_immutable_blob_byte_identity_is_recomputed_before_interpretation() -> None:
    contracts = import_module("autocut_kernel.contracts")
    raw = b"immutable blob bytes"
    reference = contracts.ImmutableBlobRef(
        object_id="blob_1",
        sha256="sha256:" + hashlib.sha256(raw).hexdigest(),
        storage_locator="object-store://blob_1",
        media_type="application/octet-stream",
        byte_length_decimal=str(len(raw)),
    )
    contracts.verify_immutable_blob_bytes(reference, raw)
    with pytest.raises(contracts.ReferenceValidationError, match="sha256"):
        contracts.verify_immutable_blob_bytes(reference, b"different")
