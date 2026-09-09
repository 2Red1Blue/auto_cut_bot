"""V12 durable command-chain scheduling stays isolated from prior profiles."""

from __future__ import annotations

import re

from auto_cut_bot.pipeline.runtime import (
    PipelineCommand,
    PipelineExecutionProfile,
    PipelineRunRequest,
    PipelineRunSnapshot,
    PostgresPipelineRunStore,
    postgres,
)

V12_STAGES = (
    "source_prep",
    "context_prepare",
    "vlm",
    "stage1_narrative",
    "stage2_portfolio",
    "stage3_blueprint",
    "media_preflight",
    "stage4_recipe",
)


class _RecordingCursor:
    def __init__(self, inserted_run_id: str) -> None:
        self._inserted_run_id = inserted_run_id
        self._returned_insert = False
        self.statements: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, query: str, params: tuple[object, ...] = ()) -> None:
        self.statements.append((query, params))

    def fetchone(self) -> tuple[object, ...] | None:
        if not self._returned_insert:
            self._returned_insert = True
            return (self._inserted_run_id,)
        return None


def test_semantic_story_media_claim_schedules_eight_durable_commands(monkeypatch) -> None:
    """The new profile predicate alone selects the v12 chain in the run store."""
    run_id = "pipeline_run_" + "2" * 32
    request = PipelineRunRequest("test", source_reference="source:v12-chain")
    profile = PipelineExecutionProfile.legacy_unresolved()
    cursor = _RecordingCursor(run_id)
    snapshot = PipelineRunSnapshot(
        run_id,
        request,
        request.request_hash,
        "accepted",
        tuple(
            PipelineCommand(f"command-{index}", stage, "pending")
            for index, stage in enumerate(V12_STAGES)
        ),
        0,
        profile,
    )
    store = PostgresPipelineRunStore(lambda: None)  # type: ignore[arg-type]
    monkeypatch.setattr(
        PipelineExecutionProfile,
        "is_semantic_story_media",
        property(lambda _profile: True),
        raising=False,
    )
    monkeypatch.setattr(PipelineExecutionProfile, "to_doubao_policy", lambda _profile: None)
    monkeypatch.setattr(
        PipelineExecutionProfile,
        "to_generation_retry_policy",
        lambda _profile: None,
    )
    monkeypatch.setattr(
        PipelineExecutionProfile,
        "build_stage1_command_policy",
        lambda _profile: None,
    )
    monkeypatch.setattr(
        PipelineExecutionProfile,
        "build_stage2_command_policy",
        lambda _profile: None,
    )
    monkeypatch.setattr(
        PipelineExecutionProfile,
        "build_stage3_command_policy",
        lambda _profile: None,
    )
    monkeypatch.setattr(store, "_transaction", lambda operation: operation(cursor))
    monkeypatch.setattr(store, "_read_snapshot", lambda _cursor, _run_id: snapshot)

    claim = store._claim_run_sync(
        run_id,
        "v12-chain-idempotency",
        request,
        request.request_hash,
        profile,
    )

    assert claim.snapshot == snapshot
    command_insert = next(
        (query, params)
        for query, params in cursor.statements
        if "INSERT INTO runtime.pipeline_commands" in query
    )
    command_query, command_params = command_insert
    assert tuple(re.findall(r"\d, '([^']+)', 'pending'", command_query)) == V12_STAGES
    assert tuple(command_params[1::2]) == (run_id,) * len(V12_STAGES)


def test_v12_run_cannot_succeed_before_stage4_recipe() -> None:
    all_succeeded = [(stage, "succeeded") for stage in V12_STAGES]

    assert postgres._terminal_run_state(all_succeeded) == "succeeded"
    assert (
        postgres._terminal_run_state(all_succeeded[:-1] + [("stage4_recipe", "pending")])
        == "running"
    )
    assert (
        postgres._terminal_run_state(all_succeeded[:-1] + [("stage4_recipe", "failed")]) == "failed"
    )
