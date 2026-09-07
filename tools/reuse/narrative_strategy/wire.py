"""Candidate wire decoder: strict decode of narrative-annotated Stage 2 drafts.

Strategy: the candidate wire is the frozen compact wire plus an optional
narrative_duties array per proposal. We strip the duties, rewrite the schema
version tag, and hand the payload to the kernel's own strict decoder — the
baseline decode rules (closed fields, references, obligations, material
constraints) are reused unchanged, never reimplemented. The stripped duties are
then validated against the same compact context.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from autocut_kernel.semantic_chain.narrative_models import EventAttributes
from autocut_kernel.semantic_chain.story_design_compact import (
    COMPACT_WIRE_SCHEMA_VERSION,
    ProposalDraftSetV2,
    decode_story_design_compact,
)
from autocut_kernel.semantic_chain.story_design_compact_context import StoryDesignCompactContext
from autocut_kernel.semantic_chain.story_design_draft import StoryDesignDraftPolicy

from tools.reuse.narrative_strategy.policy import (
    NARRATIVE_DUTY_KINDS,
    NARRATIVE_WIRE_SCHEMA_VERSION,
    is_paired_kind,
)


class NarrativeDutyError(Exception):
    """Closed diagnostic for duty-annotation failures.

    Plain Exception subclass (not a frozen dataclass): exception machinery
    must be able to set __traceback__."""

    def __init__(self, code: str, message: str, proposal_index: int | None = None,
                 json_path: str | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.proposal_index = proposal_index
        self.json_path = json_path

    def to_diagnostic(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "proposal_index": self.proposal_index,
            "json_path": self.json_path,
        }


DUTY_UNKNOWN_KIND = "NARRATIVE_DUTY_UNKNOWN_KIND"
DUTY_FIELD_INVALID = "NARRATIVE_DUTY_FIELD_INVALID"
DUTY_TARGET_MISMATCH = "NARRATIVE_DUTY_TARGET_MISMATCH"
DUTY_REFERENCE_NOT_FOUND = "NARRATIVE_DUTY_REFERENCE_NOT_FOUND"
DUTY_REFERENCE_TYPE_MISMATCH = "NARRATIVE_DUTY_REFERENCE_TYPE_MISMATCH"
DUTY_SETUP_PAYOFF_IDENTICAL = "NARRATIVE_DUTY_SETUP_PAYOFF_IDENTICAL"
DUTY_ORDER_REVERSED = "NARRATIVE_DUTY_ORDER_REVERSED"
DUTY_DUPLICATE = "NARRATIVE_DUTY_DUPLICATE"


@dataclass(frozen=True)
class NarrativeDuty:
    duty_kind: str
    target_ref: str
    reason: str
    setup_ref: str | None
    payoff_ref: str | None


@dataclass(frozen=True)
class NarrativeDraft:
    """Kernel-decoded base draft plus validated duty annotations."""

    base: ProposalDraftSetV2
    duties: tuple[tuple[str, tuple[NarrativeDuty, ...]], ...]  # (proposal_id, duties)
    order_notes: tuple[str, ...]  # indeterminate setup/payoff order notes

    @property
    def duty_count(self) -> int:
        return sum(len(duties) for _, duties in self.duties)


def _alias_index(context: StoryDesignCompactContext) -> dict[str, str]:
    return {alias: ref.object_type for alias, ref in context.aliases}


def _episode_ordinal(context: StoryDesignCompactContext, episode_id: str) -> int | None:
    """Episode ordering comes from the graph's episode attributes, not filenames."""
    for node in context.graph.nodes:
        attrs = node.attributes
        episode_ordinal = getattr(attrs, "ordinal", None)
        if node.node_type == "episode" and getattr(attrs, "episode_id", None) == episode_id:
            if isinstance(episode_ordinal, int):
                return episode_ordinal
    return None


