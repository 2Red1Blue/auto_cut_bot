#!/usr/bin/env python3
"""Shared policy for automatic rendering of Story QC ``review`` results.

Manual ``--include-review`` remains an explicit operator override.  Auto mode
is narrower: it may render only reviews whose complete non-info finding set is
already covered by the formal render contract.
"""

from __future__ import annotations

from typing import Any


AUTO_RENDERABLE_REVIEW_FINDING_CODES = frozenset(
    {
        "local-audio-fade-fallback-source_start",
        "local-audio-fade-fallback-source_end",
    }
)


def auto_review_render_decision(
    report: dict[str, Any],
) -> tuple[bool, str]:
    status = report.get("status")
    if status == "approved":
        return True, "approved"
    if status != "review":
        return False, f"status={status}"
    material_findings = [
        item
        for item in report.get("findings", [])
        if isinstance(item, dict)
        and item.get("severity") in {"review", "block"}
    ]
    if not material_findings:
        return False, "review has no typed render-safe finding"
    if any(item.get("severity") == "block" for item in material_findings):
        return False, "review contains a blocking finding"
    codes = {
        str(item.get("code") or "") for item in material_findings
    }
    unsafe_codes = sorted(
        code
        for code in codes
        if code not in AUTO_RENDERABLE_REVIEW_FINDING_CODES
    )
    if unsafe_codes:
        return False, f"review requires human decision: {unsafe_codes}"
    return True, f"render-safe review findings: {sorted(codes)}"
