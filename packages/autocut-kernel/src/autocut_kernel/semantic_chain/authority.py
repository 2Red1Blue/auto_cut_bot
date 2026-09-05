"""Private decoder authority for the pure Stage 1 compiler."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..media.types import canonical_sha256, sha256_prefixed
from ..store import CommittedSemanticInputs
from ..store.models import PersistedReprocessedVlmChild
from .derived_input_binding import bind_derived_input

_Status = Literal["resolved", "tainted", "unresolved", "conflicted"]
_OBLIGATION_IDS = ("cross_window_merge", "semantic_closure")


class Stage1AuthorityError(ValueError):
    """A Stage 1 authority boundary is not exact, closed, or input-bound."""


def require_sha256(value: object, label: str) -> str:
    try:
        return sha256_prefixed(value, label)
    except ValueError as error:
        raise Stage1AuthorityError(f"{label} must be a lowercase sha256 digest") from error


def _closed_status(value: object, label: str) -> _Status:
    if value not in {"resolved", "tainted", "unresolved", "conflicted"}:
        raise Stage1AuthorityError(f"{label} is unsupported")
    return value  # type: ignore[return-value]


def _text(value: object, label: str) -> str:
    if type(value) is not str or not value.strip():  # noqa: E721
        raise Stage1AuthorityError(f"{label} must be non-empty text")
    return value


def require_committed_semantic_inputs(value: object) -> CommittedSemanticInputs:
    if type(value) is not CommittedSemanticInputs:  # noqa: E721
        raise Stage1AuthorityError("Stage 1 requires exact CommittedSemanticInputs")
    return value


def _committed_input_binding(inputs: CommittedSemanticInputs) -> str:
    """Bind every durable source/window/VLM identity consumed by Stage 1."""

    members: list[dict[str, object]] = []
    for item in inputs.inputs:
        source = item.source_window
        pack = item.semantic_pack
        child = pack.source_child
        derived = type(child) is PersistedReprocessedVlmChild  # noqa: E721
        if derived:
            try:
                bind_derived_input(item, inputs)
            except ValueError as error:
                raise Stage1AuthorityError("committed derived VLM provenance is not closed") from error
        if (
            source.window_manifest_sha256 != item.request_identity.window_manifest_sha256
            or source.window_manifest_sha256 != pack.semantic_pack.window_manifest_sha256
            or source.window_manifest_sha256 != child.window_manifest_sha256
            or pack.semantic_pack.request_identity_sha256 != child.request_identity_sha256
            or item.request_identity.canonical_hash != child.request_identity_sha256
            or (not derived and item.response_record.content_hash != pack.semantic_pack.raw_response_sha256)
        ):
            raise Stage1AuthorityError("committed VLM input provenance is not closed")
        members.append(
            {
                "request_identity_sha256": item.request_identity.canonical_hash,
                "response_record": item.response_record.to_mapping(),
                "semantic_pack": {
                    "content_hash": pack.reference.content_hash,
                    "logical_id": pack.reference.logical_id,
                    "revision": pack.reference.revision,
                    "semantic_pack_sha256": pack.semantic_pack.canonical_hash,
                },
                "source_window": {
                    "episode_index": source.episode_index,
                    "source_sha256": source.source_sha256,
                    "window_manifest_set_sha256": source.window_manifest_set_sha256,
                    "window_manifest_sha256": source.window_manifest_sha256,
                },
            }
        )
    return canonical_sha256(
        {
            "source_grant_sha256": inputs.source_grant.canonical_hash,
            "source_manifest_provenance_sha256": inputs.source_manifest.canonical_hash,
            "vlm_aggregate_policy": inputs.vlm_aggregate_policy.to_mapping(),
            "vlm_semantic_pack_set": inputs.vlm_semantic_pack_set.to_mapping(),
            "vlm_members": members,
        }
    )


@dataclass(frozen=True, slots=True)
class _LedgerState:
    subject_id: str
    status: _Status

    def __post_init__(self) -> None:
        _text(self.subject_id, "ledger subject")
        _closed_status(self.status, "ledger status")


@dataclass(frozen=True, slots=True)
class _TaintSeed:
    subject_id: str
    seed_sha256: str

    def __post_init__(self) -> None:
        _text(self.subject_id, "taint subject")
        require_sha256(self.seed_sha256, "taint seed")


@dataclass(frozen=True, slots=True)
class _ConflictClaim:
    subject_id: str
    competing_claim_sha256: str

    def __post_init__(self) -> None:
        _text(self.subject_id, "conflict subject")
        require_sha256(self.competing_claim_sha256, "competing conflict claim")


@dataclass(frozen=True, slots=True, init=False)
class _DecodedStage1Authority:
    audit_record_sha256: str
    input_binding_sha256: str
    policy_id: str
    policy_sha256: str
    window_states: tuple[_LedgerState, ...]
    obligation_states: tuple[_LedgerState, ...]
    taint_seeds: tuple[_TaintSeed, ...]
    conflict_claims: tuple[_ConflictClaim, ...]

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("Stage 1 authority is created only by a persisted decoder")

    @classmethod
    def _from_decoder(
        cls,
        *,
        inputs: CommittedSemanticInputs,
        audit_record_sha256: str,
        policy_id: str,
        policy_sha256: str,
        window_states: tuple[_LedgerState, ...],
        obligation_states: tuple[_LedgerState, ...],
        taint_seeds: tuple[_TaintSeed, ...],
        conflict_claims: tuple[_ConflictClaim, ...],
    ) -> _DecodedStage1Authority:
        require_sha256(audit_record_sha256, "audit_record_sha256")
        _text(policy_id, "policy_id")
        require_sha256(policy_sha256, "policy_sha256")
        windows = tuple(sorted(item.source_window.window_manifest_sha256 for item in inputs.inputs))
        states = tuple(window_states)
        obligations = tuple(obligation_states)
        if tuple(item.subject_id for item in states) != windows:
            raise Stage1AuthorityError("decoded window states must exactly cover committed windows")
        if tuple(item.subject_id for item in obligations) != _OBLIGATION_IDS:
            raise Stage1AuthorityError("decoded obligation states must exactly cover compiler obligations")
        for values, expected_type, label in (
            (states, _LedgerState, "window states"),
            (obligations, _LedgerState, "obligation states"),
            (tuple(taint_seeds), _TaintSeed, "taint seeds"),
            (tuple(conflict_claims), _ConflictClaim, "conflict claims"),
        ):
            if any(type(value) is not expected_type for value in values):  # noqa: E721
                raise Stage1AuthorityError(f"decoded {label} are invalid")
        result = object.__new__(cls)
        object.__setattr__(result, "audit_record_sha256", audit_record_sha256)
        object.__setattr__(result, "input_binding_sha256", _committed_input_binding(inputs))
        object.__setattr__(result, "policy_id", policy_id)
        object.__setattr__(result, "policy_sha256", policy_sha256)
        object.__setattr__(result, "window_states", states)
        object.__setattr__(result, "obligation_states", obligations)
        object.__setattr__(result, "taint_seeds", tuple(sorted(taint_seeds, key=lambda item: (item.subject_id, item.seed_sha256))))
        object.__setattr__(result, "conflict_claims", tuple(sorted(conflict_claims, key=lambda item: (item.subject_id, item.competing_claim_sha256))))
        return result

    def _assert_bound(self, inputs: CommittedSemanticInputs) -> None:
        if self.input_binding_sha256 != _committed_input_binding(inputs):
            raise Stage1AuthorityError("decoded Stage 1 authority is not bound to committed inputs")

    def _to_mapping(self) -> dict[str, object]:
        return {
            "audit_record_sha256": self.audit_record_sha256,
            "conflict_claims": [{"competing_claim_sha256": item.competing_claim_sha256, "subject_id": item.subject_id} for item in self.conflict_claims],
            "input_binding_sha256": self.input_binding_sha256,
            "obligation_states": [{"status": item.status, "subject_id": item.subject_id} for item in self.obligation_states],
            "policy_id": self.policy_id,
            "policy_sha256": self.policy_sha256,
            "taint_seeds": [{"seed_sha256": item.seed_sha256, "subject_id": item.subject_id} for item in self.taint_seeds],
            "window_states": [{"status": item.status, "subject_id": item.subject_id} for item in self.window_states],
        }


def _decode_stage1_authority_for_test(  # pyright: ignore[reportUnusedFunction]
    inputs: CommittedSemanticInputs,
    *,
    audit_record_sha256: str,
    policy_id: str,
    policy_sha256: str,
    window_statuses: tuple[_Status, ...],
    obligation_statuses: tuple[_Status, _Status],
    taint_seeds: tuple[tuple[str, str], ...] = (),
    conflict_claims: tuple[tuple[str, str], ...] = (),
) -> _DecodedStage1Authority:
    """Private test seam; a future command replaces it with a persisted decoder."""

    committed = require_committed_semantic_inputs(inputs)
    windows = tuple(sorted(item.source_window.window_manifest_sha256 for item in committed.inputs))
    return _DecodedStage1Authority._from_decoder(  # pyright: ignore[reportPrivateUsage]
        inputs=committed,
        audit_record_sha256=audit_record_sha256,
        policy_id=policy_id,
        policy_sha256=policy_sha256,
        window_states=tuple(_LedgerState(subject_id, status) for subject_id, status in zip(windows, window_statuses, strict=True)),
        obligation_states=tuple(_LedgerState(subject_id, status) for subject_id, status in zip(_OBLIGATION_IDS, obligation_statuses, strict=True)),
        taint_seeds=tuple(_TaintSeed(*item) for item in taint_seeds),
        conflict_claims=tuple(_ConflictClaim(*item) for item in conflict_claims),
    )
