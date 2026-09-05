"""Bounded Stage 2 failure metadata; never a repair or admission decision."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from ..contracts.compiler.canonical import sha256_bytes
from ..semantic_chain.story_design_validation import StoryProposalValidationError


def story_design_failure_detail(
    error: ValueError, *, phase: Literal["compilation", "independent_evaluation"],
    raw: bytes, attempt_id: UUID,
) -> dict[str, object]:
    """Keep typed validation metadata across existing material-support wrappers.

    Exception messages may embed untrusted model content or private inputs. Do
    not copy them (or tracebacks) into durable diagnostics. Follow explicit
    causes only, with cycle/depth bounds; raw bytes remain in their audit Blob.
    """
    causes: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = error
    validation: dict[str, object] | None = None
    while current is not None and id(current) not in seen and len(causes) < 8:
        seen.add(id(current))
        causes.append(type(current).__name__)
        if type(current) is StoryProposalValidationError:
            validation = current.to_diagnostic()
            break
        current = current.__cause__
    diagnostic: dict[str, object] = {
        "schema_version": "stage2-failure-diagnostic-v1",
        "stage": "stage2_portfolio",
        "phase": phase,
        "attempt_id": str(attempt_id),
        "raw_response_sha256": sha256_bytes(raw),
        "cause_types": causes,
        "validation": validation,
        "retryability": "requires_diagnosis",
        "recommended_scope": "stage2_portfolio",
    }
    return {"reason": "Stage 2 draft or compilation rejected", "diagnostic": diagnostic}
