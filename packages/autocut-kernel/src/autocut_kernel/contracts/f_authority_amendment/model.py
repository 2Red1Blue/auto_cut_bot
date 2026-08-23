"""Closed vocabulary for non-authoritative amendment proposal-shape records."""
# pyright: reportUnknownVariableType=none, reportUnknownArgumentType=none

from __future__ import annotations

import re
from typing import Any

from autocut_kernel.contracts.f_authority_intake.model import IntakeError

F0_PRODUCER = "9393dc4d49c93dce8fbd0d6fe1083adab8de310f"
F0_ATTESTATION = "b9491925f35a858b4e9797b9555b559602101390"
ERRATA_I1 = "e2318652cfaed0f99f1ba355de7982f1d34df150"
ERRATA_I2 = "baf667f797ac7d4eb34e48caad8047fb07433c9c"
PACKET_IDS = frozenset({"AC-B-001", "AC-B-002", "AC-B-003", "AC-B-004", "AC-B-007", "AC-B-008", "AC-B-009"})
CONTROLLED_SLOTS = {
    "AC-B-001": frozenset({"catalogue_envelope"}),
    "AC-B-002": frozenset({"catalogue_envelope"}),
    "AC-B-003": frozenset({"receipt_dispatcher"}),
    "AC-B-004": frozenset({"recovery_ledger"}),
    "AC-B-007": frozenset({"diagnostic_degradation"}),
    "AC-B-008": frozenset({"catalogue_envelope"}),
    "AC-B-009": frozenset({"command_identity"}),
}
FORBIDDEN_FIELD = re.compile(
    r"(?:business[_ -]?id|default|registry[_ -]?(?:row|entry)|generated[_ -]?(?:output|artifact)|"
    r"runtime[_ -]?(?:state|claim)|readiness[_ -]?(?:claim|state)|(?:selected|chosen|material)[_ -]?owner)",
    re.IGNORECASE,
)
FORBIDDEN_CLAIM = re.compile(
    r"(?:\b(?:business[_ -]?id|default(?:s|ed|ing)?)\b|\bregistry[_ -]?(?:row|entry)s?\b|"
    r"\bgenerated[_ -]?(?:output|artifact)s?\b|\bruntime[_ -]?(?:state|claim)s?\b|"
    r"\breadiness[_ -]?(?:claim|state)s?\b|\b(?:selected|chosen|material)[_ -]?owner\b)",
    re.IGNORECASE,
)


class AmendmentError(IntakeError):
    """Raised when a proposal-shape record overclaims authority or scope."""


def reject_proposal_forbidden(value: object, *, key: str = "") -> None:
    """Deny implementation vocabulary, including values hidden in nested records."""

    if isinstance(value, str) and FORBIDDEN_CLAIM.search(value):
        raise AmendmentError(f"forbidden proposal data claim in {key or 'value'}")
    if isinstance(value, dict):
        for child_key, child in value.items():
            if FORBIDDEN_FIELD.search(child_key):
                raise AmendmentError(f"forbidden proposal field {child_key}")
            reject_proposal_forbidden(child, key=child_key)
    elif isinstance(value, list):
        for child in value:
            reject_proposal_forbidden(child, key=key)


def require_nonempty_strings(value: Any, *, field: str) -> list[str]:
    if not isinstance(value, list) or not value or any(not isinstance(item, str) or not item for item in value):
        raise AmendmentError(f"{field} must be a non-empty list of non-empty strings")
    if len(set(value)) != len(value):
        raise AmendmentError(f"{field} must be duplicate-free")
    return value
