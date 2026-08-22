"""Fail-closed resolution of the v2.1.3 closed Scope union.

Authority: v2-production-system-contracts.md#4.2;
sha256:7260bf922f8852ea22142220227fdda9a4e03e81433592c68957dffe08b7531d.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from .canonical import canonical_json_hash
from .errors import ContractCompilerError


@dataclass(frozen=True, slots=True)
class ScopeIdentity:
    """The namespace and primary key determined by one closed Scope value."""

    namespace_id: str
    primary_id: str


_FIELDS_BY_KIND: Final[dict[str, frozenset[str]]] = {
    "root_input": frozenset({"kind", "job_id", "root_input_id"}),
    "job": frozenset({"kind", "run_id", "job_id"}),
    "portfolio": frozenset({"kind", "run_id", "job_id", "portfolio_id"}),
    "story": frozenset({"kind", "run_id", "job_id", "portfolio_id", "story_id"}),
    "publication_batch": frozenset({"kind", "run_id", "job_id", "portfolio_id", "batch_id"}),
    "publication_lineage": frozenset(
        {"kind", "job_id", "publication_lineage_id", "visibility_domain_hash"}
    ),
    "run_lineage": frozenset({"kind", "job_id", "run_lineage_id", "recovery_budget_epoch_id"}),
    "job_execution": frozenset({"kind", "job_id", "job_execution_id"}),
}


def scope_identity(scope: Mapping[str, object]) -> ScopeIdentity:
    """Resolve the exact namespace/primary identity licensed by Scope §4.2.

    The resolver intentionally does not infer IDs from prefixes or accept a
    partial mapping.  JSON Schema validates serialized inputs; this dependency-
    free helper repeats the closed structural boundary for callers that receive
    an in-memory mapping.
    """

    if not isinstance(scope, Mapping):
        raise ContractCompilerError("scope must be a mapping")
    kind = scope.get("kind")
    if type(kind) is not str or kind not in _FIELDS_BY_KIND:  # noqa: E721
        raise ContractCompilerError("scope.kind must be one of the eight registered variants")
    expected = _FIELDS_BY_KIND[kind]
    actual = set(scope)
    if actual != expected:
        raise ContractCompilerError(f"scope {kind!r} must have exactly {sorted(expected)!r}")
    values = _validated_text_values(scope, expected)

    if kind == "root_input":
        return ScopeIdentity(namespace_id=values["root_input_id"], primary_id=values["job_id"])
    if kind == "job":
        return ScopeIdentity(namespace_id=values["run_id"], primary_id=values["job_id"])
    if kind == "portfolio":
        return ScopeIdentity(namespace_id=values["run_id"], primary_id=values["portfolio_id"])
    if kind == "story":
        return ScopeIdentity(namespace_id=values["run_id"], primary_id=values["story_id"])
    if kind == "publication_batch":
        return ScopeIdentity(namespace_id=values["run_id"], primary_id=values["batch_id"])
    if kind == "publication_lineage":
        _validate_sha256(values["visibility_domain_hash"], field="visibility_domain_hash")
        return ScopeIdentity(
            namespace_id=values["publication_lineage_id"],
            primary_id=canonical_json_hash(
                {"job_id": values["job_id"], "visibility_domain_hash": values["visibility_domain_hash"]}
            ),
        )
    if kind == "run_lineage":
        return ScopeIdentity(
            namespace_id=values["run_lineage_id"], primary_id=values["recovery_budget_epoch_id"]
        )
    # `kind` is closed above; this is the final job_execution variant.
    return ScopeIdentity(
        namespace_id=values["job_execution_id"], primary_id=values["job_execution_id"]
    )


def _validated_text_values(scope: Mapping[str, object], expected: frozenset[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for field in expected:
        value = scope[field]
        if type(value) is not str or not value:  # noqa: E721 - reject string subclasses and empty IDs.
            raise ContractCompilerError(f"scope.{field} must be a non-empty string")
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as error:
            raise ContractCompilerError(f"scope.{field} must be valid UTF-8 text") from error
        values[field] = value
    return values


def _validate_sha256(value: str, *, field: str) -> None:
    if len(value) != 71 or not value.startswith("sha256:") or any(
        character not in "0123456789abcdef" for character in value[7:]
    ):
        raise ContractCompilerError(f"scope.{field} must be a lowercase sha256 digest")
