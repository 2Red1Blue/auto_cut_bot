"""Shared closed primitives for the Stage 1--3 production contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final, Mapping, Sequence, cast

from ..contracts.compiler.canonical import canonical_json_bytes, canonical_json_hash
from ..contracts.compiler.refs import ArtifactRef, DomainRef

_SHA256: Final = re.compile(r"sha256:[0-9a-f]{64}\Z")
_IDENTIFIER: Final = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]*\Z")
_SAFE_TOKEN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]*\Z")
_EXACT_DECIMAL: Final = re.compile(r"(?:0(?:\.[0-9]+)?|1(?:\.0+)?)\Z")
_FORBIDDEN_FIELD_PARTS: Final = (
    "asr",
    "transcript",
    "vad",
    "start_seconds",
    "end_seconds",
    "physical_endpoint",
    "cut_endpoint",
)


class ProductionModelError(ValueError):
    """A Stage 1--3 value violates its closed production contract."""


class CanonicalModel:
    """Mixin for immutable values with a reproducible JCS content hash."""

    def to_mapping(self) -> dict[str, object]:
        raise NotImplementedError

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())


class EvaluatorOwnedModel(CanonicalModel):
    """A value whose public construction is restricted to a verifier/compiler."""

    def __new__(cls, *_args: object, **_kwargs: object) -> EvaluatorOwnedModel:
        raise TypeError(f"{cls.__name__} can only be constructed by its evaluator")


def mapping(value: object, expected: set[str], label: str) -> Mapping[str, object]:
    reject_forbidden_fields(value, label)
    if type(value) is not dict:  # noqa: E721 - the JSON boundary is exact.
        raise ProductionModelError(f"{label} must be an object")
    raw = cast(dict[object, object], value)
    if set(raw) != expected:
        raise ProductionModelError(f"{label} must have exactly {sorted(expected)}")
    return cast(Mapping[str, object], raw)


def reject_forbidden_fields(value: object, label: str) -> None:
    if type(value) is dict:  # noqa: E721
        for key, child in cast(dict[object, object], value).items():
            if type(key) is not str:  # noqa: E721
                raise ProductionModelError(f"{label} field names must be strings")
            folded = key.casefold()
            if any(part in folded for part in _FORBIDDEN_FIELD_PARTS):
                raise ProductionModelError(f"{label}.{key} is forbidden in Stage 1-3")
            reject_forbidden_fields(child, f"{label}.{key}")
    elif type(value) is list:  # noqa: E721
        for index, child in enumerate(cast(list[object], value)):
            reject_forbidden_fields(child, f"{label}[{index}]")
    elif type(value) is float:  # noqa: E721
        raise ProductionModelError(f"{label} must not contain float values")


def text(value: object, label: str) -> str:
    if type(value) is not str or not value.strip():  # noqa: E721
        raise ProductionModelError(f"{label} must be a non-empty string")
    return value


def identifier(value: object, label: str) -> str:
    result = text(value, label)
    if not _IDENTIFIER.fullmatch(result):
        raise ProductionModelError(f"{label} must be an opaque identifier, not a path")
    return result


def safe_token(value: object, label: str) -> str:
    result = text(value, label)
    if not _SAFE_TOKEN.fullmatch(result):
        raise ProductionModelError(f"{label} must be a safe opaque token")
    return result


def sha256(value: object, label: str) -> str:
    if type(value) is not str or not _SHA256.fullmatch(value):  # noqa: E721
        raise ProductionModelError(f"{label} must be a lowercase sha256 digest")
    return value


def integer(value: object, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:  # noqa: E721 - bool is forbidden.
        raise ProductionModelError(f"{label} must be an integer >= {minimum}")
    return value


def exact_decimal(value: object, label: str) -> str:
    result = text(value, label)
    if not _EXACT_DECIMAL.fullmatch(result):
        raise ProductionModelError(f"{label} must be an exact decimal in [0,1]")
    try:
        decimal = Decimal(result)
    except InvalidOperation as error:
        raise ProductionModelError(f"{label} must be an exact decimal in [0,1]") from error
    canonical = format(decimal.normalize(), "f")
    if canonical == "-0":
        canonical = "0"
    if result != canonical:
        raise ProductionModelError(f"{label} must use canonical decimal lexical form ({canonical})")
    return result


def object_list(value: object, label: str) -> list[object]:
    if type(value) is not list:  # noqa: E721
        raise ProductionModelError(f"{label} must be an array")
    return cast(list[object], value)


def artifact_ref(value: object, label: str) -> ArtifactRef:
    try:
        return ArtifactRef.from_mapping(value)
    except ValueError as error:
        raise ProductionModelError(f"{label} is invalid") from error


def domain_ref(value: object, label: str) -> DomainRef:
    try:
        return DomainRef.from_mapping(value)
    except ValueError as error:
        raise ProductionModelError(f"{label} is invalid") from error


def jcs_key(value: object) -> bytes:
    """Return the only comparison/equality key allowed for contract sets."""

    if isinstance(value, CanonicalModel):
        value = value.to_mapping()
    elif type(value) in {ArtifactRef, DomainRef}:  # noqa: E721
        value = value.to_mapping()  # type: ignore[union-attr]
    return canonical_json_bytes(value)


def canonical_values(
    values: Sequence[object],
    expected_type: type[object],
    label: str,
    *,
    nonempty: bool = False,
) -> tuple[object, ...]:
    result = tuple(values)
    if nonempty and not result:
        raise ProductionModelError(f"{label} must not be empty")
    if any(type(item) is not expected_type for item in result):  # noqa: E721
        raise ProductionModelError(f"{label} contains an invalid value")
    keys = tuple(jcs_key(item) for item in result)
    if len(keys) != len(set(keys)):
        raise ProductionModelError(f"{label} must not contain duplicates")
    if keys != tuple(sorted(keys)):
        raise ProductionModelError(f"{label} must use canonical JCS-byte order")
    return result


def canonical_domain_refs(
    values: Sequence[DomainRef], label: str, *, nonempty: bool = False
) -> tuple[DomainRef, ...]:
    return cast(
        tuple[DomainRef, ...],
        canonical_values(values, DomainRef, label, nonempty=nonempty),
    )


def canonical_artifact_refs(
    values: Sequence[ArtifactRef], label: str, *, nonempty: bool = False
) -> tuple[ArtifactRef, ...]:
    return cast(
        tuple[ArtifactRef, ...],
        canonical_values(values, ArtifactRef, label, nonempty=nonempty),
    )


def canonical_ids(values: Sequence[str], label: str, *, nonempty: bool = False) -> tuple[str, ...]:
    result = tuple(identifier(item, label) for item in values)
    if nonempty and not result:
        raise ProductionModelError(f"{label} must not be empty")
    keys = tuple(jcs_key(item) for item in result)
    if len(keys) != len(set(keys)):
        raise ProductionModelError(f"{label} must not contain duplicates")
    if keys != tuple(sorted(keys)):
        raise ProductionModelError(f"{label} must use canonical JCS-byte order")
    return result


@dataclass(frozen=True, slots=True)
class DurationRangeSeconds(CanonicalModel):
    minimum: int
    target: int
    maximum: int

    def __post_init__(self) -> None:
        minimum = integer(self.minimum, "duration.minimum", minimum=1)
        target = integer(self.target, "duration.target", minimum=1)
        maximum = integer(self.maximum, "duration.maximum", minimum=1)
        if not minimum <= target <= maximum:
            raise ProductionModelError("duration must satisfy minimum <= target <= maximum")

    @classmethod
    def from_mapping(cls, value: object) -> DurationRangeSeconds:
        item = mapping(value, {"min", "target", "max"}, "duration_seconds")
        return cls(
            integer(item["min"], "duration.min", minimum=1),
            integer(item["target"], "duration.target", minimum=1),
            integer(item["max"], "duration.max", minimum=1),
        )

    def to_mapping(self) -> dict[str, object]:
        return {"max": self.maximum, "min": self.minimum, "target": self.target}


@dataclass(frozen=True, slots=True)
class TimeBaseValue(CanonicalModel):
    numerator: int
    denominator: int

    def __post_init__(self) -> None:
        integer(self.numerator, "time_base.num", minimum=1)
        integer(self.denominator, "time_base.den", minimum=1)

    def to_mapping(self) -> dict[str, object]:
        return {"den": self.denominator, "num": self.numerator}


@dataclass(frozen=True, slots=True)
class RuleResult(CanonicalModel):
    rule_id: str
    status: str
    subject_hash: str

    def __post_init__(self) -> None:
        identifier(self.rule_id, "rule_id")
        if self.status not in {"pass", "fail", "indeterminate"}:
            raise ProductionModelError("rule result status is unknown")
        sha256(self.subject_hash, "subject_hash")

    def to_mapping(self) -> dict[str, object]:
        return {
            "rule_id": self.rule_id,
            "status": self.status,
            "subject_hash": self.subject_hash,
        }


@dataclass(frozen=True, slots=True)
class PendingBusinessMember(CanonicalModel):
    artifact_type: str
    artifact_ref: ArtifactRef

    def __post_init__(self) -> None:
        identifier(self.artifact_type, "artifact_type")
        if type(self.artifact_ref) is not ArtifactRef:  # noqa: E721
            raise ProductionModelError("artifact_ref must be an ArtifactRef")

    def to_mapping(self) -> dict[str, object]:
        return {
            "artifact_ref": self.artifact_ref.to_mapping(),
            "artifact_type": self.artifact_type,
        }


@dataclass(frozen=True, slots=True)
class PendingBusinessSet(CanonicalModel):
    pending_set_id: str
    admission_kind: str
    members: tuple[PendingBusinessMember, ...]

    def __post_init__(self) -> None:
        identifier(self.pending_set_id, "pending_set_id")
        identifier(self.admission_kind, "admission_kind")
        members = cast(
            tuple[PendingBusinessMember, ...],
            canonical_values(self.members, PendingBusinessMember, "pending members", nonempty=True),
        )
        types = tuple(item.artifact_type for item in members)
        if len(types) != len(set(types)):
            raise ProductionModelError("pending set must have exactly one member per artifact type")
        object.__setattr__(self, "members", members)

    def require_exact_types(self, expected: set[str]) -> None:
        actual = {item.artifact_type for item in self.members}
        if actual != expected:
            raise ProductionModelError(
                f"pending set member types mismatch: expected {sorted(expected)}, got {sorted(actual)}"
            )

    def require_member(self, artifact_type: str, value: CanonicalModel) -> ArtifactRef:
        matches = tuple(item for item in self.members if item.artifact_type == artifact_type)
        if len(matches) != 1 or matches[0].artifact_ref.content_hash != value.canonical_hash:
            raise ProductionModelError(f"pending {artifact_type} does not bind the exact payload")
        return matches[0].artifact_ref

    def to_mapping(self) -> dict[str, object]:
        return {
            "admission_kind": self.admission_kind,
            "members": [item.to_mapping() for item in self.members],
            "pending_set_id": self.pending_set_id,
        }


def require_passed_rules(
    values: Sequence[RuleResult], expected_rule_ids: set[str], subject_hash: str
) -> tuple[RuleResult, ...]:
    rules = cast(
        tuple[RuleResult, ...],
        canonical_values(values, RuleResult, "rule_results", nonempty=True),
    )
    if {item.rule_id for item in rules} != expected_rule_ids:
        raise ProductionModelError("rule_results do not exactly cover the frozen evaluator rules")
    if any(item.subject_hash != subject_hash for item in rules):
        raise ProductionModelError("rule_results bind an unrelated pending set")
    if any(item.status != "pass" for item in rules):
        raise ProductionModelError("continue cannot be minted while a required rule is not pass")
    return rules


def computed_rule_results(
    rule_ids: set[str], subject_hash: str, *, failed_rule_ids: set[str] | None = None
) -> tuple[RuleResult, ...]:
    """Create evaluator-owned results for one exact pending-set subject."""

    sha256(subject_hash, "rule result subject_hash")
    failed: set[str] = set() if failed_rule_ids is None else failed_rule_ids
    if not failed <= rule_ids:
        raise ProductionModelError("failed rule IDs are outside the frozen evaluator rules")
    values = tuple(
        RuleResult(rule_id, "fail" if rule_id in failed else "pass", subject_hash)
        for rule_id in rule_ids
    )
    return tuple(sorted(values, key=jcs_key))


__all__ = [
    "CanonicalModel",
    "DurationRangeSeconds",
    "EvaluatorOwnedModel",
    "PendingBusinessMember",
    "PendingBusinessSet",
    "ProductionModelError",
    "RuleResult",
    "TimeBaseValue",
    "artifact_ref",
    "canonical_artifact_refs",
    "canonical_domain_refs",
    "canonical_ids",
    "canonical_values",
    "computed_rule_results",
    "domain_ref",
    "exact_decimal",
    "identifier",
    "integer",
    "jcs_key",
    "mapping",
    "object_list",
    "reject_forbidden_fields",
    "require_passed_rules",
    "safe_token",
    "sha256",
    "text",
]