def _validate_duties(
    duties_by_proposal: dict[int, list[dict[str, Any]]],
    context: StoryDesignCompactContext,
    proposal_count: int,
) -> tuple[dict[int, tuple[NarrativeDuty, ...]], tuple[str, ...]]:
    alias_types = _alias_index(context)
    seen: set[tuple[int, str, str]] = set()
    validated: dict[int, tuple[NarrativeDuty, ...]] = {}
    notes: list[str] = []
    for index in sorted(duties_by_proposal):
        if not 0 <= index < proposal_count:
            raise NarrativeDutyError(
                DUTY_TARGET_MISMATCH,
                f"duties attached to unknown proposal index {index}",
                proposal_index=index,
            )
        out: list[NarrativeDuty] = []
        for position, raw in enumerate(duties_by_proposal[index]):
            path = f"proposals[{index}].narrative_duties[{position}]"
            if not isinstance(raw, dict):
                raise NarrativeDutyError(DUTY_FIELD_INVALID, "duty must be an object",
                                         proposal_index=index, json_path=path)
            duty_kind = raw.get("duty_kind")
            if duty_kind not in NARRATIVE_DUTY_KINDS:
                raise NarrativeDutyError(DUTY_UNKNOWN_KIND, f"unknown duty_kind {duty_kind!r}",
                                         proposal_index=index, json_path=f"{path}.duty_kind")
            target_ref = raw.get("target_ref")
            expected_target = f"proposal-{index}"
            if not isinstance(target_ref, str) or target_ref != expected_target:
                raise NarrativeDutyError(
                    DUTY_TARGET_MISMATCH,
                    f"target_ref must be {expected_target!r}, got {target_ref!r}",
                    proposal_index=index, json_path=f"{path}.target_ref",
                )
            reason = raw.get("reason")
            if not isinstance(reason, str) or not reason:
                raise NarrativeDutyError(DUTY_FIELD_INVALID, "reason required",
                                         proposal_index=index, json_path=f"{path}.reason")
            setup_ref = raw.get("setup_ref")
            payoff_ref = raw.get("payoff_ref")
            for name in ("setup_ref", "payoff_ref"):
                value = raw.get(name)
                if value is not None and (not isinstance(value, str) or not value):
                    raise NarrativeDutyError(DUTY_FIELD_INVALID, f"{name} must be a non-empty string",
                                             proposal_index=index, json_path=f"{path}.{name}")
            if is_paired_kind(duty_kind) and (setup_ref is None or payoff_ref is None):
                raise NarrativeDutyError(
                    DUTY_FIELD_INVALID,
                    f"duty_kind {duty_kind!r} requires setup_ref and payoff_ref",
                    proposal_index=index, json_path=path,
                )
            for name in ("setup_ref", "payoff_ref"):
                value = raw.get(name)
                if value is None:
                    continue
                obj_type = alias_types.get(value)
                if obj_type is None:
                    raise NarrativeDutyError(DUTY_REFERENCE_NOT_FOUND,
                                             f"{name} {value!r} not in compact context",
                                             proposal_index=index, json_path=f"{path}.{name}")
                if obj_type not in ("event", "fact"):
                    raise NarrativeDutyError(
                        DUTY_REFERENCE_TYPE_MISMATCH,
                        f"{name} {value!r} resolves to {obj_type!r}, expected event/fact",
                        proposal_index=index, json_path=f"{path}.{name}",
                    )
            if setup_ref is not None and payoff_ref is not None and setup_ref == payoff_ref:
                raise NarrativeDutyError(
                    DUTY_SETUP_PAYOFF_IDENTICAL, "setup_ref and payoff_ref must differ",
                    proposal_index=index, json_path=path,
                )
            key = (index, duty_kind, target_ref)
            if key in seen:
                raise NarrativeDutyError(DUTY_DUPLICATE,
                                         f"duplicate duty {duty_kind!r} on {target_ref!r}",
                                         proposal_index=index, json_path=path)
            seen.add(key)
            if setup_ref is not None and payoff_ref is not None:
                note = _check_order(context, alias_types, setup_ref, payoff_ref, index, path)
                if note:
                    notes.append(note)
            out.append(NarrativeDuty(
                duty_kind=duty_kind, target_ref=target_ref, reason=reason,
                setup_ref=setup_ref, payoff_ref=payoff_ref,
            ))
        validated[index] = tuple(out)
    return validated, tuple(notes)


