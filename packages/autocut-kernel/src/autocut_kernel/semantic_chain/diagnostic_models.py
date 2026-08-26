"""Closed diagnostic values, not proof of evidence truth or KC admission.

External references identify earlier content only. The compiler/evaluator must
resolve their actual observations and independently verify draft/continuity
cause hashes. Claims are local objects: none embeds its owning member hash.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, TypeVar, cast

from ..contracts.compiler.canonical import canonical_json_bytes, canonical_json_hash
from .member_refs import SemanticMemberIdentity, SemanticObjectRef
from .narrative_models import Confidence

_T = TypeVar("_T")
_EARLIER = {
    "whole_series_source_manifest": ("source", "source_window"),
    "vlm_semantic_pack": ("vlm_entity", "vlm_fact", "vlm_event"),
    "event_card_set": ("event", "source_range"),
    "episode_digest_set": ("episode_digest",),
    "narrative_graph": (
        "entity",
        "fact",
        "beat",
        "obligation",
        "story_thread",
        "character",
        "character_state",
        "relationship",
        "question",
        "foreshadow",
        "edge",
    ),
}


class DiagnosticModelError(ValueError):
    """Malformed closed diagnostic content or broken local claim references."""


def _text(value: object) -> str:
    if type(value) is not str or not value.strip():  # noqa: E721
        raise DiagnosticModelError("diagnostic text must be nonempty UTF-8")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise DiagnosticModelError("diagnostic text must be valid UTF-8") from error
    return value


def _hash(value: object) -> str:
    text = _text(value)
    if re.fullmatch(r"sha256:[0-9a-f]{64}", text) is None:
        raise DiagnosticModelError("diagnostic identity must be lowercase sha256")
    return text


def _enum(value: object, choices: tuple[str, ...]) -> str:
    result = _text(value)
    if result not in choices:
        raise DiagnosticModelError("unsupported diagnostic enum")
    return result


def _closed(value: object, keys: tuple[str, ...]) -> dict[str, object]:
    if type(value) is not dict:  # noqa: E721
        raise DiagnosticModelError("diagnostic wire value must be a closed object")
    item = cast(dict[str, object], value)
    if any(type(key) is not str for key in item) or set(item) != set(keys):  # noqa: E721
        raise DiagnosticModelError("diagnostic object has missing or unknown fields")
    return item


def _array(value: object, parse: Callable[[object], _T]) -> tuple[_T, ...]:
    if type(value) is not list:  # noqa: E721
        raise DiagnosticModelError("diagnostic wire collection must be an array")
    return tuple(parse(item) for item in cast(list[object], value))


def _tuple(value: object, kind: type[_T], *, minimum: int = 0) -> tuple[_T, ...]:
    if type(value) is not tuple:  # noqa: E721
        raise DiagnosticModelError("diagnostic collections must be actual tuples")
    items = cast(tuple[object, ...], value)
    if len(items) < minimum or any(type(item) is not kind for item in items):
        raise DiagnosticModelError("diagnostic collection has missing or mistyped values")
    return cast(tuple[_T, ...], items)


def _records(value: object, kind: type[_T], key: Callable[[_T], str]) -> tuple[_T, ...]:
    items = _tuple(value, kind)
    if len({key(item) for item in items}) != len(items):
        raise DiagnosticModelError("diagnostic local IDs must be unique")
    return tuple(sorted(items, key=key))


def _ref(value: object) -> SemanticObjectRef:
    try:
        return SemanticObjectRef.from_mapping(value)
    except ValueError as error:
        raise DiagnosticModelError("malformed diagnostic object reference") from error


def _member(value: object) -> SemanticMemberIdentity:
    try:
        return SemanticMemberIdentity.from_mapping(value)
    except ValueError as error:
        raise DiagnosticModelError("malformed diagnostic member identity") from error


def _earlier(value: object) -> SemanticObjectRef:
    if type(value) is not SemanticObjectRef:  # noqa: E721
        raise DiagnosticModelError("diagnostic references must be exact SemanticObjectRef")
    if value.object_type not in _EARLIER.get(value.member_ref.artifact_type, ()):
        raise DiagnosticModelError("diagnostic reference has an unsupported or later owner")
    if (
        value.member_ref.artifact_type == "vlm_semantic_pack"
        or value.object_type == "source_window"
    ):
        _hash(value.object_id)
    return value


def _owned(value: object, artifact_type: str, object_type: str) -> SemanticObjectRef:
    ref = _earlier(value)
    if ref.member_ref.artifact_type != artifact_type or ref.object_type != object_type:
        raise DiagnosticModelError("diagnostic reference has the wrong object owner")
    return ref


def _refs(value: object, *, minimum: int = 1) -> tuple[SemanticObjectRef, ...]:
    items = _tuple(value, SemanticObjectRef, minimum=minimum)
    for item in items:
        _earlier(item)
    if len(set(items)) != len(items):
        raise DiagnosticModelError("diagnostic references must be unique")
    return tuple(sorted(items, key=lambda item: canonical_json_bytes(item.to_mapping())))


def _fixed(item: dict[str, object], key: str, expected: str) -> None:
    if type(item[key]) is not str or item[key] != expected:  # noqa: E721
        raise DiagnosticModelError("derived diagnostic field cannot be overridden")


class _Value:
    __slots__ = ()

    def to_mapping(self) -> dict[str, object]:
        raise NotImplementedError

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())


@dataclass(frozen=True, slots=True)
class ContinuityClaimValue(_Value):
    source_window_ref: SemanticObjectRef
    pack_ref: SemanticMemberIdentity
    direction: str
    continues: bool
    state_fact_refs: tuple[SemanticObjectRef, ...]

    def __post_init__(self) -> None:
        _owned(self.source_window_ref, "whole_series_source_manifest", "source_window")
        if (
            type(self.pack_ref) is not SemanticMemberIdentity
            or self.pack_ref.artifact_type != "vlm_semantic_pack"
        ):  # noqa: E721
            raise DiagnosticModelError("continuity requires an exact VLM pack identity")
        _enum(self.direction, ("previous", "next"))
        if type(self.continues) is not bool:  # noqa: E721
            raise DiagnosticModelError("continuation must be an exact boolean")
        refs = _refs(self.state_fact_refs, minimum=0)
        for ref in refs:
            _owned(ref, "vlm_semantic_pack", "vlm_fact")
            if ref.member_ref != self.pack_ref:
                raise DiagnosticModelError("continuity state Facts must belong to its exact pack")
        if bool(refs) != self.continues:
            raise DiagnosticModelError("continuity Fact presence must match its raw boolean")
        object.__setattr__(self, "state_fact_refs", refs)

    def to_mapping(self) -> dict[str, object]:
        return {
            "source_window_ref": self.source_window_ref.to_mapping(),
            "pack_ref": self.pack_ref.to_mapping(),
            "direction": self.direction,
            "continues": self.continues,
            "state_fact_refs": [ref.to_mapping() for ref in self.state_fact_refs],
        }

    @classmethod
    def from_mapping(cls, value: object) -> ContinuityClaimValue:
        item = _closed(
            value, ("source_window_ref", "pack_ref", "direction", "continues", "state_fact_refs")
        )
        return cls(
            _ref(item["source_window_ref"]),
            _member(item["pack_ref"]),
            _text(item["direction"]),
            cast(bool, item["continues"]),
            _array(item["state_fact_refs"], _ref),
        )


@dataclass(frozen=True, slots=True)
class IdentityObservationValue(_Value):
    observation_ref: SemanticObjectRef

    def __post_init__(self) -> None:
        _owned(self.observation_ref, "vlm_semantic_pack", "vlm_entity")

    def to_mapping(self) -> dict[str, object]:
        return {"observation_ref": self.observation_ref.to_mapping()}

    @classmethod
    def from_mapping(cls, value: object) -> IdentityObservationValue:
        return cls(_ref(_closed(value, ("observation_ref",))["observation_ref"]))


@dataclass(frozen=True, slots=True)
class ConflictClaim(_Value):
    claim_id: str
    payload: ContinuityClaimValue | IdentityObservationValue

    def __post_init__(self) -> None:
        _text(self.claim_id)
        if type(self.payload) not in (ContinuityClaimValue, IdentityObservationValue):
            raise DiagnosticModelError("conflict claim requires a closed typed payload")

    @property
    def claim_type(self) -> str:
        return (
            "continuity" if type(self.payload) is ContinuityClaimValue else "identity_observation"
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "claim_id": self.claim_id,
            "claim_type": self.claim_type,
            "payload": self.payload.to_mapping(),
        }

    @classmethod
    def from_mapping(cls, value: object) -> ConflictClaim:
        item = _closed(value, ("claim_id", "claim_type", "payload"))
        kind = _enum(item["claim_type"], ("continuity", "identity_observation"))
        payload = (
            ContinuityClaimValue.from_mapping(item["payload"])
            if kind == "continuity"
            else IdentityObservationValue.from_mapping(item["payload"])
        )
        return cls(_text(item["claim_id"]), payload)


@dataclass(frozen=True, slots=True)
class MergeProposalCause(_Value):
    cause_id: str
    merge_id: str
    rationale: str
    entity_refs: tuple[SemanticObjectRef, ...]
    evidence_refs: tuple[SemanticObjectRef, ...]

    def __post_init__(self) -> None:
        _hash(self.cause_id)
        _text(self.merge_id)
        _text(self.rationale)
        entities, evidence = _refs(self.entity_refs, minimum=2), _refs(self.evidence_refs)
        for ref in entities:
            _owned(ref, "vlm_semantic_pack", "vlm_entity")
        for ref in evidence:
            if ref.member_ref.artifact_type != "vlm_semantic_pack" or ref.object_type not in (
                "vlm_fact",
                "vlm_event",
            ):
                raise DiagnosticModelError("merge evidence must retain raw VLM Fact/Event owners")
        object.__setattr__(self, "entity_refs", entities)
        object.__setattr__(self, "evidence_refs", evidence)

    def to_mapping(self) -> dict[str, object]:
        return {
            "cause_id": self.cause_id,
            "merge_id": self.merge_id,
            "rationale": self.rationale,
            "entity_refs": [ref.to_mapping() for ref in self.entity_refs],
            "evidence_refs": [ref.to_mapping() for ref in self.evidence_refs],
        }

    @classmethod
    def from_mapping(cls, value: object) -> MergeProposalCause:
        item = _closed(value, ("cause_id", "merge_id", "rationale", "entity_refs", "evidence_refs"))
        return cls(
            _hash(item["cause_id"]),
            _text(item["merge_id"]),
            _text(item["rationale"]),
            _array(item["entity_refs"], _ref),
            _array(item["evidence_refs"], _ref),
        )


@dataclass(frozen=True, slots=True)
class ConfidenceMeasurement(_Value):
    observation_ref: SemanticObjectRef
    observation_kind: str
    value: str
    threshold: str
    policy_sha256: str

    def __post_init__(self) -> None:
        kind = _enum(self.observation_kind, ("entity", "fact", "event", "window_summary"))
        if kind == "window_summary":
            _owned(self.observation_ref, "whole_series_source_manifest", "source_window")
        else:
            _owned(self.observation_ref, "vlm_semantic_pack", f"vlm_{kind}")
        try:
            Confidence(self.value, "model")
            Confidence(self.threshold, "rule")
        except ValueError as error:
            raise DiagnosticModelError(
                "confidence requires canonical decimal values in [0, 1]"
            ) from error
        if Decimal(self.value) >= Decimal(self.threshold):
            raise DiagnosticModelError("low confidence measurement must be below its threshold")
        _hash(self.policy_sha256)

    def to_mapping(self) -> dict[str, object]:
        return {
            "observation_ref": self.observation_ref.to_mapping(),
            "observation_kind": self.observation_kind,
            "value": self.value,
            "threshold": self.threshold,
            "policy_sha256": self.policy_sha256,
        }

    @classmethod
    def from_mapping(cls, value: object) -> ConfidenceMeasurement:
        item = _closed(
            value, ("observation_ref", "observation_kind", "value", "threshold", "policy_sha256")
        )
        return cls(
            _ref(item["observation_ref"]),
            _text(item["observation_kind"]),
            _text(item["value"]),
            _text(item["threshold"]),
            _hash(item["policy_sha256"]),
        )


def _diagnostic_refs(value: EvidenceDiagnostic | ConflictDiagnostic) -> None:
    _text(value.diagnostic_id)
    _earlier(value.scope_ref)
    object.__setattr__(value, "evidence_refs", _refs(value.evidence_refs))
    object.__setattr__(value, "affected_refs", _refs(value.affected_refs))


@dataclass(frozen=True, slots=True)
class EvidenceDiagnostic(_Value):
    diagnostic_id: str
    reason_code: str
    scope_ref: SemanticObjectRef
    evidence_refs: tuple[SemanticObjectRef, ...]
    affected_refs: tuple[SemanticObjectRef, ...]
    measurement: ConfidenceMeasurement | None
    continuity_claim: ContinuityClaimValue | None
    summary: str | None

    def __post_init__(self) -> None:
        _diagnostic_refs(self)
        _enum(
            self.reason_code,
            (
                "low_confidence",
                "continuity_missing_context",
                "summary_evidence_missing",
                "unassigned",
            ),
        )
        if self.reason_code == "low_confidence":
            if (
                type(self.measurement) is not ConfidenceMeasurement
                or self.continuity_claim is not None
                or self.summary is not None
            ):  # noqa: E721
                raise DiagnosticModelError("low_confidence requires only its measurement detail")
            if self.measurement.observation_ref not in self.evidence_refs:
                raise DiagnosticModelError("measurement observation must be retained as evidence")
        elif self.reason_code == "continuity_missing_context":
            if (
                type(self.continuity_claim) is not ContinuityClaimValue
                or self.measurement is not None
                or self.summary is not None
            ):  # noqa: E721
                raise DiagnosticModelError("missing context requires only its continuity claim")
            if not self.continuity_claim.continues:
                raise DiagnosticModelError("missing context requires a true continuation claim")
            if self.continuity_claim.source_window_ref not in self.evidence_refs:
                raise DiagnosticModelError("continuity window must be retained as evidence")
        elif self.reason_code == "summary_evidence_missing":
            _text(self.summary)
            if self.measurement is not None or self.continuity_claim is not None:
                raise DiagnosticModelError("summary evidence gap cannot carry unrelated details")
            _owned(self.scope_ref, "whole_series_source_manifest", "source_window")
            if self.scope_ref not in self.evidence_refs:
                raise DiagnosticModelError("summary window must be retained as evidence")
        elif any(
            value is not None for value in (self.measurement, self.continuity_claim, self.summary)
        ):
            raise DiagnosticModelError("unassigned cannot carry unrelated detail fields")

    @property
    def kind(self) -> str:
        return "low_confidence" if self.reason_code == "low_confidence" else "insufficient_evidence"

    @property
    def rule_id(self) -> str:
        return "KC-COV-003" if self.reason_code == "unassigned" else "KC-GRAPH-002"

    @property
    def severity(self) -> str:
        return "error"

    def to_mapping(self) -> dict[str, object]:
        return {
            "diagnostic_id": self.diagnostic_id,
            "reason_code": self.reason_code,
            "kind": self.kind,
            "rule_id": self.rule_id,
            "severity": self.severity,
            "scope_ref": self.scope_ref.to_mapping(),
            "evidence_refs": [ref.to_mapping() for ref in self.evidence_refs],
            "affected_refs": [ref.to_mapping() for ref in self.affected_refs],
            "measurement": None if self.measurement is None else self.measurement.to_mapping(),
            "continuity_claim": None
            if self.continuity_claim is None
            else self.continuity_claim.to_mapping(),
            "summary": self.summary,
        }

    @classmethod
    def from_mapping(cls, value: object) -> EvidenceDiagnostic:
        item = _closed(
            value,
            (
                "diagnostic_id",
                "reason_code",
                "kind",
                "rule_id",
                "severity",
                "scope_ref",
                "evidence_refs",
                "affected_refs",
                "measurement",
                "continuity_claim",
                "summary",
            ),
        )
        result = cls(
            _text(item["diagnostic_id"]),
            _text(item["reason_code"]),
            _ref(item["scope_ref"]),
            _array(item["evidence_refs"], _ref),
            _array(item["affected_refs"], _ref),
            None
            if item["measurement"] is None
            else ConfidenceMeasurement.from_mapping(item["measurement"]),
            None
            if item["continuity_claim"] is None
            else ContinuityClaimValue.from_mapping(item["continuity_claim"]),
            None if item["summary"] is None else _text(item["summary"]),
        )
        for key in ("kind", "rule_id", "severity"):
            _fixed(item, key, getattr(result, key))
        return result


@dataclass(frozen=True, slots=True)
class ConflictDiagnostic(_Value):
    diagnostic_id: str
    kind: str
    scope_ref: SemanticObjectRef
    evidence_refs: tuple[SemanticObjectRef, ...]
    affected_refs: tuple[SemanticObjectRef, ...]
    cause_id: str
    competing_claim_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _diagnostic_refs(self)
        _enum(self.kind, ("timeline_order_conflict", "possible_duplicate"))
        _hash(self.cause_id)
        ids = _tuple(self.competing_claim_ids, str, minimum=2)
        for claim_id in ids:
            _text(claim_id)
        if len(set(ids)) != len(ids):
            raise DiagnosticModelError("competing claim IDs must be unique")
        object.__setattr__(self, "competing_claim_ids", tuple(sorted(ids)))

    @property
    def rule_id(self) -> str:
        return "KC-COV-004"

    @property
    def severity(self) -> str:
        return "error"

    def to_mapping(self) -> dict[str, object]:
        return {
            "diagnostic_id": self.diagnostic_id,
            "kind": self.kind,
            "rule_id": self.rule_id,
            "severity": self.severity,
            "scope_ref": self.scope_ref.to_mapping(),
            "evidence_refs": [ref.to_mapping() for ref in self.evidence_refs],
            "affected_refs": [ref.to_mapping() for ref in self.affected_refs],
            "cause_id": self.cause_id,
            "competing_claim_ids": list(self.competing_claim_ids),
        }

    @classmethod
    def from_mapping(cls, value: object) -> ConflictDiagnostic:
        item = _closed(
            value,
            (
                "diagnostic_id",
                "kind",
                "rule_id",
                "severity",
                "scope_ref",
                "evidence_refs",
                "affected_refs",
                "cause_id",
                "competing_claim_ids",
            ),
        )
        _fixed(item, "rule_id", "KC-COV-004")
        _fixed(item, "severity", "error")
        return cls(
            _text(item["diagnostic_id"]),
            _text(item["kind"]),
            _ref(item["scope_ref"]),
            _array(item["evidence_refs"], _ref),
            _array(item["affected_refs"], _ref),
            _hash(item["cause_id"]),
            _array(item["competing_claim_ids"], _text),
        )


def _bindings(input_hash: str, raw_hash: str, canonical_hash: str) -> None:
    for value in (input_hash, raw_hash, canonical_hash):
        _hash(value)


@dataclass(frozen=True, slots=True)
class EvidenceDiagnostics(_Value):
    evidence_diagnostics_id: str
    input_binding_sha256: str
    raw_draft_sha256: str
    canonical_draft_sha256: str
    items: tuple[EvidenceDiagnostic, ...]

    def __post_init__(self) -> None:
        _text(self.evidence_diagnostics_id)
        _bindings(self.input_binding_sha256, self.raw_draft_sha256, self.canonical_draft_sha256)
        object.__setattr__(
            self, "items", _records(self.items, EvidenceDiagnostic, lambda item: item.diagnostic_id)
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "evidence_diagnostics_id": self.evidence_diagnostics_id,
            "input_binding_sha256": self.input_binding_sha256,
            "raw_draft_sha256": self.raw_draft_sha256,
            "canonical_draft_sha256": self.canonical_draft_sha256,
            "items": [item.to_mapping() for item in self.items],
        }

    @classmethod
    def from_mapping(cls, value: object) -> EvidenceDiagnostics:
        item = _closed(
            value,
            (
                "evidence_diagnostics_id",
                "input_binding_sha256",
                "raw_draft_sha256",
                "canonical_draft_sha256",
                "items",
            ),
        )
        return cls(
            _text(item["evidence_diagnostics_id"]),
            _hash(item["input_binding_sha256"]),
            _hash(item["raw_draft_sha256"]),
            _hash(item["canonical_draft_sha256"]),
            _array(item["items"], EvidenceDiagnostic.from_mapping),
        )


@dataclass(frozen=True, slots=True)
class ConflictDiagnostics(_Value):
    conflict_diagnostics_id: str
    input_binding_sha256: str
    raw_draft_sha256: str
    canonical_draft_sha256: str
    items: tuple[ConflictDiagnostic, ...]
    claims: tuple[ConflictClaim, ...]
    merge_causes: tuple[MergeProposalCause, ...]

    def __post_init__(self) -> None:
        _text(self.conflict_diagnostics_id)
        _bindings(self.input_binding_sha256, self.raw_draft_sha256, self.canonical_draft_sha256)
        items = _records(self.items, ConflictDiagnostic, lambda item: item.diagnostic_id)
        claims = _records(self.claims, ConflictClaim, lambda item: item.claim_id)
        causes = _records(self.merge_causes, MergeProposalCause, lambda item: item.cause_id)
        if len({cause.merge_id for cause in causes}) != len(causes):
            raise DiagnosticModelError("merge proposal local IDs must be unique")
        claim_index = {claim.claim_id: claim for claim in claims}
        cause_index = {cause.cause_id: cause for cause in causes}
        used_claims: set[str] = set()
        used_causes: set[str] = set()
        for item in items:
            if not set(item.competing_claim_ids) <= claim_index.keys():
                raise DiagnosticModelError("conflict names an unknown local claim")
            payloads = tuple(claim_index[key].payload for key in item.competing_claim_ids)
            used_claims.update(item.competing_claim_ids)
            if item.kind == "possible_duplicate":
                if item.cause_id not in cause_index or any(
                    type(payload) is not IdentityObservationValue for payload in payloads
                ):
                    raise DiagnosticModelError(
                        "possible duplicate requires its merge cause and entity claims"
                    )
                observations = tuple(
                    cast(IdentityObservationValue, payload).observation_ref for payload in payloads
                )
                if len(set(observations)) != len(observations) or set(observations) != set(
                    cause_index[item.cause_id].entity_refs
                ):
                    raise DiagnosticModelError(
                        "merge claims must match every proposed entity exactly"
                    )
                used_causes.add(item.cause_id)
            else:
                if len(payloads) != 2 or any(
                    type(payload) is not ContinuityClaimValue for payload in payloads
                ):
                    raise DiagnosticModelError(
                        "timeline conflict requires exactly two continuity claims"
                    )
                first, second = cast(tuple[ContinuityClaimValue, ContinuityClaimValue], payloads)
                if (
                    first.source_window_ref == second.source_window_ref
                    or {first.direction, second.direction} != {"previous", "next"}
                    or first.continues == second.continues
                ):
                    raise DiagnosticModelError(
                        "timeline conflict must retain opposing window-side booleans"
                    )
        if used_claims != claim_index.keys() or used_causes != cause_index.keys():
            raise DiagnosticModelError(
                "diagnostic set contains unreferenced claims or merge causes"
            )
        object.__setattr__(self, "items", items)
        object.__setattr__(self, "claims", claims)
        object.__setattr__(self, "merge_causes", causes)

    def to_mapping(self) -> dict[str, object]:
        return {
            "conflict_diagnostics_id": self.conflict_diagnostics_id,
            "input_binding_sha256": self.input_binding_sha256,
            "raw_draft_sha256": self.raw_draft_sha256,
            "canonical_draft_sha256": self.canonical_draft_sha256,
            "items": [item.to_mapping() for item in self.items],
            "claims": [claim.to_mapping() for claim in self.claims],
            "merge_causes": [cause.to_mapping() for cause in self.merge_causes],
        }

    @classmethod
    def from_mapping(cls, value: object) -> ConflictDiagnostics:
        item = _closed(
            value,
            (
                "conflict_diagnostics_id",
                "input_binding_sha256",
                "raw_draft_sha256",
                "canonical_draft_sha256",
                "items",
                "claims",
                "merge_causes",
            ),
        )
        return cls(
            _text(item["conflict_diagnostics_id"]),
            _hash(item["input_binding_sha256"]),
            _hash(item["raw_draft_sha256"]),
            _hash(item["canonical_draft_sha256"]),
            _array(item["items"], ConflictDiagnostic.from_mapping),
            _array(item["claims"], ConflictClaim.from_mapping),
            _array(item["merge_causes"], MergeProposalCause.from_mapping),
        )
