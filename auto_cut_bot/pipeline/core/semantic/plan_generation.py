#!/usr/bin/env python3
"""Derive the active Story Plan generation from all planning inputs."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from autocut_core.io import json_sha256, load_json, sha256_file
from autocut_core.libs.editorial_plan import COMPILER_VERSION, PLANNING_CONTRACT_VERSION


def plan_generation_sha256(
    *,
    story_approval_sha256: str,
    story_evidence_index_sha256: str,
    span_candidate_index_sha256: str,
    preflight: dict[str, Any],
) -> str:
    """Return a non-self-referential fingerprint for one Plan generation."""

    normalized_preflight = deepcopy(preflight)
    normalized_preflight.pop("plan_generation_sha256", None)
    return json_sha256(
        {
            "compiler_version": COMPILER_VERSION,
            "planning_contract_version": PLANNING_CONTRACT_VERSION,
            "story_approval_sha256": story_approval_sha256,
            "story_evidence_index_sha256": (
                story_evidence_index_sha256
            ),
            "span_candidate_index_sha256": span_candidate_index_sha256,
            "preflight_contract_sha256": json_sha256(
                normalized_preflight
            ),
        }
    )


def current_plan_generation(job_root: Path) -> str:
    """Recompute the generation recorded by the current preflight artifact."""

    root = job_root.expanduser().resolve()
    approval_path = root / "story-approval.json"
    evidence_index_path = root / "story-evidence" / "index.json"
    span_index_path = root / "span-candidates" / "index.json"
    preflight_path = root / "story-plan-preflight.json"
    for path in (
        approval_path,
        evidence_index_path,
        span_index_path,
        preflight_path,
    ):
        if not path.is_file():
            raise FileNotFoundError(path)
    return plan_generation_sha256(
        story_approval_sha256=sha256_file(approval_path),
        story_evidence_index_sha256=sha256_file(evidence_index_path),
        span_candidate_index_sha256=sha256_file(span_index_path),
        preflight=load_json(preflight_path),
    )