def _check_order(
    context: StoryDesignCompactContext,
    alias_types: dict[str, str],
    setup_ref: str,
    payoff_ref: str,
    proposal_index: int,
    path: str,
) -> str | None:
    """Prove setup-before-payoff when both are events with resolvable episodes.

    Ordering is only claimed from graph episode ordinals; same-episode or
    fact-based pairs are recorded as indeterminate, never silently accepted."""
    if alias_types.get(setup_ref) != "event" or alias_types.get(payoff_ref) != "event":
        return f"order indeterminate at {path}: non-event reference pair"
    setup = context.resolve(setup_ref, "e")
    payoff = context.resolve(payoff_ref, "e")
    setup_node = _event_node(context, setup)
    payoff_node = _event_node(context, payoff)
    if setup_node is None or payoff_node is None:
        return f"order indeterminate at {path}: event node missing"
    setup_attrs, payoff_attrs = setup_node.attributes, payoff_node.attributes
    assert isinstance(setup_attrs, EventAttributes) and isinstance(payoff_attrs, EventAttributes)
    if setup_attrs.episode_id == payoff_attrs.episode_id:
        return f"order indeterminate at {path}: same episode {setup_attrs.episode_id!r}"
    setup_ord = _episode_ordinal(context, setup_attrs.episode_id)
    payoff_ord = _episode_ordinal(context, payoff_attrs.episode_id)
    if setup_ord is None or payoff_ord is None:
        return f"order indeterminate at {path}: episode ordinal missing"
    if payoff_ord < setup_ord:
        raise NarrativeDutyError(
            DUTY_ORDER_REVERSED,
            f"payoff episode {payoff_attrs.episode_id!r} precedes setup episode {setup_attrs.episode_id!r}",
            proposal_index=proposal_index, json_path=path,
        )
    return None


def _event_node(context: StoryDesignCompactContext, ref: Any):
    for node in context.graph.nodes:
        if node.node_type == "event" and node.node_id == ref.object_id:
            return node
    return None


def decode_narrative_story_draft(
    raw: str | bytes,
    *,
    context: StoryDesignCompactContext,
    policy: StoryDesignDraftPolicy,
) -> NarrativeDraft:
    """Strict decode of a candidate-variant draft.

    Raises CompactDraftError (from the kernel decoder) or NarrativeDutyError."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise NarrativeDutyError(DUTY_FIELD_INVALID, "draft root must be an object")
    if payload.get("schema_version") != NARRATIVE_WIRE_SCHEMA_VERSION:
        raise NarrativeDutyError(
            DUTY_FIELD_INVALID,
            f"schema_version must be {NARRATIVE_WIRE_SCHEMA_VERSION}",
        )
    proposals = payload.get("proposals")
    if not isinstance(proposals, list):
        raise NarrativeDutyError(DUTY_FIELD_INVALID, "proposals must be an array")
    duties_by_proposal: dict[int, list[dict[str, Any]]] = {}
    stripped: list[dict[str, Any]] = []
    for index, proposal in enumerate(proposals):
        if not isinstance(proposal, dict):
            raise NarrativeDutyError(DUTY_FIELD_INVALID, f"proposals[{index}] must be an object",
                                     proposal_index=index)
        duties = proposal.get("narrative_duties", [])
        if not isinstance(duties, list):
            raise NarrativeDutyError(DUTY_FIELD_INVALID, f"proposals[{index}].narrative_duties must be an array",
                                     proposal_index=index, json_path=f"proposals[{index}].narrative_duties")
        if duties:
            duties_by_proposal[index] = duties
        stripped_proposal = {k: v for k, v in proposal.items() if k != "narrative_duties"}
        stripped.append(stripped_proposal)
    # Unknown extra keys (anything beyond narrative_duties) are still rejected:
    # the kernel decoder enforces closed proposal fields on the stripped payload.
    payload["schema_version"] = COMPACT_WIRE_SCHEMA_VERSION
    payload["proposals"] = stripped
    # the kernel decoder requires canonical UTF-8 bytes
    stripped_raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    base = decode_story_design_compact(stripped_raw, context=context, policy=policy)
    validated, notes = _validate_duties(duties_by_proposal, context, len(base.proposals))
    duties_out = tuple(
        (proposal.proposal_id, validated.get(index, ()))
        for index, proposal in enumerate(base.proposals)
    )
    return NarrativeDraft(base=base, duties=duties_out, order_notes=notes)
