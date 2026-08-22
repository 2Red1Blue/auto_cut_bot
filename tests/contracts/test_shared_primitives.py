"""Source-level regression coverage for v2.1.3 shared reference primitives."""

from __future__ import annotations

import json
from importlib import import_module
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
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
KERNEL_SOURCE = REPOSITORY_ROOT / "packages" / "autocut-kernel" / "src"
AUTHORITY_HASH = "sha256:7260bf922f8852ea22142220227fdda9a4e03e81433592c68957dffe08b7531d"
STAGE_04_HASH = "sha256:df28393582d1cef40f5c58df58688d56169e19dd79c92482567deda24548c5ae"


def _schema(filename: str) -> dict[str, object]:
    return json.loads((COMMON_SOURCE / filename).read_text(encoding="utf-8"))


def _validator(filename: str) -> Draft202012Validator:
    schemas = [_schema(path.name) for path in sorted(COMMON_SOURCE.glob("*.schema.json"))]
    target = next(schema for schema in schemas if schema["$id"].endswith(filename))
    registry = Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema)) for schema in schemas
    )
    return Draft202012Validator(target, registry=registry)


def _assert_invalid(filename: str, value: object) -> None:
    assert list(_validator(filename).iter_errors(value)), value


def test_all_primitive_sources_are_closed_2020_12_and_provenance_bound() -> None:
    expected = {
        "artifact-ref.schema.json",
        "artifact-set-ref.schema.json",
        "domain-ref.schema.json",
        "immutable-blob-ref.schema.json",
        "source-span-ref.schema.json",
    }
    assert {path.name for path in COMMON_SOURCE.glob("*.schema.json")} == expected
    for filename in expected:
        schema = _schema(filename)
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
        assert AUTHORITY_HASH in schema["$comment"]
    assert STAGE_04_HASH in _schema("source-span-ref.schema.json")["$comment"]


def test_artifact_and_set_refs_reject_bare_forms_unknown_keys_and_bad_hashes() -> None:
    artifact = _validator("artifact-ref.schema.json")
    assert artifact.is_valid({"artifact_id": "art_01", "content_hash": "sha256:" + "a" * 64})
    _assert_invalid("artifact-ref.schema.json", "art_01")
    _assert_invalid("artifact-ref.schema.json", {"artifact_id": "art_01", "content_hash": "sha256:ABC"})
    _assert_invalid(
        "artifact-ref.schema.json",
        {"artifact_id": "art_01", "content_hash": "sha256:" + "a" * 64, "url": "https://invalid"},
    )

    artifact_set = _validator("artifact-set-ref.schema.json")
    assert artifact_set.is_valid({"artifact_set_id": "set_01", "set_hash": "sha256:" + "b" * 64})
    _assert_invalid("artifact-set-ref.schema.json", "set_01")
    _assert_invalid("artifact-set-ref.schema.json", {"artifact_set_id": "set_01", "set_hash": "sha256:" + "B" * 64})


def test_domain_ref_requires_structured_artifact_provenance() -> None:
    value = {
        "artifact_ref": {"artifact_id": "art_events", "content_hash": "sha256:" + "c" * 64},
        "object_type": "event",
        "object_id": "event-001",
    }
    assert _validator("domain-ref.schema.json").is_valid(value)
    _assert_invalid("domain-ref.schema.json", {**value, "artifact_ref": "event:event-001"})
    _assert_invalid("domain-ref.schema.json", {**value, "artifact_ref": {"artifact_id": "art_events"}})


