from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from autocut_kernel.pipeline import (
    FinalizeTimedMediaEvidenceBatchResult,
    MediaRecoveryEntry,
    MediaRecoveryFrontier,
    MediaRecoveryPlan,
)
from autocut_kernel.store import CommandOutcome, Job

from auto_cut_bot.pipeline.runtime import (
    MediaPreflightRecomputeRequest,
    PipelineCommand,
    PipelineRunRequest,
    PipelineStageContext,
)
from auto_cut_bot.pipeline.runtime.media_preflight_stage import MediaPreflightPipelineStage
from tests.authority.test_committed_timed_media_batch import _two_episode_batch_case
from tests.pipeline.runtime_profile_fixture import execution_profile, media_preflight_policy
from tests.pipeline.test_pipeline_vlm_stage import _bundle


@pytest.mark.asyncio
async def test_two_failed_episodes_accumulate_across_two_selected_successors(
    tmp_path, monkeypatch: pytest.MonkeyPatch,  # type: ignore[no-untyped-def]
) -> None:
    store, _request, resolver, batch = _two_episode_batch_case(tmp_path, monkeypatch)
    base_requests = tuple(child.request for child in batch.children)
    source_bundle, _blobs = _bundle(2)
    outcomes: dict[tuple[str, int], CommandOutcome] = {}
    frontier: MediaRecoveryFrontier | None = None
    finalized_children: list[tuple[str, int]] = []

    def result(job: Job, episode_index: int, state: str) -> CommandOutcome:
        value = CommandOutcome(
            uuid4(),
            state,  # type: ignore[arg-type]
            receipt_id=uuid4(),
            artifact_set_id=uuid4() if state == "succeeded" else None,
            job_id=uuid4(),
        )
        outcomes[(job.job_key, episode_index)] = value
        return value

    base_job = base_requests[0].job
    result(base_job, 0, "failed")
    result(base_job, 1, "failed")

    def read_outcome(job: Job, key: str) -> CommandOutcome | None:
        request = next(
            (
                candidate
                for candidate in base_requests
                if candidate.job == job and candidate.idempotency_key == key
            ),
            None,
        )
        if request is not None:
            return outcomes.get((job.job_key, request.episode_index))
        for episode_index in range(2):
            if (job.job_key, episode_index) in outcomes:
                return outcomes[(job.job_key, episode_index)]
        return None

    def claim(plan: MediaRecoveryPlan) -> MediaRecoveryFrontier:
        nonlocal frontier
        if frontier is None:
            frontier = MediaRecoveryFrontier(uuid4(), plan, "open", 0, ())
        assert frontier.plan == plan
        return frontier

    def merge(
        plan: MediaRecoveryPlan,
        participant: Job,
        entries: tuple[MediaRecoveryEntry, ...],
    ) -> MediaRecoveryFrontier:
        nonlocal frontier
        assert frontier is not None and frontier.plan == plan
        selected = {entry.episode_index: entry for entry in frontier.entries}
        for entry in entries:
            selected.setdefault(entry.episode_index, entry)
        ordered = tuple(selected[index] for index in sorted(selected))
        if len(ordered) == len(plan.requirement_sha256s):
            owner = frontier.finalizer_job or participant
            state = "complete"
        else:
            owner = None
            state = "open"
        frontier = MediaRecoveryFrontier(
            frontier.frontier_id,
            plan,
            state,
            frontier.version + 1,
            ordered,
            owner,
        )
        return frontier

    def mark(
        plan: MediaRecoveryPlan, owner: Job, outcome: CommandOutcome
    ) -> MediaRecoveryFrontier:
        nonlocal frontier
        assert frontier is not None and frontier.plan == plan
        assert frontier.finalizer_job == owner
        assert outcome.receipt_id is not None and outcome.artifact_set_id is not None
        frontier = MediaRecoveryFrontier(
            frontier.frontier_id,
            plan,
            "finalized",
            frontier.version + 1,
            frontier.entries,
            owner,
            outcome.receipt_id,
            outcome.artifact_set_id,
        )
        return frontier

    monkeypatch.setattr(store, "read_outcome", read_outcome)
    monkeypatch.setattr(store, "claim_media_recovery_frontier", claim, raising=False)
    monkeypatch.setattr(store, "merge_media_recovery_successes", merge, raising=False)
    monkeypatch.setattr(store, "mark_media_recovery_finalized", mark, raising=False)

    scripted: dict[tuple[str, int], str] = {}

    class FakePrepare:
        def __init__(self, *_args: object) -> None:
            pass

        def execute(self, request):  # type: ignore[no-untyped-def]
            state = scripted[(request.job.job_key, request.episode_index)]
            return SimpleNamespace(outcome=result(request.job, request.episode_index, state))

    class FakeFinalize:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def execute(self, request):  # type: ignore[no-untyped-def]
            finalized_children.extend(
                (child.request.job.job_key, child.request.episode_index)
                for child in request.children
            )
            return FinalizeTimedMediaEvidenceBatchResult(
                CommandOutcome(
                    uuid4(),
                    "succeeded",
                    receipt_id=uuid4(),
                    artifact_set_id=uuid4(),
                    job_id=uuid4(),
                )
            )

    monkeypatch.setattr(
        "auto_cut_bot.pipeline.runtime.media_preflight_stage.PrepareTimedMediaEvidenceCommand",
        FakePrepare,
    )
    monkeypatch.setattr(
        "auto_cut_bot.pipeline.runtime.media_preflight_stage.FinalizeTimedMediaEvidenceBatchCommand",
        FakeFinalize,
    )
    stage = MediaPreflightPipelineStage(store, object(), resolver)  # type: ignore[arg-type]
    profile = execution_profile(media_policy=media_preflight_policy())

    def context(run_id: str, selected: int) -> PipelineStageContext:
        return PipelineStageContext(
            run_id,
            PipelineRunRequest("test", source_reference="fixture"),
            PipelineCommand("media", "media_preflight", "running", lease_id="lease"),
            profile,
            MediaPreflightRecomputeRequest(
                "pipeline_run_" + "b" * 32, 1, (selected + 1,)
            ),
        )

    first_job = Job("pipeline_run_" + "c" * 32, base_job.profile)
    first_request = replace(
        base_requests[0],
        job=first_job,
        idempotency_key="media-preflight:first-recovery",
        artifact_scope=replace(base_requests[0].artifact_scope, key=first_job.job_key),
        input_job=base_job,
    )
    scripted[(first_job.job_key, 0)] = "succeeded"
    first = await stage._execute_batch(  # pyright: ignore[reportPrivateUsage]
        context(first_job.job_key, 0),
        source_bundle,
        (first_request,),
        media_preflight_policy(),
        aggregate_requests=base_requests,
    )

    assert first.outcome.state == "succeeded"
    assert frontier is not None and frontier.state == "open"
    assert tuple(entry.episode_index for entry in frontier.entries) == (0,)
    assert finalized_children == []

    second_job = Job("pipeline_run_" + "d" * 32, base_job.profile)
    second_request = replace(
        base_requests[1],
        job=second_job,
        idempotency_key="media-preflight:second-recovery",
        artifact_scope=replace(base_requests[1].artifact_scope, key=second_job.job_key),
        input_job=base_job,
    )
    scripted[(second_job.job_key, 1)] = "succeeded"
    second = await stage._execute_batch(  # pyright: ignore[reportPrivateUsage]
        context(second_job.job_key, 1),
        source_bundle,
        (second_request,),
        media_preflight_policy(),
        aggregate_requests=base_requests,
    )

    assert second.outcome.state == "succeeded"
    assert frontier is not None and frontier.state == "finalized"
    assert finalized_children == [(first_job.job_key, 0), (second_job.job_key, 1)]
