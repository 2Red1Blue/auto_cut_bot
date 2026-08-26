"""Closed succeeded predecessor identity transport, not proof of commitment."""

from __future__ import annotations

from uuid import UUID

from ..semantic_chain.stage1_command_policy import require_closed_mapping
from ..store.models import CommandOutcome


def succeeded_outcome_mapping(outcome: CommandOutcome) -> dict[str, object]:
    if (type(outcome) is not CommandOutcome or type(outcome.state) is not str  # noqa: E721
            or outcome.state != "succeeded" or type(outcome.is_fresh_claim) is not bool  # noqa: E721
            or outcome.failure_code is not None or outcome.failure_detail_json is not None
            or any(type(value) is not UUID for value in (
                outcome.job_id, outcome.command_slot_id, outcome.receipt_id, outcome.artifact_set_id,
            ))):
        raise ValueError("predecessor requires an exact succeeded Job/slot/Receipt/Set outcome")
    # Freshness is transport history. Dataclass equality excludes job_id, so
    # consumers must compare this complete identity, never outcome equality.
    return {"job_id": str(outcome.job_id), "command_slot_id": str(outcome.command_slot_id),
            "state": outcome.state, "receipt_id": str(outcome.receipt_id),
            "artifact_set_id": str(outcome.artifact_set_id)}


def _uuid(value: object) -> UUID:
    if type(value) is not str:  # noqa: E721
        raise ValueError("predecessor identity must use canonical UUID strings")
    result = UUID(value)
    if str(result) != value:
        raise ValueError("predecessor UUID spelling is not canonical")
    return result


def succeeded_outcome_from_mapping(value: object) -> CommandOutcome:
    item = require_closed_mapping(
        value, {"job_id", "command_slot_id", "state", "receipt_id", "artifact_set_id"},
        "succeeded predecessor outcome",
    )
    if type(item["state"]) is not str or item["state"] != "succeeded":  # noqa: E721
        raise ValueError("predecessor outcome must be succeeded")
    return CommandOutcome(
        _uuid(item["command_slot_id"]), "succeeded", receipt_id=_uuid(item["receipt_id"]),
        artifact_set_id=_uuid(item["artifact_set_id"]), job_id=_uuid(item["job_id"]),
    )