def test_source_span_requires_source_hash_and_structured_owner_binding() -> None:
    value = {
        "artifact_ref": {"artifact_id": "art_source_manifest", "content_hash": "sha256:" + "d" * 64},
        "source_id": "source-001",
        "source_sha256": "sha256:" + "e" * 64,
        "clock_id": "source-001:video_pts",
        "time_base": {"num": 1, "den": 90000},
        "in_tick": 0,
        "out_tick": 90000,
        "interval": "[in,out)",
    }
    assert _validator("source-span-ref.schema.json").is_valid(value)
    _assert_invalid("source-span-ref.schema.json", {key: item for key, item in value.items() if key != "source_sha256"})
    _assert_invalid("source-span-ref.schema.json", {**value, "source_sha256": "sha256:" + "E" * 64})
    _assert_invalid("source-span-ref.schema.json", {**value, "artifact_ref": "source-001"})
    _assert_invalid("source-span-ref.schema.json", {**value, "time_base": {"num": 1}})
    _assert_invalid("source-span-ref.schema.json", {**value, "time_base": {"num": 1, "den": 0}})
    _assert_invalid("source-span-ref.schema.json", {**value, "time_base": {"num": 1, "den": 90000, "fps": 30}})
    _assert_invalid("source-span-ref.schema.json", {**value, "extra": True})


def test_source_span_temporal_semantics_require_reduced_nonempty_bounded_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(KERNEL_SOURCE))
    contracts = import_module("autocut_kernel.contracts")
    source_clock = contracts.SourceClockBinding(
        artifact_id="art_source_manifest",
        content_hash="sha256:" + "d" * 64,
        source_id="source-001",
        source_sha256="sha256:" + "e" * 64,
        clock_id="source-001:video_pts",
        numerator=1,
        denominator=90000,
        origin_tick=0,
        duration_tick=100,
    )
    valid = {
        "artifact_ref": {"artifact_id": "art_source_manifest", "content_hash": "sha256:" + "d" * 64},
        "source_id": "source-001",
        "source_sha256": "sha256:" + "e" * 64,
        "clock_id": "source-001:video_pts",
        "time_base": {"num": 1, "den": 90000},
        "in_tick": 10,
        "out_tick": 20,
    }
    contracts.validate_source_span_temporal_semantics(valid, source_clock=source_clock)

    with pytest.raises(ValueError, match="reduced"):
        contracts.validate_source_span_temporal_semantics(
            {**valid, "time_base": {"num": 2, "den": 4}}, source_clock=source_clock
        )
    with pytest.raises(ValueError, match="origin <= in < out"):
        contracts.validate_source_span_temporal_semantics(
            {**valid, "in_tick": 20, "out_tick": 20}, source_clock=source_clock
        )
    with pytest.raises(ValueError, match="origin <= in < out"):
        contracts.validate_source_span_temporal_semantics(
            {**valid, "in_tick": -1}, source_clock=source_clock
        )
    with pytest.raises(ValueError, match="origin <= in < out"):
        contracts.validate_source_span_temporal_semantics(
            {**valid, "out_tick": 101}, source_clock=source_clock
        )
    with pytest.raises(ValueError, match="resolved SourceClock owner"):
        contracts.validate_source_span_temporal_semantics(
            {**valid, "source_id": "different-source"}, source_clock=source_clock
        )


@pytest.mark.parametrize("byte_length", ["", "01", "-1", "1.0", 1])
def test_immutable_blob_ref_rejects_invalid_decimal_byte_lengths(byte_length: object) -> None:
    value = {
        "object_id": "blob_01",
        "sha256": "sha256:" + "f" * 64,
        "storage_locator": "object-store://controlled/blob_01",
        "media_type": "application/octet-stream",
        "byte_length_decimal": byte_length,
    }
    _assert_invalid("immutable-blob-ref.schema.json", value)


def test_immutable_blob_ref_accepts_zero_or_canonical_nonzero_length() -> None:
    validator = _validator("immutable-blob-ref.schema.json")
    base = {
        "object_id": "blob_01",
        "sha256": "sha256:" + "f" * 64,
        "storage_locator": "object-store://controlled/blob_01",
        "media_type": "application/octet-stream",
    }
    assert validator.is_valid({**base, "byte_length_decimal": "0"})
    assert validator.is_valid({**base, "byte_length_decimal": "12345"})
