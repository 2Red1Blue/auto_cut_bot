"""Fail-closed composition for the local PostgreSQL/Doubao pipeline runtime."""

# pyright: reportMissingTypeStubs=false

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

import psycopg
from autocut_kernel.registry.installed_runtime import (
    InstalledLocalRunProfileResolver,
    InstalledRuntimeCapabilityResolver,
    InstalledRuntimeTimedSpeechAuthorityResolver,
    load_installed_local_run_resolver,
    runtime_calibration_policy_for_installed_resource,
)
from autocut_kernel.registry.runtime_timed_speech import RuntimeTimedMediaAuthoritySelector
from autocut_kernel.source_manifest import SourceOperationPolicy, SourceOperationPurpose
from autocut_kernel.store import PostgresRuntimeStore, StoreValidationError
from autocut_kernel.store.models import MaterializationLimits
from autocut_kernel.store.postgres import DbConnection as KernelDbConnection
from autocut_kernel.store.postgres import validate_materialization_staging_root
from autocut_kernel.vlm import (
    GENERATION_RETRY_STRATEGY_VERSION,
    GenerationRetryPolicy,
)

from auto_cut_bot.pipeline.media_preflight import (
    FunASRRuntimeMeasurementIdentityHttpPort,
    LocalMediaPreflightPolicy,
    LocalMediaPreflightPort,
)
from auto_cut_bot.pipeline.media_preflight.installed_policy import validate_installed_media_policy
from auto_cut_bot.pipeline.source_prep import AuthorizedSeriesSourceRoot
from auto_cut_bot.pipeline.vlm import (
    DoubaoArkVlmProvider,
    DoubaoArkVlmProviderConfig,
    DoubaoVlmRequestPolicy,
    PostgresArkFileCache,
)
from auto_cut_bot.pipeline.vlm.ark_file_cache import DbConnection as ArkDbConnection
from auto_cut_bot.pipeline.vlm.ark_responses_transport import ArkResponsesTransportConfig
from auto_cut_bot.pipeline.vlm.doubao_draft_provider import DoubaoDraftProvider
from auto_cut_bot.pipeline.vlm.policy_binding import validate_installed_vlm_policy

from .highlight_projection import PipelineHighlightReadService
from .media_preflight_stage import MediaPreflightPipelineStage, media_evidence_read_limits
from .models import EvidenceReadLimits, PipelineExecutionProfile, PipelineRunRequest
from .ports import PipelineRunService
from .postgres import ConnectionFactory, PostgresPipelineRunStore, PostgresPipelineScheduler
from .semantic_authority import (
    SemanticRunAuthorityError,
    load_installed_semantic_run_authority,
)
from .service import DurablePipelineRunService
from .source_prep_stage import SourcePrepPipelineStage
from .stage1_narrative_stage import Stage1NarrativePipelineStage
from .stage2_portfolio_stage import Stage2PortfolioPipelineStage
from .stage3_blueprint_stage import Stage3BlueprintPipelineStage
from .stages import PipelineStageReconciler, PipelineStageRegistry, PipelineStageRunner
from .vlm_stage import VlmPipelineStage
from .worker import DurablePipelineWorker

PIPELINE_POSTGRES_DSN_ENV = "AUTO_CUT_BOT_PIPELINE_POSTGRES_DSN"
PIPELINE_KERNEL_POSTGRES_DSN_ENV = "AUTO_CUT_BOT_PIPELINE_KERNEL_POSTGRES_DSN"
PIPELINE_SOURCE_CATALOG_ENV = "AUTO_CUT_BOT_PIPELINE_SOURCE_CATALOG"
PIPELINE_ARK_API_KEY_ENV = "AUTO_CUT_BOT_PIPELINE_ARK_API_KEY"
PIPELINE_ARK_TENANT_ID_ENV = "AUTO_CUT_BOT_PIPELINE_ARK_TENANT_ID"
PIPELINE_ARK_PROJECT_ID_ENV = "AUTO_CUT_BOT_PIPELINE_ARK_PROJECT_ID"
PIPELINE_ARK_MODEL_ID_ENV = "AUTO_CUT_BOT_PIPELINE_ARK_MODEL_ID"
PIPELINE_ARK_MAX_OUTPUT_TOKENS_ENV = "AUTO_CUT_BOT_PIPELINE_ARK_MAX_OUTPUT_TOKENS"
PIPELINE_ARK_BASE_URL_ENV = "AUTO_CUT_BOT_PIPELINE_ARK_BASE_URL"
PIPELINE_MEDIA_PREFLIGHT_POLICY_ENV = "AUTO_CUT_BOT_MEDIA_PREFLIGHT_POLICY_JSON"
PIPELINE_MEDIA_PREFLIGHT_STAGING_ROOT_ENV = "AUTO_CUT_BOT_MEDIA_PREFLIGHT_STAGING_ROOT"
PIPELINE_MEDIA_PREFLIGHT_MATERIALIZATION_LIMITS_ENV = (
    "AUTO_CUT_BOT_MEDIA_PREFLIGHT_MATERIALIZATION_LIMITS_JSON"
)
PIPELINE_EVIDENCE_READ_LIMITS_ENV = "AUTO_CUT_BOT_PIPELINE_EVIDENCE_READ_LIMITS_JSON"
PIPELINE_PLAN_ENV = "AUTO_CUT_BOT_PIPELINE_PLAN"
SEMANTIC_ONLY_PLAN = "semantic_only"

