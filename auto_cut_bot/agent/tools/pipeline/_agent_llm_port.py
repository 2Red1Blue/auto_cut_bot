"""AgentLLMPort — auto_cut_bot implementation of LLMPort.

Injects the Agent's LLM provider (via AgentRunner) into pipeline stages
so they can share the agent's model configuration, rate limits, and
tool-calling infrastructure. Falls back to autocut_core's direct
semantic batch runner when the Agent runner is not available.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from autocut_core.stages.ports import LLMPort
from auto_cut_bot.agent.skills import SkillsLoader

logger = logging.getLogger(__name__)

# Stage name → skill directory name mapping for context injection.
# Stages that share a domain use the same skill file.
_STAGE_SKILL_MAP: dict[str, str] = {
    # Source Prep
    "source_windows": "ac_source_prep",
    "source_transcripts": "ac_source_prep",
    "global_context": "ac_source_prep",
    "vlm_analysis": "ac_source_prep",
    "confidence_check": "ac_source_prep",
    "event_cards": "ac_source_prep",
    # Series Knowledge
    "episode_digests": "ac_series_knowledge",
    "chapter_digests": "ac_series_knowledge",
    "series_registry": "ac_series_knowledge",
    "series_assignment": "ac_series_knowledge",
    "series_bible": "ac_series_knowledge",
    # Story Generation
    "story_catalog": "ac_story_generation",
    "story_portfolio": "ac_story_generation",
    "story_treatments": "ac_story_generation",
    "story_scripts": "ac_story_generation",
    "story_preflight": "ac_story_generation",
    "story_approval": "ac_story_generation",
    # Plan Orchestration
    "story_evidence": "ac_plan_orchestration",
    "span_candidates": "ac_plan_orchestration",
    "story_plans": "ac_plan_orchestration",
    "story_plans_preflight": "ac_plan_orchestration",
    "story_plans_materialize": "ac_plan_orchestration",
    # QC
    "story_plans_qc_admission": "ac_qc",
    "story_qc": "ac_qc",
    "story_qc_review": "ac_qc",
    # Render
    "story_render": "ac_render",
    # Review
    "story_review": "ac_review",
}


class AgentLLMPort(LLMPort):
    """Agent-aware LLM port for pipeline stages.

    When the AgentRunner is available (running inside an agent loop), uses
    the agent's LLM provider for all calls. Otherwise falls back to the
    autocut_core semantic batch orchestrator and backend registry.

    Attributes:
        agent_runner: Optional AgentRunner instance. When set, all LLM calls
            go through the agent's provider chain.
        skills_loader: SkillsLoader instance for reading skill file content.
    """

    def __init__(
        self,
        agent_runner: Any = None,
        skills_loader: SkillsLoader | None = None,
    ) -> None:
        """Initialise the port.

        Args:
            agent_runner: Optional AgentRunner instance for LLM calls.
            skills_loader: Optional SkillsLoader; a default is created if None.
        """
        self._agent_runner = agent_runner
        self._skills_loader = skills_loader or SkillsLoader(
            workspace=Path("~/.auto_cut_bot/workspace").expanduser(),
        )

    # ------------------------------------------------------------------
    # run_batch
    # ------------------------------------------------------------------

    def run_batch(
        self,
        manifest_path: Path,
        *,
        backend: str,
        workers: int | str,
        requests_per_minute: float,
        semantic_retries: int,
        context_injection: dict[str, Any] | None = None,
        job_ids: list[str] | None = None,
    ) -> None:
        """Execute batch LLM inference.

        Tries to delegate to an installed ``ac_auto_cut`` batch orchestrator
        first (battle-tested pipeline implementation with media recovery,
        attempt ledgers, and cache lookups).  When ``ac_auto_cut`` is not
        available (pure open-source usage), falls back to a simple litellm-based
        concurrent batch runner.
        """
        # Try the ac_auto_cut orchestrator first (private deployment path)
        try:
            from ac_auto_cut.semantic.batch_orchestrator import run_batch as _run_batch  # type: ignore[import-not-found]
        except ImportError:
            _run_batch = None

        if _run_batch is not None:
            logger.info(
                "AgentLLMPort.run_batch: delegating to ac_auto_cut batch orchestrator "
                "(manifest=%s, backend=%s, workers=%s, rpm=%s, job_ids=%s)",
                manifest_path, backend, workers, requests_per_minute, job_ids,
            )
            _run_batch(
                manifest_path=manifest_path,
                backend=backend,
                workers=workers,
                requests_per_minute=requests_per_minute,
                semantic_retries=semantic_retries,
                context_injection=context_injection,
                job_ids=job_ids,
            )
            return

        # Open-source fallback: simple litellm-based batch execution
        self._run_batch_litellm(
            manifest_path=manifest_path,
            workers=workers,
            requests_per_minute=requests_per_minute,
            semantic_retries=semantic_retries,
            context_injection=context_injection,
            job_ids=job_ids,
        )

    def _run_batch_litellm(
        self,
        *,
        manifest_path: Path,
        workers: int | str,
        requests_per_minute: float,
        semantic_retries: int,
        context_injection: dict[str, Any] | None,
        job_ids: list[str] | None,
    ) -> None:
        """Simple litellm-based batch runner for open-source deployments.

        Reads the manifest JSON, executes jobs concurrently using a
        ThreadPoolExecutor, and writes results back.  This is a minimal
        implementation without cache, attempt ledgers, or media recovery;
        install ``ac_auto_cut`` for production-grade orchestration.
        """
        import json
        import time
        from concurrent.futures import ThreadPoolExecutor, as_completed

        try:
            import litellm
        except ImportError as exc:
            raise RuntimeError(
                "litellm is required for batch execution when ac_auto_cut is not installed"
            ) from exc

        with manifest_path.open("r", encoding="utf-8") as f:
            manifest = json.load(f)

        jobs = manifest.get("jobs", [])
        if job_ids:
            job_id_set = set(job_ids)
            jobs = [j for j in jobs if j.get("job_id") in job_id_set]

        if not jobs:
            return

        n_workers = workers if isinstance(workers, int) else min(8, len(jobs))
        rate_interval = 60.0 / requests_per_minute if requests_per_minute > 0 else 0
        _last_call_time = 0.0

        results_dir = Path(manifest.get("results_dir", manifest_path.parent / "results"))
        results_dir.mkdir(parents=True, exist_ok=True)

        def _execute_job(job: dict) -> tuple[str, dict]:
            nonlocal _last_call_time
            if rate_interval > 0:
                with __import__("threading").Lock():
                    now = time.monotonic()
                    delay = max(0, rate_interval - (now - _last_call_time))
                    if delay:
                        time.sleep(delay)
                    _last_call_time = time.monotonic()
            prompt = job.get("prompt", "")
            model = job.get("model", "gpt-4o")
            messages = job.get("messages") or [{"role": "user", "content": prompt}]
            for attempt in range(semantic_retries + 1):
                try:
                    resp = litellm.completion(
                        model=model, messages=messages,
                        temperature=job.get("temperature", 0.1),
                        max_tokens=job.get("max_tokens", 4096),
                        timeout=job.get("timeout", 120),
                    )
                    content = resp.choices[0].message.content or ""
                    return job["job_id"], {"status": "completed", "content": content}
                except Exception as exc:
                    if attempt == semantic_retries:
                        return job["job_id"], {"status": "error", "error": str(exc)}
                    time.sleep(min(30, 2 ** attempt))
            return job["job_id"], {"status": "error", "error": "max retries exceeded"}

        with ThreadPoolExecutor(max_workers=n_workers) as pool:
            futures = {pool.submit(_execute_job, job): job for job in jobs}
            for fut in as_completed(futures):
                job_id, result = fut.result()
                out_path = results_dir / f"{job_id}.json"
                with out_path.open("w", encoding="utf-8") as f:
                    json.dump(result, f, ensure_ascii=False, indent=2)

        logger.info(
            "AgentLLMPort._run_batch_litellm: completed %d jobs -> %s",
            len(jobs), results_dir,
        )

    # ------------------------------------------------------------------
    # build_context_injection
    # ------------------------------------------------------------------

    def build_context_injection(
        self,
        stage_name: str,
        config: Any,
        bus: Any,
    ) -> dict[str, Any] | None:
        """Build context injection from skill files and agent knowledge.

        Instead of querying the DB (like the autocut_core prompt_context
        module does), this reads skill files from the project's skills/
        directory.  The skill content provides the agent's domain
        knowledge (tools, contracts, editorial rules) for the current
        pipeline stage.

        Returns:
            A dict with keys:
              - ``global_context``: pipeline-level config (backend, mode, etc.)
              - ``skill_context``: skill file content for the current stage
              - ``stage_name``: the stage we are building context for
            Returns ``None`` when no skill content is available.
        """
        skill_name = _STAGE_SKILL_MAP.get(stage_name)
        if not skill_name:
            logger.debug(
                "AgentLLMPort.build_context_injection: no skill mapping for "
                "stage %r, skipping context injection",
                stage_name,
            )
            return None

        skill_content = self._skills_loader.load_skills_for_context([skill_name])
        if not skill_content:
            logger.debug(
                "AgentLLMPort.build_context_injection: skill %r not found or empty",
                skill_name,
            )
            return None

        context: dict[str, Any] = {
            "stage_name": stage_name,
            "skill_name": skill_name,
            "skill_context": skill_content,
            "global_context": {
                "backend": getattr(config, "backend", "qwen"),
                "mode": getattr(config, "mode", "auto"),
                "job_root": str(getattr(config, "job_root", "")),
            },
        }

        # 3. highlight_skill — 读取高光识别标准 (与 PipelineLLMPort 一致)
        highlight_skill = _load_highlight_skill(config)
        if highlight_skill:
            context["highlight_skill"] = highlight_skill

        # 4. global_context DB fallback — 从 DB 读取 synopsis/themes/relationships
        db_ctx = _build_db_global_context(config)
        if db_ctx:
            context["db_global_context"] = db_ctx

        logger.info(
            "AgentLLMPort.build_context_injection: built context for stage %r "
            "(skill=%s, %d chars)",
            stage_name, skill_name, len(skill_content),
        )
        return context

    # ------------------------------------------------------------------
    # call_llm
    # ------------------------------------------------------------------

    def call_llm(
        self,
        prompt: str,
        model: str,
        *,
        messages: list[dict[str, str]] | None = None,
        temperature: float = 0.1,
        max_tokens: int = 131072,
        response_format: dict[str, str] | None = None,
        timeout: float = 120.0,
    ) -> dict[str, Any]:
        """Single-shot LLM call.

        If the AgentRunner is available, uses the agent's LLM provider
        (which benefits from the agent's retry, rate-limit, and error
        recovery infrastructure).  Otherwise falls back to the
        autocut_core backend registry (``get_backend``).

        Returns:
            A dict with keys:
              - ``content``: the LLM response text
              - ``finish_reason``: ``"stop"``, ``"length"``, or ``"error"``
              - ``usage``: token usage dict, or empty dict
        """
        if self._agent_runner is not None:
            return self._call_via_agent(
                prompt=prompt,
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format=response_format,
                timeout=timeout,
            )

        return self._call_via_backend(
            prompt=prompt,
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
            timeout=timeout,
        )

    # ------------------------------------------------------------------
    # Internal: agent-provider path
    # ------------------------------------------------------------------

    def _call_via_agent(
        self,
        prompt: str,
        model: str,
        *,
        messages: list[dict[str, str]] | None,
        temperature: float,
        max_tokens: int,
        response_format: dict[str, str] | None,
        timeout: float,
    ) -> dict[str, Any]:
        """Call the LLM through the AgentRunner's provider."""
        import asyncio

        from auto_cut_bot.providers.base import LLMResponse

        provider = self._agent_runner_provider()
        if provider is None:
            logger.warning(
                "AgentLLMPort: AgentRunner provided but no LLM provider accessible; "
                "falling back to backend registry"
            )
            return self._call_via_backend(
                prompt=prompt,
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format=response_format,
                timeout=timeout,
            )

        payload: list[dict[str, str]] = list(messages or [])
        if not payload:
            payload = [{"role": "user", "content": prompt}]

        request_kwargs: dict[str, Any] = {
            "messages": payload,
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format:
            request_kwargs["response_format"] = response_format

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # We are inside an async context — create a task.
                # This is unusual for a sync method but possible when
                # called from a Stage that runs inside an event loop.
                import concurrent.futures

                future = concurrent.futures.Future()

                async def _do_call() -> None:
                    try:
                        result = await provider.chat_with_retry(**request_kwargs)
                        future.set_result(result)
                    except Exception as exc:
                        future.set_exception(exc)

                _ = asyncio.ensure_future(_do_call())
                response: LLMResponse = future.result(timeout=timeout)
            else:
                response = asyncio.run(
                    asyncio.wait_for(
                        provider.chat_with_retry(**request_kwargs),
                        timeout=timeout,
                    )
                )
        except Exception as exc:
            logger.error(
                "AgentLLMPort.call_llm: agent provider call failed: %s", exc,
            )
            return {
                "content": f"Error calling LLM via agent: {exc}",
                "finish_reason": "error",
                "usage": {},
            }

        return _llm_response_to_dict(response)

    def _agent_runner_provider(self) -> Any:
        """Extract the LLM provider from the AgentRunner instance.

        The AgentRunner itself does not expose a public ``provider``
        attribute — the provider lives on the AgentRunSpec.runtime
        which is assembled per-run.  We reach into the runner's
        internal state to find the most recent provider instance.
        """
        runner = self._agent_runner
        if runner is None:
            return None

        # AgentRunner does not hold a persistent provider reference.
        # The provider is attached to LLMRuntime, which is created
        # per-run.  As a pragmatic fallback, try the factory.
        try:
            from auto_cut_bot.providers.factory import ProviderFactory
            factory = ProviderFactory()
            return factory.get_default_provider()
        except Exception:
            logger.debug(
                "AgentLLMPort: could not resolve provider from AgentRunner or factory",
                exc_info=True,
            )
            return None

    # ------------------------------------------------------------------
    # Internal: backend-registry fallback
    # ------------------------------------------------------------------

    @staticmethod
    def _call_via_backend(
        prompt: str,
        model: str,
        *,
        messages: list[dict[str, str]] | None,
        temperature: float,
        max_tokens: int,
        response_format: dict[str, str] | None,
        timeout: float,
    ) -> dict[str, Any]:
        """Fallback: call the LLM through litellm (OpenAI-compatible).

        Uses litellm which supports all major providers via environment variables
        (OPENAI_API_KEY, ANTHROPIC_API_KEY, etc.).  This avoids any dependency on
        private deployment backends when running outside the agent framework.
        """
        try:
            import litellm
        except ImportError:
            return {
                "content": (
                    "Error: litellm is not installed and no AgentRunner provider is available. "
                    "Install litellm or provide an agent_runner to AgentLLMPort."
                ),
                "finish_reason": "error",
                "usage": {},
            }

        payload: list[dict[str, str]] = list(messages or [])
        if not payload:
            payload = [{"role": "user", "content": prompt}]

        try:
            result = litellm.completion(
                model=model,
                messages=payload,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
                response_format=response_format if response_format else None,
            )
            return {
                "content": result.choices[0].message.content or "",
                "finish_reason": result.choices[0].finish_reason or "stop",
                "usage": {
                    "prompt_tokens": getattr(result.usage, "prompt_tokens", 0),
                    "completion_tokens": getattr(result.usage, "completion_tokens", 0),
                    "total_tokens": getattr(result.usage, "total_tokens", 0),
                } if result.usage else {},
            }
        except Exception as exc:
            logger.error(
                "AgentLLMPort.call_llm: litellm fallback failed: %s", exc,
            )
            return {
                "content": f"Error calling LLM: {exc}",
                "finish_reason": "error",
                "usage": {},
            }


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

# Default skill file paths relative to job_root
_HIGHLIGHT_SKILL_REL_PATH = "skills/ac_story_generation/references/highlight-recognition.md"


def _load_highlight_skill(config: Any) -> str | None:
    """加载高光识别 skill 文件。"""
    job_root = getattr(config, "job_root", None)
    if job_root is None:
        return None
    skill_path = Path(job_root) / _HIGHLIGHT_SKILL_REL_PATH
    if not skill_path.is_file():
        return None
    try:
        return skill_path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.debug("Failed to read highlight_skill: %s", exc)
        return None


def _build_db_global_context(config: Any) -> str | None:
    """从 DB 构建 global_context (synopsis + themes + relationships)。"""
    from autocut_core.db.client import StageDBClient
    from autocut_core.semantic.prompt_context import build_global_context_injection

    book_id = _resolve_book_id(config)
    if not book_id:
        return None
    db_url = getattr(config, "db_url", None)
    if not db_url:
        return None
    db = StageDBClient(db_url=db_url, schema=getattr(config, "db_schema", "autocut"))
    if not db.is_available:
        return None
    try:
        return build_global_context_injection(book_id, db)
    except Exception:
        return None


def _resolve_book_id(config: Any) -> str | None:
    """从 config 解析 book_id。"""
    book_id = getattr(config, "book_id", None)
    if book_id:
        return str(book_id)
    extra = getattr(config, "extra", {}) or {}
    return extra.get("book_id") or None


def _llm_response_to_dict(response: Any) -> dict[str, Any]:
    """Convert an auto_cut_bot LLMResponse to a plain dict."""
    return {
        "content": getattr(response, "content", ""),
        "finish_reason": getattr(response, "finish_reason", "stop"),
        "usage": dict(getattr(response, "usage", {}) or {}),
    }


def _provider_result_to_dict(result: Any) -> dict[str, Any]:
    """Convert an autocut_core call_provider result to a plain dict.

    call_provider returns a dict with keys like ``choices``, ``usage``, etc.
    """
    if not isinstance(result, dict):
        return {
            "content": str(result),
            "finish_reason": "stop",
            "usage": {},
        }

    choices = result.get("choices", [])
    if choices and isinstance(choices, list):
        message = choices[0].get("message", {})
        content = message.get("content", "")
        finish_reason = choices[0].get("finish_reason", "stop")
    else:
        content = ""
        finish_reason = "stop"

    return {
        "content": content,
        "finish_reason": finish_reason,
        "usage": dict(result.get("usage", {}) or {}),
    }