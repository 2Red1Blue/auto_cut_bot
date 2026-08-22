"""Focused regression coverage for the dependency-free compiler foundation."""

from __future__ import annotations

from importlib import import_module
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
KERNEL_SOURCE = REPOSITORY_ROOT / "packages" / "autocut-kernel" / "src"


@pytest.fixture(autouse=True)
def _load_kernel_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.syspath_prepend(str(KERNEL_SOURCE))


def _metadata():
    contracts = import_module("autocut_kernel.contracts")
    return contracts.SourceMetadata.from_mapping(
        {
            "contract_path": {
                "document": "v2-production-system-contracts.md",
                "anchor": "#compiler-foundation",
            },
            "source_document_sha256": "sha256:" + "a" * 64,
            "reviewer": "contract-reviewer",
        }
    )


def _source_input():
    contracts = import_module("autocut_kernel.contracts")
    return contracts.SourceInput.from_json_bytes(
        path="common/compiler-foundation.json",
        raw=b'{"kind":"compiler_foundation","revision":1}',
        metadata=_metadata(),
    )


def test_source_metadata_is_closed_and_contract_path_is_safe() -> None:
    contracts = import_module("autocut_kernel.contracts")
    with pytest.raises(contracts.SourceMetadataError if hasattr(contracts, "SourceMetadataError") else ValueError):
        contracts.SourceMetadata.from_mapping(
            {
                "contract_path": {"document": "../outside.md", "anchor": "#section"},
                "source_document_sha256": "sha256:" + "a" * 64,
                "reviewer": "contract-reviewer",
                "unreviewed_default": True,
            }
        )


def test_canonical_json_is_reproducible_and_rejects_float_values() -> None:
    contracts = import_module("autocut_kernel.contracts")
    first = contracts.canonical_json_bytes({"z": [2, 1], "a": "中"})
    second = contracts.canonical_json_bytes({"a": "中", "z": [2, 1]})

    assert first == second == '{"a":"中","z":[2,1]}'.encode("utf-8")
    assert contracts.canonical_json_hash({"a": 1}).startswith("sha256:")
    with pytest.raises(ValueError, match="float"):
        contracts.canonical_json_bytes({"seconds": 0.5})
    with pytest.raises(ValueError, match="safe range"):
        contracts.canonical_json_bytes({"too_large": 2**53})
    with pytest.raises(ValueError, match="invalid Unicode"):
        contracts.canonical_json_bytes({"bad": "\ud800"})


def test_json_source_rejects_duplicate_keys_and_uses_jcs_utf16_key_order(tmp_path: Path) -> None:
    contracts = import_module("autocut_kernel.contracts")
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_bytes(b'{"rule_id":"first","rule_id":"second"}')
    with pytest.raises(ValueError, match="duplicate JSON object key"):
        contracts.load_json_source(
            duplicate,
            relative_path="common/duplicate.json",
            metadata=_metadata(),
        )

    nested_duplicate = tmp_path / "nested-duplicate.json"
    nested_duplicate.write_bytes(b'{"nested":{"rule_id":"first","rule_id":"second"}}')
    with pytest.raises(ValueError, match="duplicate JSON object key"):
        contracts.load_json_source(
            nested_duplicate,
            relative_path="common/nested-duplicate.json",
            metadata=_metadata(),
        )

    # UTF-16 order puts the high surrogate of 😀 before U+E000; Unicode code-point
    # order would put these keys in the opposite order.
    assert contracts.canonical_json_bytes({"\ue000": 1, "😀": 2}) == '{"😀":2,"\ue000":1}'.encode(
        "utf-8"
    )


def test_generated_snapshot_and_manifest_are_reproducible(tmp_path: Path) -> None:
    contracts = import_module("autocut_kernel.contracts")
    source = _source_input()
    output = {"schemas/foundation.json": b'{"type":"object"}\n', "registry/empty.json": b"{}\n"}

    first = contracts.write_generated_tree(
        tmp_path / "first",
        generated_files=output,
        sources=(source,),
        compiler_version="0.1.0",
    )
    second = contracts.write_generated_tree(
        tmp_path / "second",
        generated_files=dict(reversed(tuple(output.items()))),
        sources=(source,),
        compiler_version="0.1.0",
    )

    assert first.to_bytes() == second.to_bytes()
    assert first.sha256 == second.sha256
    assert contracts.check_generated_tree(
        tmp_path / "first",
        generated_files=output,
        sources=(source,),
        compiler_version="0.1.0",
    ).sha256 == first.sha256


def test_generated_tree_drift_and_unowned_directory_are_rejected(tmp_path: Path) -> None:
    contracts = import_module("autocut_kernel.contracts")
    source = _source_input()
    output = {"schemas/foundation.json": b'{"type":"object"}\n'}
    generated_root = tmp_path / "generated"
    contracts.write_generated_tree(
        generated_root,
        generated_files=output,
        sources=(source,),
        compiler_version="0.1.0",
    )

    (generated_root / "schemas" / "foundation.json").write_bytes(b"manual edit\n")
    with pytest.raises(contracts.GeneratedTreeDriftError, match="manifest|drift"):
        contracts.check_generated_tree(
            generated_root,
            generated_files=output,
            sources=(source,),
            compiler_version="0.1.0",
        )

    unowned_root = tmp_path / "hand-maintained"
    unowned_root.mkdir()
    (unowned_root / "notes.txt").write_text("do not replace", encoding="utf-8")
    with pytest.raises(contracts.GeneratedTreeOwnershipError, match="ownership marker"):
        contracts.write_generated_tree(
            unowned_root,
            generated_files=output,
            sources=(source,),
            compiler_version="0.1.0",
        )

    empty_unowned_root = tmp_path / "empty-hand-maintained"
    empty_unowned_root.mkdir()
    with pytest.raises(contracts.GeneratedTreeOwnershipError, match="ownership marker"):
        contracts.write_generated_tree(
            empty_unowned_root,
            generated_files=output,
            sources=(source,),
            compiler_version="0.1.0",
        )
