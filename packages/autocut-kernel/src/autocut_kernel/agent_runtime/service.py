"""Linear, fail-closed application orchestration over accepted local ports."""

from __future__ import annotations

from dataclasses import dataclass

from ..pipeline import (
    PersistedRenderLocalRequest,
    RenderLocalDenied,
    RenderLocalSuccess,
)
from ..scenario_registry import (
    ScenarioRegistryDenied,
    SemanticScenarioSuccess,
    UpstreamScenarioOutputs,
)
from ..semantic_chain import SemanticProfile
from ..store import CommandOutcome, Job, RuntimeStoreError
from .models import (
    AgentRunIntent,
    AgentRunResult,
    AgentRunStage,
    AgentRunState,
    AgentRuntimeError,
    AgentStageTrace,
)
from .ports import (
    LocalOutputConfiguration,
    MediaCommandPort,
    PersistedRenderPort,
    ScenarioRegistryPort,
    SemanticCommandPort,
    SucceededMediaArtifactsReader,
)


@dataclass(frozen=True, slots=True)
class AgentRuntimeService:
    """Execute the fixed four-stage local chain once, stopping on every denial."""

    scenarios: ScenarioRegistryPort
    upstream_media: MediaCommandPort
    semantic: SemanticCommandPort
    downstream_media: MediaCommandPort
    media_outputs: SucceededMediaArtifactsReader
    renderer: PersistedRenderPort
    output: LocalOutputConfiguration

    def run(self, intent: AgentRunIntent) -> AgentRunResult:
        if type(intent) is not AgentRunIntent:  # noqa: E721
            raise AgentRuntimeError("runtime accepts only AgentRunIntent")
        if intent.profile is SemanticProfile.PRODUCTION:
            return AgentRunResult(intent.run_id, intent.profile, AgentRunState.REJECTED_BEFORE_START, ())
        upstream_job, semantic_job, downstream_job = self._jobs(intent)
        traces: tuple[AgentStageTrace, ...] = ()
        try:
            upstream_plan = self.scenarios.prepare_upstream(intent.scenario, upstream_job)
            upstream_outcome = self.upstream_media.execute(upstream_plan.request)
            trace = self._trace(AgentRunStage.UPSTREAM_MEDIA, upstream_job, upstream_outcome)
            traces = (*traces, trace)
            state = self._command_stop(AgentRunStage.UPSTREAM_MEDIA, upstream_outcome)
            if state is not None:
                return AgentRunResult(intent.run_id, intent.profile, state, traces)
            upstream = self.media_outputs.read_succeeded_media_outputs(upstream_job)
        except ScenarioRegistryDenied:
            return self._failed(intent, AgentRunState.UPSTREAM_MEDIA_DENIED, traces, upstream_job)
        except RuntimeStoreError:
            return self._failed(intent, AgentRunState.UPSTREAM_MEDIA_FAILED, traces, upstream_job)
        except Exception:
            return self._failed(intent, AgentRunState.UPSTREAM_MEDIA_FAILED, traces, upstream_job)

        try:
            semantic_plan = self.scenarios.prepare_semantic(
                intent.scenario,
                semantic_job,
                UpstreamScenarioOutputs(upstream_job, upstream.media_evidence, upstream.recipe),
            )
            semantic_result = self.semantic.execute(semantic_plan.request)
            semantic_outcome = semantic_result.outcome
            trace = self._trace(AgentRunStage.SEMANTIC, semantic_job, semantic_outcome)
            traces = (*traces, trace)
            state = self._command_stop(AgentRunStage.SEMANTIC, semantic_outcome)
            if state is not None:
                return AgentRunResult(intent.run_id, intent.profile, state, traces)
            try:
                semantic_success = SemanticScenarioSuccess(semantic_plan, semantic_result)
            except ScenarioRegistryDenied:
                # A succeeded receipt without a re-resolvable bridge is an
                # infrastructure/provenance failure, not a semantic denial.
                traces = (
                    *traces[:-1],
                    AgentStageTrace(
                        AgentRunStage.SEMANTIC,
                        semantic_job.job_key,
                        "failed",
                        semantic_outcome.receipt_id,
                    ),
                )
                return self._failed(intent, AgentRunState.SEMANTIC_FAILED, traces, semantic_job)
        except ScenarioRegistryDenied:
            return self._failed(intent, AgentRunState.SEMANTIC_DENIED, traces, semantic_job)
        except RuntimeStoreError:
            return self._failed(intent, AgentRunState.SEMANTIC_FAILED, traces, semantic_job)
        except Exception:
            return self._failed(intent, AgentRunState.SEMANTIC_FAILED, traces, semantic_job)

        try:
            downstream_plan = self.scenarios.prepare_downstream(intent.scenario, downstream_job, semantic_success)
            downstream_outcome = self.downstream_media.execute(downstream_plan.request)
            trace = self._trace(AgentRunStage.DOWNSTREAM_MEDIA, downstream_job, downstream_outcome)
            traces = (*traces, trace)
            state = self._command_stop(AgentRunStage.DOWNSTREAM_MEDIA, downstream_outcome)
            if state is not None:
                return AgentRunResult(intent.run_id, intent.profile, state, traces)
            downstream = self.media_outputs.read_succeeded_media_outputs(downstream_job)
        except ScenarioRegistryDenied:
            return self._failed(intent, AgentRunState.DOWNSTREAM_MEDIA_DENIED, traces, downstream_job)
        except RuntimeStoreError:
            return self._failed(intent, AgentRunState.DOWNSTREAM_MEDIA_FAILED, traces, downstream_job)
        except Exception:
            return self._failed(intent, AgentRunState.DOWNSTREAM_MEDIA_FAILED, traces, downstream_job)

        try:
            render = self.renderer.execute_persisted(
                PersistedRenderLocalRequest(
                    downstream_job,
                    downstream.recipe,
                    upstream_plan.request.preflight_request.source_path,
                    self.output.output_root,
                    intent.run_id,
                )
            )
            if isinstance(render, RenderLocalSuccess):
                traces = (*traces, AgentStageTrace(AgentRunStage.RENDER, downstream_job.job_key, "succeeded", None))
                return AgentRunResult(intent.run_id, intent.profile, AgentRunState.SUCCEEDED, traces, render.promotion.current_path)
            if isinstance(render, RenderLocalDenied):
                traces = (*traces, AgentStageTrace(AgentRunStage.RENDER, downstream_job.job_key, "denied", None))
                return AgentRunResult(intent.run_id, intent.profile, AgentRunState.RENDER_DENIED, traces)
            traces = (*traces, AgentStageTrace(AgentRunStage.RENDER, downstream_job.job_key, "failed", None))
            return AgentRunResult(intent.run_id, intent.profile, AgentRunState.RENDER_FAILED, traces)
        except Exception:
            return self._failed(intent, AgentRunState.RENDER_FAILED, traces, downstream_job)

    @staticmethod
    def _jobs(intent: AgentRunIntent) -> tuple[Job, Job, Job]:
        suffix = intent.run_id.removeprefix("agent_run_")
        profile = intent.profile.value
        return (
            Job(f"runtime_{suffix}_upstream", profile),
            Job(f"runtime_{suffix}_semantic", profile),
            Job(f"runtime_{suffix}_downstream", profile),
        )

    @staticmethod
    def _trace(stage: AgentRunStage, job: Job, outcome: CommandOutcome) -> AgentStageTrace:
        if type(outcome) is not CommandOutcome:  # noqa: E721
            raise AgentRuntimeError("command port must return CommandOutcome")
        if outcome.state not in {"succeeded", "denied", "failed"}:
            raise AgentRuntimeError("command port returned a non-terminal outcome")
        return AgentStageTrace(stage, job.job_key, outcome.state, outcome.receipt_id)

    @staticmethod
    def _command_stop(stage: AgentRunStage, outcome: CommandOutcome) -> AgentRunState | None:
        if outcome.state == "succeeded":
            return None
        return {
            (AgentRunStage.UPSTREAM_MEDIA, "denied"): AgentRunState.UPSTREAM_MEDIA_DENIED,
            (AgentRunStage.UPSTREAM_MEDIA, "failed"): AgentRunState.UPSTREAM_MEDIA_FAILED,
            (AgentRunStage.SEMANTIC, "denied"): AgentRunState.SEMANTIC_DENIED,
            (AgentRunStage.SEMANTIC, "failed"): AgentRunState.SEMANTIC_FAILED,
            (AgentRunStage.DOWNSTREAM_MEDIA, "denied"): AgentRunState.DOWNSTREAM_MEDIA_DENIED,
            (AgentRunStage.DOWNSTREAM_MEDIA, "failed"): AgentRunState.DOWNSTREAM_MEDIA_FAILED,
        }[(stage, outcome.state)]

    @staticmethod
    def _failed(
        intent: AgentRunIntent,
        state: AgentRunState,
        traces: tuple[AgentStageTrace, ...],
        job: Job,
    ) -> AgentRunResult:
        stage = {
            AgentRunState.UPSTREAM_MEDIA_DENIED: AgentRunStage.UPSTREAM_MEDIA,
            AgentRunState.UPSTREAM_MEDIA_FAILED: AgentRunStage.UPSTREAM_MEDIA,
            AgentRunState.SEMANTIC_DENIED: AgentRunStage.SEMANTIC,
            AgentRunState.SEMANTIC_FAILED: AgentRunStage.SEMANTIC,
            AgentRunState.DOWNSTREAM_MEDIA_DENIED: AgentRunStage.DOWNSTREAM_MEDIA,
            AgentRunState.DOWNSTREAM_MEDIA_FAILED: AgentRunStage.DOWNSTREAM_MEDIA,
            AgentRunState.RENDER_DENIED: AgentRunStage.RENDER,
            AgentRunState.RENDER_FAILED: AgentRunStage.RENDER,
        }[state]
        command_state = "failed" if state.value.endswith("failed") else "denied"
        if traces and traces[-1].job_key == job.job_key:
            traces = (*traces[:-1], AgentStageTrace(stage, job.job_key, command_state, traces[-1].receipt_id))
        else:
            traces = (*traces, AgentStageTrace(stage, job.job_key, command_state, None))
        return AgentRunResult(intent.run_id, intent.profile, state, traces)