# Retained as import-compatible names only. They never authorize the real runtime.
PIPELINE_SOURCE_ROOTS_ENV = "AUTO_CUT_BOT_PIPELINE_SOURCE_ROOTS"
PIPELINE_SOURCE_REFERENCES_ENV = "AUTO_CUT_BOT_PIPELINE_SOURCE_REFERENCES"

_REQUIRED_ENVIRONMENT = (
    PIPELINE_POSTGRES_DSN_ENV,
    PIPELINE_SOURCE_CATALOG_ENV,
    PIPELINE_ARK_API_KEY_ENV,
    PIPELINE_ARK_TENANT_ID_ENV,
    PIPELINE_ARK_PROJECT_ID_ENV,
    PIPELINE_ARK_MODEL_ID_ENV,
    PIPELINE_ARK_MAX_OUTPUT_TOKENS_ENV,
    PIPELINE_MEDIA_PREFLIGHT_POLICY_ENV,
    PIPELINE_MEDIA_PREFLIGHT_STAGING_ROOT_ENV,
    PIPELINE_MEDIA_PREFLIGHT_MATERIALIZATION_LIMITS_ENV,
    PIPELINE_EVIDENCE_READ_LIMITS_ENV,
)
_SEMANTIC_REQUIRED_ENVIRONMENT = (
    PIPELINE_POSTGRES_DSN_ENV,
    PIPELINE_SOURCE_CATALOG_ENV,
    PIPELINE_ARK_API_KEY_ENV,
    PIPELINE_ARK_TENANT_ID_ENV,
    PIPELINE_ARK_PROJECT_ID_ENV,
    PIPELINE_ARK_MODEL_ID_ENV,
    PIPELINE_ARK_MAX_OUTPUT_TOKENS_ENV,
)
_MATERIALIZATION_LIMIT_FIELDS = frozenset(
    {
        "copy_chunk_bytes",
        "max_source_bytes",
        "staging_quota_bytes",
        "timed_speech_max_request_bytes",
    }
)
_CATALOG_FIELDS = frozenset(
    {
        "authorization_id",
        "authorization_policy_sha256",
        "authorized_path",
        "authorized_purposes",
        "expected_source_count",
        "series_id",
    }
)

DOUBAO_GENERATION_RETRY_POLICY = GenerationRetryPolicy(
    strategy_version=GENERATION_RETRY_STRATEGY_VERSION,
    max_attempts=3,
    backoff_seconds=(2, 8),
)


class PipelineRuntimeConfigurationError(ValueError):
    """Raised when an enabled paid runtime has incomplete or invalid config."""


@dataclass(frozen=True, slots=True)
class SourceCatalogEntry:
    """One exact local source boundary and its non-secret authorization identity."""

    authorized_root: AuthorizedSeriesSourceRoot

    @property
    def path(self) -> Path:
        return self.authorized_root.root

    @property
    def authorization_id(self) -> str:
        return self.authorized_root.policy.authorization_id


