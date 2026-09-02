from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest
from autocut_kernel.pipeline import (
    MediaRecoveryEntry,
    MediaRecoveryFrontier,
    MediaRecoveryFrontierError,
    MediaRecoveryPlan,
)
from autocut_kernel.store import Job


def _sha(character: str) -> str:
    return "sha256:" + character * 64


def _plan() -> MediaRecoveryPlan:
    return MediaRecoveryPlan(
        Job("pipeline_run_" + "a" * 32, "production"),
        _sha("1"),
        _sha("2"),
        _sha("3"),
        _sha("4"),
        "local_cpu",
        _sha("5"),
        (_sha("6"), _sha("7")),
    )


def _entry(index: int, character: str = "8") -> MediaRecoveryEntry:
    plan = _plan()
    return MediaRecoveryEntry(
        index,
        plan.requirement_sha256s[index],
        Job(f"pipeline_run_{character * 32}", "production"),
        f"media-preflight:{index}:{character}",
        _sha(character),
        1,
        uuid4(),
        uuid4(),
        uuid4(),
    )


def test_media_recovery_plan_is_closed_and_round_trips() -> None:
    plan = _plan()

    assert MediaRecoveryPlan.from_mapping(plan.to_mapping()) == plan
    assert MediaRecoveryPlan.from_mapping(plan.to_mapping()).plan_sha256 == plan.plan_sha256

    with pytest.raises(MediaRecoveryFrontierError, match="closed schema"):
        MediaRecoveryPlan.from_mapping({**plan.to_mapping(), "latest": True})


def test_frontier_only_closes_with_exact_complete_census() -> None:
    plan = _plan()
    first = _entry(0)
    owner = Job("pipeline_run_" + "f" * 32, "production")

    MediaRecoveryFrontier(uuid4(), plan, "open", 1, (first,))

    with pytest.raises(MediaRecoveryFrontierError, match="complete coverage"):
        MediaRecoveryFrontier(uuid4(), plan, "complete", 2, (first,), owner)

    closed = MediaRecoveryFrontier(
        uuid4(), plan, "complete", 2, (first, _entry(1, "9")), owner
    )
    assert closed.state == "complete"


def test_frontier_rejects_reordered_or_wrong_requirement_entries() -> None:
    plan = _plan()
    first = _entry(0)
    second = _entry(1, "9")

    with pytest.raises(MediaRecoveryFrontierError, match="unique and ordered"):
        MediaRecoveryFrontier(uuid4(), plan, "open", 1, (second, first))

    with pytest.raises(MediaRecoveryFrontierError, match="plan slot"):
        MediaRecoveryFrontier(
            uuid4(),
            plan,
            "open",
            1,
            (replace(first, requirement_sha256=_sha("a")),),
        )


def test_cpu_and_cuda_plans_never_share_a_frontier_identity() -> None:
    cpu = _plan()
    cuda = replace(cpu, producer_kind="pc_cuda")
    changed_timing = replace(cuda, producer_compatibility_sha256=_sha("b"))

    assert cpu.plan_sha256 != cuda.plan_sha256
    assert cuda.plan_sha256 != changed_timing.plan_sha256
