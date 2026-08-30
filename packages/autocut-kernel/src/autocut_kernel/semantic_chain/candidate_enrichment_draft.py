"""Bounded untrusted Stage 2 candidate-enrichment draft values.

The provider sees and returns only invocation-local aliases.  Full semantic
references, source provenance, capabilities, IDs, and coarse timing belong to
the deterministic compiler.  A decoded draft is therefore content, never an
authority or an admission decision.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, fields
from typing import TypeVar, cast

from ..contracts.compiler.canonical import (
    canonical_json_hash,
    load_canonical_json_bytes,
)

CANDIDATE_ENRICHMENT_DRAFT_SCHEMA_VERSION = "candidate-enrichment-draft-v1"
_LOCAL_ID = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_ALIAS = re.compile(r"w[0-9]{4}/(?:event|fact)/[a-z][a-z0-9_]{0,63}\Z")
_DECIMAL = re.compile(r"(?:0|1|0\.[0-9]*[1-9])\Z")
_MEASUREMENT_KINDS = (
    "hook_strength",
    "reveal_strength",
    "emotional_payoff_strength",
    "dialogue_salience",
    "action_salience",
    "visual_salience",
)
_FORBIDDEN_FIELD_PARTS = (
    "frame",
    "asr",
    "vad",
    "transcript",
    "physical",
    "endpoint",
    "admission",
    "publication",
    "publish",
    "pass",
)
_T = TypeVar("_T")


class CandidateEnrichmentDraftError(ValueError):
    """The candidate draft is malformed, excessive, or reference-unclosed."""


@dataclass(frozen=True, slots=True)
class CandidateEnrichmentDraftPolicy:
    max_response_bytes: int
    max_candidates: int
    max_anchor_refs_per_candidate: int
    max_measurements_per_candidate: int
    max_evidence_refs_per_measurement: int
    max_text_characters: int
    max_total_text_characters: int

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if type(value) is not int or not 0 < value <= 2**53 - 1:  # noqa: E721
                raise CandidateEnrichmentDraftError(
                    "candidate draft bounds must be positive exact integers"
                )

    def to_mapping(self) -> dict[str, object]:
        return {field.name: getattr(self, field.name) for field in fields(self)}

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())


@dataclass(frozen=True, slots=True, order=True)
class CandidateEnrichmentAlias:
    alias: str
    object_type: str
    owner_window_manifest_sha256: str
    object_id: str
    direct_fact_aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _alias(self.alias)
        if self.object_type not in ("event", "fact"):
            raise CandidateEnrichmentDraftError("candidate alias object type is unsupported")
        if f"/{self.object_type}/" not in self.alias:
            raise CandidateEnrichmentDraftError("candidate alias kind disagrees with its value")
        _sha256(self.owner_window_manifest_sha256, "alias owner window")
        _sha256(self.object_id, "alias object ID")
        facts = _tuple_of(self.direct_fact_aliases, str, "direct fact aliases")
        if self.object_type == "fact" and facts:
            raise CandidateEnrichmentDraftError("fact aliases cannot declare direct facts")
        if len(facts) != len(set(facts)) or facts != tuple(sorted(facts)):
            raise CandidateEnrichmentDraftError("direct fact aliases must be canonical and unique")
        if any("/fact/" not in item for item in facts):
            raise CandidateEnrichmentDraftError("direct fact aliases must name facts")

    def to_mapping(self) -> dict[str, object]:
        return {
            "alias": self.alias,
            "object_type": self.object_type,
            "owner_window_manifest_sha256": self.owner_window_manifest_sha256,
            "object_id": self.object_id,
            "direct_fact_aliases": list(self.direct_fact_aliases),
        }


@dataclass(frozen=True, slots=True)
class CandidateEnrichmentReferenceCatalog:
    aliases: tuple[CandidateEnrichmentAlias, ...]

    def __post_init__(self) -> None:
        values = _tuple_of(self.aliases, CandidateEnrichmentAlias, "candidate aliases")
        keys = tuple(item.alias for item in values)
        if not values or len(keys) != len(set(keys)) or keys != tuple(sorted(keys)):
            raise CandidateEnrichmentDraftError(
                "candidate aliases must be non-empty, canonical, and unique"
            )
        known = set(keys)
        for item in values:
            if any(alias not in known for alias in item.direct_fact_aliases):
                raise CandidateEnrichmentDraftError("event alias has an unknown direct fact")
            if any(
                self.by_alias[alias].owner_window_manifest_sha256
                != item.owner_window_manifest_sha256
                for alias in item.direct_fact_aliases
            ):
                raise CandidateEnrichmentDraftError("event direct fact crosses its owner window")

    @property
    def by_alias(self) -> dict[str, CandidateEnrichmentAlias]:
        return {item.alias: item for item in self.aliases}

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash([item.to_mapping() for item in self.aliases])


@dataclass(frozen=True, slots=True)
class CandidateEnrichmentMeasurementDraft:
    measurement_kind: str
    value: str
    confidence: str
    evidence_refs: tuple[str, ...]

    def to_mapping(self) -> dict[str, object]:
        return {
            "measurement_kind": self.measurement_kind,
            "value": self.value,
            "confidence": self.confidence,
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True, slots=True)
class CandidateEnrichmentCandidateDraft:
    local_candidate_id: str
    summary: str
    anchor_refs: tuple[str, ...]
    semantic_measurements: tuple[CandidateEnrichmentMeasurementDraft, ...]

    def to_mapping(self) -> dict[str, object]:
        return {
            "local_candidate_id": self.local_candidate_id,
            "summary": self.summary,
            "anchor_refs": list(self.anchor_refs),
            "semantic_measurements": [
                item.to_mapping() for item in self.semantic_measurements
            ],
        }


@dataclass(frozen=True, slots=True)
class CandidateEnrichmentDraft:
    candidates: tuple[CandidateEnrichmentCandidateDraft, ...]

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": CANDIDATE_ENRICHMENT_DRAFT_SCHEMA_VERSION,
            "candidates": [item.to_mapping() for item in self.candidates],
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())


def _closed(value: object, keys: tuple[str, ...], label: str) -> dict[str, object]:
    if type(value) is not dict:  # noqa: E721
        raise CandidateEnrichmentDraftError(f"{label} must be a closed object")
    mapping = cast(dict[object, object], value)
    if any(type(key) is not str for key in mapping):  # noqa: E721
        raise CandidateEnrichmentDraftError(f"{label} field names must be strings")
    for key in cast(dict[str, object], mapping):
        folded = key.casefold()
        if any(part in folded for part in _FORBIDDEN_FIELD_PARTS):
            raise CandidateEnrichmentDraftError(f"{label}.{key} is forbidden")
    if set(mapping) != set(keys):
        raise CandidateEnrichmentDraftError(f"{label} has missing or unknown fields")
    return cast(dict[str, object], mapping)


def _array(
    value: object,
    maximum: int,
    label: str,
    *,
    minimum: int = 0,
) -> list[object]:
    if type(value) is not list:  # noqa: E721
        raise CandidateEnrichmentDraftError(f"{label} must be an array")
    result = cast(list[object], value)
    if not minimum <= len(result) <= maximum:
        raise CandidateEnrichmentDraftError(f"{label} violates its count bound")
    return result


def _tuple_of(value: object, item_type: type[_T], label: str) -> tuple[_T, ...]:
    if type(value) is not tuple:  # noqa: E721
        raise CandidateEnrichmentDraftError(f"{label} must be an exact tuple")
    items = cast(tuple[object, ...], value)
    if any(type(item) is not item_type for item in items):  # noqa: E721
        raise CandidateEnrichmentDraftError(f"{label} contains a wrong value type")
    return cast(tuple[_T, ...], items)


def _text(value: object, maximum: int, label: str) -> str:
    if (
        type(value) is not str  # noqa: E721
        or not value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise CandidateEnrichmentDraftError(f"{label} is invalid or excessive text")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise CandidateEnrichmentDraftError(f"{label} must be valid UTF-8") from error
    return value


def _local_id(value: object) -> str:
    if type(value) is not str or _LOCAL_ID.fullmatch(value) is None:  # noqa: E721
        raise CandidateEnrichmentDraftError("candidate local ID is invalid")
    return value


def _alias(value: object) -> str:
    if type(value) is not str or _ALIAS.fullmatch(value) is None:  # noqa: E721
        raise CandidateEnrichmentDraftError("candidate short reference is invalid")
    return value


def _sha256(value: object, label: str) -> str:
    if type(value) is not str or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:  # noqa: E721
        raise CandidateEnrichmentDraftError(f"{label} must be lowercase sha256")
    return value


def _decimal(value: object, label: str) -> str:
    if type(value) is not str or _DECIMAL.fullmatch(value) is None:  # noqa: E721
        raise CandidateEnrichmentDraftError(f"{label} must be canonical decimal text in [0,1]")
    return value


def _canonical_refs(
    value: object,
    maximum: int,
    label: str,
    *,
    expected_kind: str | None = None,
) -> tuple[str, ...]:
    raw = _array(value, maximum, label, minimum=1)
    refs = tuple(_alias(item) for item in raw)
    if len(refs) != len(set(refs)):
        raise CandidateEnrichmentDraftError(f"{label} contains duplicate references")
    if expected_kind is not None and any(f"/{expected_kind}/" not in item for item in refs):
        raise CandidateEnrichmentDraftError(f"{label} contains a wrong-kind reference")
    return tuple(sorted(refs))


def _measurement(
    value: object,
    *,
    policy: CandidateEnrichmentDraftPolicy,
) -> CandidateEnrichmentMeasurementDraft:
    item = _closed(
        value,
        ("measurement_kind", "value", "confidence", "evidence_refs"),
        "candidate measurement",
    )
    kind = item["measurement_kind"]
    if type(kind) is not str or kind not in _MEASUREMENT_KINDS:  # noqa: E721
        raise CandidateEnrichmentDraftError("candidate measurement kind is not registered")
    return CandidateEnrichmentMeasurementDraft(
        kind,
        _decimal(item["value"], "candidate measurement value"),
        _decimal(item["confidence"], "candidate measurement confidence"),
        _canonical_refs(
            item["evidence_refs"],
            policy.max_evidence_refs_per_measurement,
            "candidate measurement evidence",
        ),
    )


def _candidate(
    value: object,
    *,
    policy: CandidateEnrichmentDraftPolicy,
    references: CandidateEnrichmentReferenceCatalog,
) -> CandidateEnrichmentCandidateDraft:
    item = _closed(
        value,
        ("local_candidate_id", "summary", "anchor_refs", "semantic_measurements"),
        "candidate",
    )
    anchors = _canonical_refs(
        item["anchor_refs"],
        policy.max_anchor_refs_per_candidate,
        "candidate anchors",
        expected_kind="event",
    )
    known = references.by_alias
    if any(ref not in known for ref in anchors):
        raise CandidateEnrichmentDraftError("candidate contains an unknown anchor reference")
    owners = {known[ref].owner_window_manifest_sha256 for ref in anchors}
    if len(owners) != 1:
        raise CandidateEnrichmentDraftError("candidate anchors cross observation owners")
    measurements = tuple(
        _measurement(raw, policy=policy)
        for raw in _array(
            item["semantic_measurements"],
            policy.max_measurements_per_candidate,
            "candidate semantic measurements",
            minimum=1,
        )
    )
    kinds = tuple(measurement.measurement_kind for measurement in measurements)
    if len(kinds) != len(set(kinds)):
        raise CandidateEnrichmentDraftError("candidate measurement kinds must be unique")
    closure = set(anchors)
    for anchor in anchors:
        closure.update(known[anchor].direct_fact_aliases)
    owner = next(iter(owners))
    for measurement in measurements:
        if any(ref not in known for ref in measurement.evidence_refs):
            raise CandidateEnrichmentDraftError("measurement contains an unknown reference")
        if any(known[ref].owner_window_manifest_sha256 != owner for ref in measurement.evidence_refs):
            raise CandidateEnrichmentDraftError("measurement evidence crosses observation owners")
        if not set(measurement.evidence_refs) <= closure:
            raise CandidateEnrichmentDraftError("measurement evidence escapes anchor closure")
    measurements = tuple(
        sorted(
            measurements,
            key=lambda entry: (
                _MEASUREMENT_KINDS.index(entry.measurement_kind),
                entry.evidence_refs,
                entry.value,
                entry.confidence,
            ),
        )
    )
    return CandidateEnrichmentCandidateDraft(
        _local_id(item["local_candidate_id"]),
        _text(item["summary"], policy.max_text_characters, "candidate summary"),
        anchors,
        measurements,
    )


def decode_candidate_enrichment_draft(
    raw_response: bytes,
    *,
    policy: CandidateEnrichmentDraftPolicy,
    references: CandidateEnrichmentReferenceCatalog,
) -> CandidateEnrichmentDraft:
    """Decode one complete response and close every short reference.

    Arrays that express sets are sorted by the Kernel.  Duplicate aliases are
    rejected instead of silently deduplicated.
    """

    if type(raw_response) is not bytes:  # noqa: E721
        raise CandidateEnrichmentDraftError("candidate response must be exact bytes")
    if type(policy) is not CandidateEnrichmentDraftPolicy:  # noqa: E721
        raise CandidateEnrichmentDraftError("candidate response requires an exact draft policy")
    if type(references) is not CandidateEnrichmentReferenceCatalog:  # noqa: E721
        raise CandidateEnrichmentDraftError("candidate response requires an exact reference catalog")
    if not raw_response or len(raw_response) > policy.max_response_bytes:
        raise CandidateEnrichmentDraftError("candidate response violates its byte bound")
    try:
        value, _canonical = load_canonical_json_bytes(raw_response, origin="candidate_enrichment")
    except (ValueError, UnicodeError, RecursionError) as error:
        raise CandidateEnrichmentDraftError("candidate response is not strict JSON") from error
    root = _closed(value, ("schema_version", "candidates"), "candidate response")
    if root["schema_version"] != CANDIDATE_ENRICHMENT_DRAFT_SCHEMA_VERSION:
        raise CandidateEnrichmentDraftError("candidate response schema version is unsupported")
    candidates = tuple(
        _candidate(raw, policy=policy, references=references)
        for raw in _array(
            root["candidates"],
            policy.max_candidates,
            "candidate response candidates",
            minimum=1,
        )
    )
    ids = tuple(item.local_candidate_id for item in candidates)
    if len(ids) != len(set(ids)):
        raise CandidateEnrichmentDraftError("candidate local IDs must be unique")
    candidates = tuple(sorted(candidates, key=lambda item: item.local_candidate_id))
    total_text = sum(len(item.summary) for item in candidates)
    if total_text > policy.max_total_text_characters:
        raise CandidateEnrichmentDraftError("candidate response exceeds its total text bound")
    return CandidateEnrichmentDraft(candidates)


def candidate_enrichment_response_schema(
    policy: CandidateEnrichmentDraftPolicy,
) -> dict[str, object]:
    """Return the closed provider JSON Schema for a future lifecycle request."""

    if type(policy) is not CandidateEnrichmentDraftPolicy:  # noqa: E721
        raise CandidateEnrichmentDraftError("response schema requires an exact draft policy")

    def closed(properties: dict[str, object], required: tuple[str, ...]) -> dict[str, object]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": properties,
            "required": list(required),
        }

    alias = {"type": "string", "pattern": _ALIAS.pattern[:-2] + "$"}
    measurement = closed(
        {
            "measurement_kind": {"type": "string", "enum": list(_MEASUREMENT_KINDS)},
            "value": {"type": "string", "pattern": _DECIMAL.pattern[:-2] + "$"},
            "confidence": {"type": "string", "pattern": _DECIMAL.pattern[:-2] + "$"},
            "evidence_refs": {
                "type": "array",
                "minItems": 1,
                "maxItems": policy.max_evidence_refs_per_measurement,
                "uniqueItems": True,
                "items": alias,
            },
        },
        ("measurement_kind", "value", "confidence", "evidence_refs"),
    )
    candidate = closed(
        {
            "local_candidate_id": {"type": "string", "pattern": _LOCAL_ID.pattern[:-2] + "$"},
            "summary": {
                "type": "string",
                "minLength": 1,
                "maxLength": policy.max_text_characters,
            },
            "anchor_refs": {
                "type": "array",
                "minItems": 1,
                "maxItems": policy.max_anchor_refs_per_candidate,
                "uniqueItems": True,
                "items": alias,
            },
            "semantic_measurements": {
                "type": "array",
                "minItems": 1,
                "maxItems": policy.max_measurements_per_candidate,
                "items": measurement,
            },
        },
        ("local_candidate_id", "summary", "anchor_refs", "semantic_measurements"),
    )
    return closed(
        {
            "schema_version": {"const": CANDIDATE_ENRICHMENT_DRAFT_SCHEMA_VERSION},
            "candidates": {
                "type": "array",
                "minItems": 1,
                "maxItems": policy.max_candidates,
                "items": candidate,
            },
        },
        ("schema_version", "candidates"),
    )