class ConfiguredSourceCatalog:
    """Authorize and resolve only exact entries from a closed source catalog."""

    def __init__(self, entries: tuple[SourceCatalogEntry, ...]) -> None:
        if not entries:
            raise PipelineRuntimeConfigurationError("source catalog must not be empty")
        paths = tuple(entry.path for entry in entries)
        authorization_ids = tuple(entry.authorization_id for entry in entries)
        series_ids = tuple(entry.authorized_root.policy.series_id for entry in entries)
        if len(set(paths)) != len(paths):
            raise PipelineRuntimeConfigurationError("source catalog paths must be unique")
        if len(set(authorization_ids)) != len(authorization_ids):
            raise PipelineRuntimeConfigurationError(
                "source catalog authorization_id values must be unique"
            )
        if len(set(series_ids)) != len(series_ids):
            raise PipelineRuntimeConfigurationError(
                "source catalog series_id values must be unique"
            )
        self._entries = entries

    @classmethod
    def from_json(cls, raw: str) -> ConfiguredSourceCatalog:
        try:
            decoded = cast(object, json.loads(raw, object_pairs_hook=_closed_json_object))
        except (json.JSONDecodeError, UnicodeError, ValueError) as error:
            raise PipelineRuntimeConfigurationError(
                f"{PIPELINE_SOURCE_CATALOG_ENV} must be valid JSON"
            ) from error
        if type(decoded) is not list or not decoded:  # noqa: E721
            raise PipelineRuntimeConfigurationError(
                f"{PIPELINE_SOURCE_CATALOG_ENV} must be a non-empty JSON array"
            )
        entries: list[SourceCatalogEntry] = []
        for index, value in enumerate(cast(list[object], decoded)):
            if type(value) is not dict:  # noqa: E721
                raise PipelineRuntimeConfigurationError(
                    f"source catalog entry {index} must be an object"
                )
            item = cast(dict[object, object], value)
            if any(type(key) is not str for key in item) or frozenset(item) != _CATALOG_FIELDS:
                raise PipelineRuntimeConfigurationError(
                    f"source catalog entry {index} must contain only the closed catalog fields"
                )
            authorized_path = _catalog_text(item["authorized_path"], index, "authorized_path")
            path = Path(authorized_path).expanduser()
            if not path.is_absolute():
                raise PipelineRuntimeConfigurationError(
                    f"source catalog entry {index} authorized_path must be absolute"
                )
            expected_count = item["expected_source_count"]
            if type(expected_count) is not int or expected_count < 1:  # noqa: E721
                raise PipelineRuntimeConfigurationError(
                    f"source catalog entry {index} expected_source_count must be positive"
                )
            raw_purposes = item["authorized_purposes"]
            if type(raw_purposes) is not list or not raw_purposes:  # noqa: E721
                raise PipelineRuntimeConfigurationError(
                    f"source catalog entry {index} authorized_purposes must be a non-empty array"
                )
            purposes = tuple(cast(list[object], raw_purposes))
            if any(type(purpose) is not str for purpose in purposes):
                raise PipelineRuntimeConfigurationError(
                    f"source catalog entry {index} authorized_purposes must contain strings"
                )
            try:
                policy = SourceOperationPolicy(
                    authorization_id=_catalog_text(
                        item["authorization_id"], index, "authorization_id"
                    ),
                    series_id=_catalog_text(item["series_id"], index, "series_id"),
                    expected_source_count=expected_count,
                    authorized_purposes=cast(
                        tuple[SourceOperationPurpose, ...],
                        purposes,
                    ),
                )
            except ValueError as error:
                raise PipelineRuntimeConfigurationError(
                    f"source catalog entry {index} operation policy is invalid"
                ) from error
            if purposes != policy.authorized_purposes:
                raise PipelineRuntimeConfigurationError(
                    f"source catalog entry {index} authorized_purposes must be canonical"
                )
            supplied_policy_hash = _catalog_text(
                item["authorization_policy_sha256"],
                index,
                "authorization_policy_sha256",
            )
            if supplied_policy_hash != policy.policy_sha256:
                raise PipelineRuntimeConfigurationError(
                    f"source catalog entry {index} authorization policy hash mismatch"
                )
            entries.append(
                SourceCatalogEntry(
                    AuthorizedSeriesSourceRoot(
                        root=path.resolve(strict=False),
                        policy=policy,
                    )
                )
            )
        return cls(tuple(entries))

    def allows(self, request: PipelineRunRequest) -> bool:
        return self._match(request) is not None

    def resolve(self, context: object) -> AuthorizedSeriesSourceRoot:
        request = getattr(context, "request", None)
        if type(request) is not PipelineRunRequest:  # noqa: E721
            raise PipelineRuntimeConfigurationError(
                "source catalog resolver requires a pipeline stage context"
            )
        matched = self._match(request)
        if matched is None:
            raise PipelineRuntimeConfigurationError(
                "persisted source is not present in the configured source catalog"
            )
        return matched.authorized_root

    def _match(self, request: PipelineRunRequest) -> SourceCatalogEntry | None:
        if request.source_root is not None:
            candidate = Path(request.source_root).expanduser()
            if not candidate.is_absolute():
                return None
            resolved = candidate.resolve(strict=False)
            return next((entry for entry in self._entries if entry.path == resolved), None)
        reference = request.source_reference
        return next(
            (entry for entry in self._entries if entry.authorization_id == reference),
            None,
        )


