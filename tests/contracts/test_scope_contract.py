"""Executable coverage for the v2.1.3 Scope union and derived identity."""

from __future__ import annotations

import json
from importlib import import_module
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCOPE_SCHEMA = (
    REPOSITORY_ROOT
    / "packages"
    / "autocut-kernel"
    / "src"
    / "autocut_kernel"
    / "contracts"
    / "source"
    / "2_1_3"
    / "common"
    / "schemas"
    / "primitives"
    / "scope.schema.json"
)
KERNEL_SOURCE = REPOSITORY_ROOT / "packages" / "autocut-kernel" / "src"
AUTHORITY_HASH = "sha256:7260bf922f8852ea22142220227fdda9a4e03e81433592c68957dffe08b7531d"


def _scope_schema() -> dict[str, object]:
    return json.loads(SCOPE_SCHEMA.read_text(encoding="utf-8"))


def _validator() -> Draft202012Validator:
    return Draft202012Validator(_scope_schema())


def _invalid(value: object) -> None:
    assert list(_validator().iter_errors(value)), value


@pytest.mark.parametrize(
    ("scope", "namespace_id", "primary_id"),
    [
        ({"kind": "root_input", "job_id": "job_1", "root_input_id": "root_1"}, "root_1", "job_1"),
        ({"kind": "job", "run_id": "run_1", "job_id": "job_1"}, "run_1", "job_1"),
        (
            {"kind": "portfolio", "run_id": "run_1", "job_id": "job_1", "portfolio_id": "portfolio_1"},
            "run_1",
            "portfolio_1",
        ),
        (
            {"kind": "story", "run_id": "run_1", "job_id": "job_1", "portfolio_id": "portfolio_1", "story_id": "story_1"},
            "run_1",
            "story_1",
        ),
        (
            {"kind": "publication_batch", "run_id": "run_1", "job_id": "job_1", "portfolio_id": "portfolio_1", "batch_id": "batch_1"},
            "run_1",
            "batch_1",
        ),
        (
            {"kind": "run_lineage", "job_id": "job_1", "run_lineage_id": "runline_1", "recovery_budget_epoch_id": "recovery_epoch_1"},
            "runline_1",
            "recovery_epoch_1",
        ),
        (
            {"kind": "job_execution", "job_id": "job_1", "job_execution_id": "jobexec_1"},
            "jobexec_1",
            "jobexec_1",
        ),
    ],
)
def test_closed_scope_variants_validate_and_resolve(
    monkeypatch: pytest.MonkeyPatch, scope: dict[str, str], namespace_id: str, primary_id: str
) -> None:
    assert _validator().is_valid(scope)
    monkeypatch.syspath_prepend(str(KERNEL_SOURCE))
    identity = import_module("autocut_kernel.contracts").scope_identity(scope)
    assert (identity.namespace_id, identity.primary_id) == (namespace_id, primary_id)


def test_publication_lineage_has_jcs_derived_primary_and_distinct_domains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(KERNEL_SOURCE))
    contracts = import_module("autocut_kernel.contracts")
    first = {
        "kind": "publication_lineage",
        "job_id": "job_1",
        "publication_lineage_id": "publine_1",
        "visibility_domain_hash": "sha256:" + "a" * 64,
    }
    second = {**first, "visibility_domain_hash": "sha256:" + "b" * 64}
    assert _validator().is_valid(first)
    first_identity = contracts.scope_identity(first)
    second_identity = contracts.scope_identity(second)
    assert first_identity.namespace_id == "publine_1"
    assert first_identity.primary_id == contracts.canonical_json_hash(
        {"job_id": "job_1", "visibility_domain_hash": first["visibility_domain_hash"]}
    )
    assert first_identity.primary_id != second_identity.primary_id


def test_scope_schema_is_closed_eight_variant_and_authority_bound() -> None:
    schema = _scope_schema()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert AUTHORITY_HASH in schema["$comment"]
    assert len(schema["oneOf"]) == 8
    assert set(schema["$defs"]) == {
        "non_empty_text", "root_input", "job", "portfolio", "story", "publication_batch",
        "publication_lineage", "run_lineage", "job_execution",
    }


@pytest.mark.parametrize(
    "scope",
    [
        {},
        {"kind": "unknown", "job_id": "job_1"},
        {"kind": "job", "run_id": "run_1"},
        {"kind": "job", "run_id": "run_1", "job_id": "job_1", "extra": "no"},
        {"kind": "job", "run_id": "", "job_id": "job_1"},
        {"kind": "publication_lineage", "job_id": "job_1", "publication_lineage_id": "publine_1", "visibility_domain_hash": "sha256:" + "A" * 64},
    ],
)
def test_scope_schema_rejects_missing_unknown_wrong_or_empty_fields(scope: object) -> None:
    _invalid(scope)


@pytest.mark.parametrize(
    "scope",
    [
        {},
        {"kind": "job", "run_id": "run_1"},
        {"kind": "job", "run_id": "run_1", "job_id": "job_1", "unlicensed": "x"},
        {"kind": "root_input", "job_id": "job_1", "root_input_id": ""},
        {"kind": "root_input", "job_id": "job_1", "root_input_id": "\ud800"},
        {"kind": "publication_lineage", "job_id": "job_1", "publication_lineage_id": "publine_1", "visibility_domain_hash": "not-a-hash"},
    ],
)
def test_scope_resolver_fails_closed_for_non_structural_input(
    monkeypatch: pytest.MonkeyPatch, scope: dict[str, object]
) -> None:
    monkeypatch.syspath_prepend(str(KERNEL_SOURCE))
    with pytest.raises(ValueError):
        import_module("autocut_kernel.contracts").scope_identity(scope)
