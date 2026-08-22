"""Focused regression coverage for the dependency-free compiler foundation."""

from __future__ import annotations

import hashlib
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


def _source_input(authority_root: Path):
    contracts = import_module("autocut_kernel.contracts")
    document = authority_root / "v2-production-system-contracts.md"
    document.write_text("# Compiler Foundation\n", encoding="utf-8")
    metadata = contracts.SourceMetadata.from_mapping(
        {
            "contract_path": {"document": document.name, "anchor": "#compiler-foundation"},
            "source_document_sha256": "sha256:" + hashlib.sha256(document.read_bytes()).hexdigest(),
            "reviewer": "contract-reviewer",
        }
    )
    return contracts.SourceInput.from_json_bytes(
        path="common/compiler-foundation.json",
        raw=b'{"kind":"compiler_foundation","revision":1}',
        metadata=metadata,
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
    authority_root = tmp_path / "authority"
    authority_root.mkdir()
    source = _source_input(authority_root)
    output = {"schemas/foundation.json": b'{"type":"object"}\n', "registry/empty.json": b"{}\n"}

    first = contracts.write_generated_tree(
        tmp_path / "first",
        generated_files=output,
        sources=(source,),
        authority_root=authority_root,
        compiler_version="0.1.0",
    )
    second = contracts.write_generated_tree(
        tmp_path / "second",
        generated_files=dict(reversed(tuple(output.items()))),
        sources=(source,),
        authority_root=authority_root,
        compiler_version="0.1.0",
    )

    assert first.to_bytes() == second.to_bytes()
    assert first.sha256 == second.sha256
    assert contracts.check_generated_tree(
        tmp_path / "first",
        generated_files=output,
        sources=(source,),
        authority_root=authority_root,
        compiler_version="0.1.0",
    ).sha256 == first.sha256


def test_generated_tree_drift_and_unowned_directory_are_rejected(tmp_path: Path) -> None:
    contracts = import_module("autocut_kernel.contracts")
    authority_root = tmp_path / "authority"
    authority_root.mkdir()
    source = _source_input(authority_root)
    output = {"schemas/foundation.json": b'{"type":"object"}\n'}
    generated_root = tmp_path / "generated"
    contracts.write_generated_tree(
        generated_root,
        generated_files=output,
        sources=(source,),
        authority_root=authority_root,
        compiler_version="0.1.0",
    )

    (generated_root / "schemas" / "foundation.json").write_bytes(b"manual edit\n")
    with pytest.raises(contracts.GeneratedTreeDriftError, match="manifest|drift"):
        contracts.check_generated_tree(
            generated_root,
            generated_files=output,
            sources=(source,),
            authority_root=authority_root,
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
            authority_root=authority_root,
            compiler_version="0.1.0",
        )

    empty_unowned_root = tmp_path / "empty-hand-maintained"
    empty_unowned_root.mkdir()
    with pytest.raises(contracts.GeneratedTreeOwnershipError, match="ownership marker"):
        contracts.write_generated_tree(
            empty_unowned_root,
            generated_files=output,
            sources=(source,),
            authority_root=authority_root,
            compiler_version="0.1.0",
        )


def test_machine_source_requires_exact_regular_authority_document(tmp_path: Path) -> None:
    contracts = import_module("autocut_kernel.contracts")
    authority_root = tmp_path / "authority"
    authority_root.mkdir()
    document = authority_root / "v2-production-system-contracts.md"
    document.write_text("# frozen authority\n", encoding="utf-8")
    metadata = contracts.SourceMetadata.from_mapping(
        {
            "contract_path": {
                "document": document.name,
                "anchor": "#frozen-authority",
            },
            "source_document_sha256": "sha256:" + hashlib.sha256(document.read_bytes()).hexdigest(),
            "reviewer": "contract-reviewer",
        }
    )
    source = contracts.SourceInput.from_json_bytes(
        path="common/frozen.json", raw=b'{"kind":"frozen"}', metadata=metadata
    )

    contracts.verify_source_authority(source=source, authority_root=authority_root)
    document.write_text("# changed authority\n", encoding="utf-8")
    with pytest.raises(contracts.AuthorityIntegrityError, match="digest differs"):
        contracts.verify_source_authority(source=source, authority_root=authority_root)

    missing_anchor = contracts.SourceMetadata.from_mapping(
        {
            "contract_path": {"document": document.name, "anchor": "#missing-heading"},
            "source_document_sha256": "sha256:" + hashlib.sha256(document.read_bytes()).hexdigest(),
            "reviewer": "contract-reviewer",
        }
    )
    source_with_missing_anchor = contracts.SourceInput.from_json_bytes(
        path="common/missing-anchor.json", raw=b'{"kind":"missing-anchor"}', metadata=missing_anchor
    )
    with pytest.raises(contracts.AuthorityIntegrityError, match="does not identify a heading"):
        contracts.verify_source_authority(
            source=source_with_missing_anchor, authority_root=authority_root
        )

    linked_directory = authority_root / "linked"
    linked_directory.symlink_to(tmp_path, target_is_directory=True)
    linked_metadata = contracts.SourceMetadata.from_mapping(
        {
            "contract_path": {"document": "linked/outside.md", "anchor": "#outside"},
            "source_document_sha256": "sha256:" + "0" * 64,
            "reviewer": "contract-reviewer",
        }
    )
    linked_source = contracts.SourceInput.from_json_bytes(
        path="common/linked.json", raw=b'{"kind":"linked"}', metadata=linked_metadata
    )
    with pytest.raises(contracts.AuthorityIntegrityError, match="regular non-symlink"):
        contracts.verify_source_authority(source=linked_source, authority_root=authority_root)

    duplicate = authority_root / "duplicate.md"
    duplicate.write_text("# Repeated\n\n## Repeated\n", encoding="utf-8")
    duplicate_metadata = contracts.SourceMetadata.from_mapping(
        {
            "contract_path": {"document": duplicate.name, "anchor": "#repeated"},
            "source_document_sha256": "sha256:" + hashlib.sha256(duplicate.read_bytes()).hexdigest(),
            "reviewer": "contract-reviewer",
        }
    )
    duplicate_source = contracts.SourceInput.from_json_bytes(
        path="common/duplicate-anchor.json", raw=b'{"kind":"duplicate-anchor"}', metadata=duplicate_metadata
    )
    with pytest.raises(contracts.AuthorityIntegrityError, match="multiple authority headings"):
        contracts.verify_source_authority(source=duplicate_source, authority_root=authority_root)


def test_generated_tree_requires_current_authority_and_rejects_invalid_documents(tmp_path: Path) -> None:
    contracts = import_module("autocut_kernel.contracts")
    authority_root = tmp_path / "authority"
    authority_root.mkdir()
    source = _source_input(authority_root)
    output = {"schemas/foundation.json": b'{}\n'}
    generated_root = tmp_path / "generated"
    contracts.write_generated_tree(
        generated_root,
        generated_files=output,
        sources=(source,),
        authority_root=authority_root,
        compiler_version="0.1.0",
    )
    document = authority_root / "v2-production-system-contracts.md"
    document.write_text("# Changed\n", encoding="utf-8")
    with pytest.raises(contracts.AuthorityIntegrityError, match="digest differs"):
        contracts.check_generated_tree(
            generated_root,
            generated_files=output,
            sources=(source,),
            authority_root=authority_root,
            compiler_version="0.1.0",
        )

    missing_document = contracts.SourceMetadata.from_mapping(
        {
            "contract_path": {"document": "missing.md", "anchor": "#missing"},
            "source_document_sha256": "sha256:" + "0" * 64,
            "reviewer": "contract-reviewer",
        }
    )
    missing_source = contracts.SourceInput.from_json_bytes(
        path="common/missing.json", raw=b'{"kind":"missing"}', metadata=missing_document
    )
    with pytest.raises(contracts.AuthorityIntegrityError, match="regular non-symlink"):
        contracts.verify_source_authority(source=missing_source, authority_root=authority_root)

    invalid_document = authority_root / "invalid.md"
    invalid_document.write_bytes(b"\xff")
    invalid_metadata = contracts.SourceMetadata.from_mapping(
        {
            "contract_path": {"document": invalid_document.name, "anchor": "#invalid"},
            "source_document_sha256": "sha256:" + hashlib.sha256(invalid_document.read_bytes()).hexdigest(),
            "reviewer": "contract-reviewer",
        }
    )
    invalid_source = contracts.SourceInput.from_json_bytes(
        path="common/invalid.json", raw=b'{"kind":"invalid"}', metadata=invalid_metadata
    )
    with pytest.raises(contracts.AuthorityIntegrityError, match="valid UTF-8"):
        contracts.verify_source_authority(source=invalid_source, authority_root=authority_root)

    linked_document = authority_root / "linked.md"
    linked_document.symlink_to(invalid_document)
    linked_metadata = contracts.SourceMetadata.from_mapping(
        {
            "contract_path": {"document": linked_document.name, "anchor": "#invalid"},
            "source_document_sha256": "sha256:" + "0" * 64,
            "reviewer": "contract-reviewer",
        }
    )
    linked_source = contracts.SourceInput.from_json_bytes(
        path="common/linked-file.json", raw=b'{"kind":"linked-file"}', metadata=linked_metadata
    )
    with pytest.raises(contracts.AuthorityIntegrityError, match="regular non-symlink"):
        contracts.verify_source_authority(source=linked_source, authority_root=authority_root)