@dataclass(frozen=True, slots=True)
class PipelineRuntime:
    """Composed service/worker pair with an application-owned lifecycle surface."""

    service: DurablePipelineRunService
    worker: DurablePipelineWorker
    execution_profile: PipelineExecutionProfile
    authority_profile_resolver: InstalledLocalRunProfileResolver | None
    authority_store: PostgresRuntimeStore

    async def startup_reconstruct(self) -> tuple[str, ...]:
        """Recover control-plane work without turning missing calibration into outage.

        Static resource/configuration checks happen while the runtime is
        composed. Dynamic per-environment calibration is a media-evidence
        prerequisite and is classified by that stage as ``awaiting_calibration``
        or ``recompute_needed``; it is not an HTTP startup prerequisite.
        """
        return await self.worker.startup_reconstruct()

    async def run_forever(
        self,
        stop_event: asyncio.Event,
        *,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        await self.worker.run_forever(
            stop_event,
            poll_interval_seconds=poll_interval_seconds,
        )


class PipelineRuntimePort(Protocol):
    """Structural lifecycle seam accepted by the HTTP app for local test injection."""

    @property
    def service(self) -> PipelineRunService: ...

    async def startup_reconstruct(self) -> tuple[str, ...]: ...

    async def run_forever(
        self,
        stop_event: asyncio.Event,
        *,
        poll_interval_seconds: float = 1.0,
    ) -> None: ...


def compose_pipeline_highlight_read_service_from_environment(
    environ: Mapping[str, str] | None = None,
) -> PipelineHighlightReadService | None:
    """Compose only the durable stores needed by the read-only highlights view."""
    values = os.environ if environ is None else environ
    control_dsn = values.get(PIPELINE_POSTGRES_DSN_ENV, "").strip()
    if not control_dsn:
        return None
    kernel_dsn = values.get(PIPELINE_KERNEL_POSTGRES_DSN_ENV, "").strip() or control_dsn
    control_factory = cast(ConnectionFactory, lambda: psycopg.connect(control_dsn))

    def kernel_factory() -> psycopg.Connection[tuple[object, ...]]:
        return psycopg.connect(kernel_dsn)

    return PipelineHighlightReadService(
        PostgresPipelineRunStore(control_factory),
        PostgresRuntimeStore(cast(Callable[[], KernelDbConnection], kernel_factory)),
    )


def compose_pipeline_runtime_from_environment(
    environ: Mapping[str, str] | None = None,
) -> PipelineRuntime | None:
    """Compose the paid runtime, rejecting every partial configuration.

    Authority comes only from the fixed controlled installed resource, never
    from an argument, environment JSON, checkout or Pipeline HTTP request.
    """
    values = os.environ if environ is None else environ
    plan = values.get(PIPELINE_PLAN_ENV, "").strip()
    if plan == SEMANTIC_ONLY_PLAN:
        return _compose_semantic_only_runtime(values)
    if plan:
        raise PipelineRuntimeConfigurationError(
            f"{PIPELINE_PLAN_ENV} must be empty or {SEMANTIC_ONLY_PLAN}"
        )
    relevant = _REQUIRED_ENVIRONMENT + (
        PIPELINE_KERNEL_POSTGRES_DSN_ENV,
        PIPELINE_ARK_BASE_URL_ENV,
    )
    if not any(values.get(name, "").strip() for name in relevant):
        return None
    missing = tuple(name for name in _REQUIRED_ENVIRONMENT if not values.get(name, "").strip())
    if missing:
        raise PipelineRuntimeConfigurationError(
            "pipeline runtime configuration is incomplete; missing: " + ", ".join(missing)
        )
    control_dsn = values[PIPELINE_POSTGRES_DSN_ENV].strip()
    kernel_dsn = values.get(PIPELINE_KERNEL_POSTGRES_DSN_ENV, "").strip() or control_dsn
    catalog = ConfiguredSourceCatalog.from_json(values[PIPELINE_SOURCE_CATALOG_ENV].strip())
    try:
        raw_max_output_tokens = values[PIPELINE_ARK_MAX_OUTPUT_TOKENS_ENV].strip()
        if not raw_max_output_tokens.isdecimal():
            raise ValueError("Ark max output tokens must be a decimal integer")
        policy = DoubaoVlmRequestPolicy(
            model_id=values[PIPELINE_ARK_MODEL_ID_ENV].strip(),
            max_output_tokens=int(raw_max_output_tokens),
        )
        decoded_media_policy = cast(
            object,
            json.loads(
                values[PIPELINE_MEDIA_PREFLIGHT_POLICY_ENV].strip(),
                object_pairs_hook=_closed_json_object,
            ),
        )
        if type(decoded_media_policy) is not dict:  # noqa: E721
            raise ValueError("media preflight policy must be an object")
        media_policy = LocalMediaPreflightPolicy.from_mapping(
            cast(dict[str, object], decoded_media_policy)
        )
        materialization_limits = _materialization_limits_from_json(
            values[PIPELINE_MEDIA_PREFLIGHT_MATERIALIZATION_LIMITS_ENV].strip()
        )
        evidence_read_limits = EvidenceReadLimits.from_mapping(
            json.loads(
                values[PIPELINE_EVIDENCE_READ_LIMITS_ENV].strip(),
                object_pairs_hook=_closed_json_object,
            )
        )
        staging_root = _staging_root(values[PIPELINE_MEDIA_PREFLIGHT_STAGING_ROOT_ENV].strip())
        authority_profile_resolver = load_installed_local_run_resolver()
        stage1_policy = authority_profile_resolver.resource.narrative.command_policy
        stage2_policy = authority_profile_resolver.resource.local_run.stage2_command_policy
        stage3_policy = authority_profile_resolver.resource.local_run.stage3_command_policy
        execution_profile = PipelineExecutionProfile.from_policies(
            policy,
            media_policy,
            retry_policy=DOUBAO_GENERATION_RETRY_POLICY,
            materialization_limits=materialization_limits,
            evidence_read_limits=evidence_read_limits,
            stage1_policy=stage1_policy,
            stage2_policy=stage2_policy,
            stage3_policy=stage3_policy,
        )
        media_evidence_read_limits(execution_profile)
        validate_installed_vlm_policy(
            authority_profile_resolver.resource.narrative,
            policy,
            DOUBAO_GENERATION_RETRY_POLICY,
        )
        validate_installed_media_policy(authority_profile_resolver.resource, media_policy)
        if materialization_limits.timed_speech_max_request_bytes != (
            authority_profile_resolver.resource.local_run.native_timed_speech.max_request_bytes
        ):
            raise ValueError("timed speech request limit differs from installed service")
        runtime_identity_port = FunASRRuntimeMeasurementIdentityHttpPort(
            timed_speech_endpoint_url=media_policy.timed_speech_endpoint_url,
            shared_token=os.environ.get("FUNASR_SHARED_TOKEN", ""),
        )
        runtime_calibration_policy = runtime_calibration_policy_for_installed_resource(
            authority_profile_resolver.resource
        )
        runtime_capability_resolver = InstalledRuntimeCapabilityResolver(runtime_calibration_policy)
        runtime_authority_resolver = InstalledRuntimeTimedSpeechAuthorityResolver(
            runtime_capability_resolver,
            RuntimeTimedMediaAuthoritySelector(
                runtime_calibration_policy,
                authority_profile_resolver.resource.local_run.source_clock_policy,
                authority_profile_resolver.resource.local_run.timing_policies,
            ),
            media_policy.canonical_hash,
        )
        api_key = values[PIPELINE_ARK_API_KEY_ENV].strip()
        tenant_id = values[PIPELINE_ARK_TENANT_ID_ENV].strip()
        project_id = values[PIPELINE_ARK_PROJECT_ID_ENV].strip()
        configured_base_url = values.get(PIPELINE_ARK_BASE_URL_ENV, "").strip()
        provider_config = (
            DoubaoArkVlmProviderConfig(
                api_key=api_key,
                tenant_id=tenant_id,
                project_id=project_id,
                base_url=configured_base_url,
            )
            if configured_base_url
            else DoubaoArkVlmProviderConfig(
                api_key=api_key,
                tenant_id=tenant_id,
                project_id=project_id,
            )
        )
    except PipelineRuntimeConfigurationError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise PipelineRuntimeConfigurationError(
            "pipeline Doubao/provider/media-preflight configuration is invalid"
        ) from error

    control_factory = cast(ConnectionFactory, lambda: psycopg.connect(control_dsn))

    def kernel_factory() -> psycopg.Connection[tuple[object, ...]]:
        return psycopg.connect(kernel_dsn)

    control_store = PostgresPipelineRunStore(control_factory)
    scheduler = PostgresPipelineScheduler(control_factory)
    kernel_store = PostgresRuntimeStore(
        cast(Callable[[], KernelDbConnection], kernel_factory),
        materialization_staging_root=staging_root,
    )
    file_cache = PostgresArkFileCache(cast(Callable[[], ArkDbConnection], kernel_factory))
    provider = DoubaoArkVlmProvider(provider_config, file_cache=file_cache)
    draft_provider = DoubaoDraftProvider(
        ArkResponsesTransportConfig(
            provider_config.api_key,
            provider_config.base_url,
            provider_config.timeout_seconds,
            stage1_policy.draft_policy.max_response_bytes,
        ),
        max_request_bytes=stage1_policy.draft_policy.max_prompt_bytes,
    )
    source_stage = SourcePrepPipelineStage(kernel_store, catalog)
    portfolio_provider = DoubaoDraftProvider(
        ArkResponsesTransportConfig(
            provider_config.api_key,
            provider_config.base_url,
            provider_config.timeout_seconds,
            stage2_policy.draft_policy.max_response_bytes,
        ),
        max_request_bytes=stage2_policy.max_prompt_bytes,
    )
    blueprint_provider = DoubaoDraftProvider(
        ArkResponsesTransportConfig(
            provider_config.api_key,
            provider_config.base_url,
            provider_config.timeout_seconds,
            stage3_policy.draft_policy.max_response_bytes,
        ),
        max_request_bytes=stage3_policy.max_prompt_bytes,
    )
    vlm_stage = VlmPipelineStage(
        kernel_store,
        provider,
        installed_profile=authority_profile_resolver.resource,
    )
    media_preflight_stage = MediaPreflightPipelineStage(
        kernel_store,
        LocalMediaPreflightPort(),
        authority_profile_resolver,
        runtime_authority_resolver,
        runtime_identity_port,
    )
    narrative_stage = Stage1NarrativePipelineStage(
        kernel_store,
        draft_provider,
        installed_profile=authority_profile_resolver.resource,
    )
    portfolio_stage = Stage2PortfolioPipelineStage(
        kernel_store,
        portfolio_provider,
        installed_profile=authority_profile_resolver.resource,
    )
    blueprint_stage = Stage3BlueprintPipelineStage(
        kernel_store,
        blueprint_provider,
        installed_profile=authority_profile_resolver.resource,
    )
    registry = PipelineStageRegistry.from_ports(
        ("source_prep", source_stage),
        ("vlm", vlm_stage),
        ("stage1_narrative", narrative_stage),
        ("stage2_portfolio", portfolio_stage),
        ("stage3_blueprint", blueprint_stage),
        ("media_preflight", media_preflight_stage),
    )
    runner = PipelineStageRunner(registry, control_store)
    reconciler = PipelineStageReconciler.from_ports(
        control_store,
        ("source_prep", source_stage),
        ("vlm", vlm_stage),
        ("stage1_narrative", narrative_stage),
        ("stage2_portfolio", portfolio_stage),
        ("stage3_blueprint", blueprint_stage),
        ("media_preflight", media_preflight_stage),
    )
    service = DurablePipelineRunService(
        control_store,
        scheduler,
        catalog,
        execution_profile=execution_profile,
    )
    worker = DurablePipelineWorker(
        worker_id=f"pipeline-http-{os.getpid()}",
        service=service,
        scheduler=scheduler,
        store=control_store,
        runner=runner,
        reconciler=reconciler,
    )
    return PipelineRuntime(
        service,
        worker,
        execution_profile,
        authority_profile_resolver,
        kernel_store,
    )


def compose_pipeline_run_service_from_environment() -> PipelineRunService | None:
    """Compatibility seam returning the service from the fully composed runtime."""
    runtime = compose_pipeline_runtime_from_environment()
    return None if runtime is None else runtime.service


def _compose_semantic_only_runtime(values: Mapping[str, str]) -> PipelineRuntime | None:
    """Compose the real HTTP SourcePrep/VLM plan without media authority.

    This is not a fallback from a full plan.  It is selected explicitly and the
    installed semantic resource binds every paid VLM policy.  No FunASR/media,
    story, render, or publication port is constructed here.
    """
    relevant = _SEMANTIC_REQUIRED_ENVIRONMENT + (PIPELINE_KERNEL_POSTGRES_DSN_ENV, PIPELINE_ARK_BASE_URL_ENV)
    if not any(values.get(name, "").strip() for name in relevant):
        return None
    missing = tuple(name for name in _SEMANTIC_REQUIRED_ENVIRONMENT if not values.get(name, "").strip())
    if missing:
        raise PipelineRuntimeConfigurationError(
            "semantic pipeline runtime configuration is incomplete; missing: " + ", ".join(missing)
        )
    control_dsn = values[PIPELINE_POSTGRES_DSN_ENV].strip()
    kernel_dsn = values.get(PIPELINE_KERNEL_POSTGRES_DSN_ENV, "").strip() or control_dsn
    try:
        catalog = ConfiguredSourceCatalog.from_json(values[PIPELINE_SOURCE_CATALOG_ENV].strip())
        raw_max_output_tokens = values[PIPELINE_ARK_MAX_OUTPUT_TOKENS_ENV].strip()
        if not raw_max_output_tokens.isdecimal():
            raise ValueError("Ark max output tokens must be a decimal integer")
        configured_policy = DoubaoVlmRequestPolicy(
            model_id=values[PIPELINE_ARK_MODEL_ID_ENV].strip(),
            max_output_tokens=int(raw_max_output_tokens),
        )
        semantic_authority = load_installed_semantic_run_authority()
        if configured_policy != semantic_authority.vlm_policy:
            raise ValueError("configured Doubao policy differs from installed semantic authority")
        execution_profile = PipelineExecutionProfile.from_semantic_policies(
            semantic_authority.vlm_policy,
            retry_policy=semantic_authority.retry_policy,
        )
        api_key = values[PIPELINE_ARK_API_KEY_ENV].strip()
        tenant_id = values[PIPELINE_ARK_TENANT_ID_ENV].strip()
        project_id = values[PIPELINE_ARK_PROJECT_ID_ENV].strip()
        configured_base_url = values.get(PIPELINE_ARK_BASE_URL_ENV, "").strip()
        provider_config = (
            DoubaoArkVlmProviderConfig(api_key=api_key, tenant_id=tenant_id, project_id=project_id, base_url=configured_base_url)
            if configured_base_url
            else DoubaoArkVlmProviderConfig(api_key=api_key, tenant_id=tenant_id, project_id=project_id)
        )
    except PipelineRuntimeConfigurationError:
        raise
    except (SemanticRunAuthorityError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise PipelineRuntimeConfigurationError(
            "semantic pipeline Doubao/authority configuration is invalid"
        ) from error

    control_factory = cast(ConnectionFactory, lambda: psycopg.connect(control_dsn))

    def kernel_factory() -> psycopg.Connection[tuple[object, ...]]:
        return psycopg.connect(kernel_dsn)

    control_store = PostgresPipelineRunStore(control_factory)
    scheduler = PostgresPipelineScheduler(control_factory)
    kernel_store = PostgresRuntimeStore(cast(Callable[[], KernelDbConnection], kernel_factory))
    provider = DoubaoArkVlmProvider(
        provider_config,
        file_cache=PostgresArkFileCache(cast(Callable[[], ArkDbConnection], kernel_factory)),
    )
    source_stage = SourcePrepPipelineStage(kernel_store, catalog)
    vlm_stage = VlmPipelineStage(kernel_store, provider)
    registry = PipelineStageRegistry.from_ports(
        ("source_prep", source_stage),
        ("vlm", vlm_stage),
    )
    service = DurablePipelineRunService(
        control_store, scheduler, catalog, execution_profile=execution_profile,
    )
    worker = DurablePipelineWorker(
        worker_id=f"pipeline-http-{os.getpid()}", service=service, scheduler=scheduler,
        store=control_store, runner=PipelineStageRunner(registry, control_store),
        reconciler=PipelineStageReconciler.from_ports(
            control_store, ("source_prep", source_stage), ("vlm", vlm_stage),
        ),
    )
    return PipelineRuntime(service, worker, execution_profile, None, kernel_store)


def _catalog_text(value: object, index: int, field_name: str) -> str:
    if type(value) is not str or not value.strip() or value != value.strip():  # noqa: E721
        raise PipelineRuntimeConfigurationError(
            f"source catalog entry {index} {field_name} must be canonical non-empty text"
        )
    return value


def _materialization_limits_from_json(raw: str) -> MaterializationLimits:
    try:
        decoded = cast(object, json.loads(raw, object_pairs_hook=_closed_json_object))
    except (json.JSONDecodeError, UnicodeError, ValueError) as error:
        raise PipelineRuntimeConfigurationError(
            f"{PIPELINE_MEDIA_PREFLIGHT_MATERIALIZATION_LIMITS_ENV} must be valid JSON"
        ) from error
    if type(decoded) is not dict:  # noqa: E721
        raise PipelineRuntimeConfigurationError(
            f"{PIPELINE_MEDIA_PREFLIGHT_MATERIALIZATION_LIMITS_ENV} must be an object"
        )
    mapping = cast(dict[str, object], decoded)
    if frozenset(mapping) != _MATERIALIZATION_LIMIT_FIELDS:
        raise PipelineRuntimeConfigurationError(
            f"{PIPELINE_MEDIA_PREFLIGHT_MATERIALIZATION_LIMITS_ENV} must contain only "
            "the closed materialization-limit fields"
        )
    try:
        return MaterializationLimits(
            max_source_bytes=cast(int, mapping["max_source_bytes"]),
            timed_speech_max_request_bytes=cast(int, mapping["timed_speech_max_request_bytes"]),
            copy_chunk_bytes=cast(int, mapping["copy_chunk_bytes"]),
            staging_quota_bytes=cast(int, mapping["staging_quota_bytes"]),
        )
    except (TypeError, ValueError) as error:
        raise PipelineRuntimeConfigurationError(
            f"{PIPELINE_MEDIA_PREFLIGHT_MATERIALIZATION_LIMITS_ENV} is invalid"
        ) from error


def _staging_root(raw: str) -> Path:
    root = Path(raw)
    if not raw or not root.is_absolute():
        raise PipelineRuntimeConfigurationError(
            f"{PIPELINE_MEDIA_PREFLIGHT_STAGING_ROOT_ENV} must be an absolute path"
        )
    try:
        return validate_materialization_staging_root(root)
    except StoreValidationError as error:
        raise PipelineRuntimeConfigurationError(
            f"{PIPELINE_MEDIA_PREFLIGHT_STAGING_ROOT_ENV} must be a private 0700 directory"
        ) from error


def _closed_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


__all__ = (
    "ConfiguredSourceCatalog",
    "PIPELINE_ARK_API_KEY_ENV",
    "PIPELINE_ARK_BASE_URL_ENV",
    "PIPELINE_ARK_MODEL_ID_ENV",
    "PIPELINE_ARK_MAX_OUTPUT_TOKENS_ENV",
    "PIPELINE_PLAN_ENV",
    "PIPELINE_ARK_PROJECT_ID_ENV",
    "PIPELINE_ARK_TENANT_ID_ENV",
    "PIPELINE_KERNEL_POSTGRES_DSN_ENV",
    "PIPELINE_EVIDENCE_READ_LIMITS_ENV",
    "PIPELINE_MEDIA_PREFLIGHT_POLICY_ENV",
    "PIPELINE_MEDIA_PREFLIGHT_MATERIALIZATION_LIMITS_ENV",
    "PIPELINE_MEDIA_PREFLIGHT_STAGING_ROOT_ENV",
    "PIPELINE_POSTGRES_DSN_ENV",
    "PIPELINE_SOURCE_CATALOG_ENV",
    "PIPELINE_SOURCE_REFERENCES_ENV",
    "PIPELINE_SOURCE_ROOTS_ENV",
    "PipelineRuntime",
    "PipelineRuntimeConfigurationError",
    "PipelineRuntimePort",
    "SEMANTIC_ONLY_PLAN",
    "SourceCatalogEntry",
    "compose_pipeline_run_service_from_environment",
    "compose_pipeline_highlight_read_service_from_environment",
    "compose_pipeline_runtime_from_environment",
)
