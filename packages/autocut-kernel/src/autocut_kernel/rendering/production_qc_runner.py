"""Lease-fenced orchestration for deterministic, full-file production QC.

This module owns only collection and durable evidence attachment.  It never
interprets an observation as a pass/fail result and never creates release or
publication authority.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol, cast
from uuid import UUID

from ..registry.installed_production_qc import load_installed_production_qc_resource
from ..store.models import (
    PRODUCTION_RENDER_QC_CHECK_SET_VERSION,
    BlobRef,
    Job,
    MaterializationError,
    MaterializationLimits,
    PersistedProductionRenderQcCollectorCapability,
    ProductionQcCollectorCapabilityBinding,
    ProductionRenderQcAttempt,
    ProductionRenderQcCheckEvidence,
    ProductionRenderQcEvidenceReport,
    ProductionRenderQcLease,
    ProductionRenderQcMeasurement,
    VerifiedMaterializedBlob,
)
from .production_process import (
    FIXED_PROCESS_ENVIRONMENT,
    PinnedExecutable,
    ProductionExecutableError,
    ProductionExecutableIdentity,
    ProductionProcessError,
    ProductionProcessPipe,
    ProductionProcessRunner,
    ProductionProcessSinkError,
    ProductionProcessTimeoutError,
    ProductionStreamingProcessResult,
    copy_pin_executable,
    create_process_pipe,
    probe_executable_version,
    resolve_executable,
    reverify_pinned_executable,
    run_bounded_process,
    run_streaming_process,
)
from .production_qc_collector_capability import (
    ProductionQcCollectorCapabilityRequest,
    ProductionQcCollectorExecutableIdentity,
    ProductionQcCollectorLiveProfile,
)
from .production_qc_collectors import (
    PRODUCTION_QC_COLLECTORS,
    AstatsReducer,
    BoundedStreamReducer,
    CollectionObservation,
    CollectorError,
    CollectorSpec,
    CompactTimelineReducer,
    DetectorIntervalReducer,
    FramehashReducer,
    Measurement,
    MetadataPrintReducer,
    ProgressReducer,
    Topology,
    bind_collector_argv,
    parse_compact_record,
    parse_topology_json,
)
from .production_qc_evaluator import ProductionRenderQcCollectorIdentity

_SHA256_EMPTY: Final = "sha256:" + hashlib.sha256(b"").hexdigest()
_CHECK_EVIDENCE_AUTHORITY_CAP: Final = 2 * 1024 * 1024
_REPORT_EVIDENCE_AUTHORITY_CAP: Final = 16 * 1024 * 1024
_TOPOLOGY_AUTHORITY_CAP: Final = 1024 * 1024
_FILE_CHUNK_BYTES: Final = 1024 * 1024
_LEASE_PROCESS_BUDGET_NUMERATOR: Final = 3
_LEASE_PROCESS_BUDGET_DENOMINATOR: Final = 4
_EVIDENCE_EXAMPLE_CAP: Final = 16


class ProductionRenderQcRunnerError(RuntimeError):
    """Base failure for the production QC orchestration boundary."""


class ProductionRenderQcInputError(ProductionRenderQcRunnerError):
    """The typed runner inputs disagree before any external collection."""


class ProductionRenderQcRetryableError(ProductionRenderQcRunnerError):
    """Retryable infrastructure failure; no evidence report was attached."""


class ProductionRenderQcCancelledError(ProductionRenderQcRetryableError):
    """The caller cancelled collection before durable attachment."""


class ProductionRenderQcIdentityDriftError(ProductionRenderQcRunnerError):
    """Materialized media or pinned executable identity changed during collection."""


class ProductionRenderQcStore(Protocol):
    """Narrow Store port required by the runner; no PostgreSQL dependency leaks in."""

    def resolve_accepted_production_qc_collector_capability(
        self, request: ProductionQcCollectorCapabilityRequest
    ) -> PersistedProductionRenderQcCollectorCapability: ...

    def materialize_immutable_blob(
        self, job: Job, reference: BlobRef, limits: MaterializationLimits
    ) -> VerifiedMaterializedBlob: ...

    def put_immutable_blob(
        self,
        job: Job,
        *,
        content: bytes,
        content_hash: str,
        media_type: str,
    ) -> BlobRef: ...

    def renew_production_render_qc_lease(
        self, lease: ProductionRenderQcLease, *, lease_seconds: int
    ) -> ProductionRenderQcLease: ...

    def record_production_render_qc_evidence(
        self,
        lease: ProductionRenderQcLease,
        report: ProductionRenderQcEvidenceReport,
    ) -> ProductionRenderQcAttempt: ...


class ProductionQcStreamingRunner(Protocol):
    def __call__(
        self,
        argv: tuple[str, ...],
        *,
        timeout_milliseconds: int,
        stdout_diagnostic_max_bytes: int,
        stderr_diagnostic_max_bytes: int,
        progress_diagnostic_max_bytes: int,
        stdout_sink: Callable[[bytes], None] | None,
        stderr_sink: Callable[[bytes], None] | None,
        progress_sink: Callable[[bytes], None] | None,
        progress_pipe: ProductionProcessPipe | None,
        pass_fds: tuple[int, ...],
        environment: Mapping[str, str] | None,
        terminate_on_diagnostic_limit: bool,
    ) -> ProductionStreamingProcessResult: ...


class ProductionRenderQcHeartbeat(Protocol):
    def __call__(self, lease: ProductionRenderQcLease) -> ProductionRenderQcLease: ...


class _Digest(Protocol):
    def update(self, data: bytes, /) -> None: ...

    def hexdigest(self) -> str: ...


@dataclass(frozen=True, slots=True)
class ProductionRenderQcCollectorProfile:
    """Portable, path-free identity of the exact collector/tool profile."""

    profile_id: str
    ffmpeg_identity: ProductionExecutableIdentity
    ffprobe_identity: ProductionExecutableIdentity

    def __post_init__(self) -> None:
        if (
            type(self.profile_id) is not str  # noqa: E721
            or not self.profile_id
            or len(self.profile_id.encode("utf-8")) > 128
            or self.profile_id[0] not in "abcdefghijklmnopqrstuvwxyz0123456789"
            or any(
                character not in "abcdefghijklmnopqrstuvwxyz0123456789._-"
                for character in self.profile_id
            )
        ):
            raise ValueError("production QC collector profile_id is invalid")
        if type(self.ffmpeg_identity) is not ProductionExecutableIdentity:  # noqa: E721
            raise ValueError("production QC profile requires an exact FFmpeg identity")
        if type(self.ffprobe_identity) is not ProductionExecutableIdentity:  # noqa: E721
            raise ValueError("production QC profile requires an exact FFprobe identity")

    @property
    def registry_sha256(self) -> str:
        return _canonical_hash(
            [
                {
                    "argv_sha256": spec.canonical_argv_sha256,
                    "check_id": spec.check_id,
                    "dependencies": list(spec.dependencies),
                    "measurements": [
                        {
                            "name": item.name,
                            "unit": item.unit,
                            "value_kind": item.value_kind,
                        }
                        for item in spec.measurements
                    ],
                    "ordinal": spec.ordinal,
                    "parser_schema_version": spec.parser_schema_version,
                }
                for spec in PRODUCTION_QC_COLLECTORS
            ]
        )

    @property
    def qc_runner_identity_sha256(self) -> str:
        return _canonical_hash(
            {
                "environment": dict(FIXED_PROCESS_ENVIRONMENT),
                "ffmpeg": self.ffmpeg_identity.to_mapping(),
                "ffprobe": self.ffprobe_identity.to_mapping(),
                "profile_id": self.profile_id,
                "registry_sha256": self.registry_sha256,
                "runner_schema_version": "production-qc-runner-v1",
            }
        )


@dataclass(frozen=True, slots=True)
class ProductionRenderQcRuntimeCapability:
    """Data-only accepted projection, rechecked through the composed Store at use.

    Construction is not authorization. No reader, process or mutable policy
    selector belongs to this object. Both identities derive from the same
    persisted tools, independently of any QC report.
    """

    persisted: PersistedProductionRenderQcCollectorCapability

    def __post_init__(self) -> None:
        if type(self.persisted) is not PersistedProductionRenderQcCollectorCapability:  # noqa: E721
            raise ProductionRenderQcInputError("production QC requires an exact persisted capability")

    @property
    def profile(self) -> ProductionRenderQcCollectorProfile:
        live = self.persisted.request.live_profile
        return ProductionRenderQcCollectorProfile(
            live.profile_id,
            _runtime_executable_identity(live.ffmpeg_identity),
            _runtime_executable_identity(live.ffprobe_identity),
        )

    @property
    def evaluator_identity(self) -> ProductionRenderQcCollectorIdentity:
        profile = self.profile
        return ProductionRenderQcCollectorIdentity(
            profile.qc_runner_identity_sha256,
            _canonical_hash(profile.ffmpeg_identity.to_mapping()),
            _canonical_hash(profile.ffprobe_identity.to_mapping()),
        )


def _runtime_executable_identity(
    identity: ProductionQcCollectorExecutableIdentity,
) -> ProductionExecutableIdentity:
    return ProductionExecutableIdentity(
        identity.executable_sha256,
        identity.executable_byte_length,
        identity.version_output_sha256,
    )


def resolve_production_render_qc_runtime_capability(
    store: ProductionRenderQcStore,
    live_profile: ProductionQcCollectorLiveProfile,
) -> ProductionRenderQcRuntimeCapability:
    """Resolve fixed installed policy plus a fresh host measurement, read-only.

    ``store`` is supplied by trusted application composition, never by request
    data or the capability. Store errors propagate fail closed. The exact
    reader owns validation of the succeeded Receipt/set/member joins; this
    adapter also checks its returned projection against the installed binding.
    """

    if type(live_profile) is not ProductionQcCollectorLiveProfile:  # noqa: E721
        raise ProductionRenderQcInputError("production QC requires an exact live profile")
    installed = load_installed_production_qc_resource()
    request = ProductionQcCollectorCapabilityRequest(installed.policy, live_profile)
    binding = ProductionQcCollectorCapabilityBinding(request, installed.provenance)
    persisted = store.resolve_accepted_production_qc_collector_capability(request)
    if type(persisted) is not PersistedProductionRenderQcCollectorCapability:  # noqa: E721
        raise ProductionRenderQcInputError("production QC Store returned no exact capability")
    if (
        persisted.request != request
        or persisted.provenance != installed.provenance
        or persisted.scope_key != binding.scope_key
        or persisted.measurement_member_sha256 != binding.measurement_member.content_hash
        or persisted.capability_member_sha256 != binding.decision_member.content_hash
    ):
        raise ProductionRenderQcInputError("production QC accepted capability binding differs")
    for reference in (
        persisted.receipt_id, persisted.artifact_set_id, persisted.command_slot_id
    ):
        if type(reference) is not UUID or reference.int == 0:  # noqa: E721
            raise ProductionRenderQcInputError("production QC capability reference is invalid")
    if persisted.accepted_at.utcoffset() is None:
        raise ProductionRenderQcInputError("production QC capability acceptance time is invalid")
    capability = ProductionRenderQcRuntimeCapability(persisted)
    # SQL0059 retains the full LiveProfile hash. Attempts/reports deliberately
    # retain the historical compact runner hash; these hashes are not equal.
    if (
        capability.profile.registry_sha256 != installed.policy.collector_registry_sha256
        or _canonical_hash(dict(FIXED_PROCESS_ENVIRONMENT))
        != installed.policy.fixed_environment_sha256
    ):
        raise ProductionRenderQcInputError("production QC installed collector identity differs")
    return capability


@dataclass(frozen=True, slots=True)
class ProductionRenderQcExecutionLimits:
    """Explicit host execution and evidence ceilings for one leased run."""

    process_timeout_milliseconds: int
    tool_probe_timeout_milliseconds: int
    tool_version_max_bytes: int
    diagnostic_max_bytes: int
    topology_max_bytes: int
    evidence_max_bytes: int
    aggregate_evidence_max_bytes: int
    lease_seconds: int
    renew_every_operations: int = 4

    def __post_init__(self) -> None:
        for name in (
            "process_timeout_milliseconds",
            "tool_probe_timeout_milliseconds",
            "tool_version_max_bytes",
            "diagnostic_max_bytes",
            "topology_max_bytes",
            "evidence_max_bytes",
            "aggregate_evidence_max_bytes",
            "lease_seconds",
            "renew_every_operations",
        ):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:  # noqa: E721
                raise ValueError(f"production QC {name} must be a positive integer")
        if self.topology_max_bytes > _TOPOLOGY_AUTHORITY_CAP:
            raise ValueError("production QC topology cap exceeds authority")
        if not 1024 <= self.evidence_max_bytes <= _CHECK_EVIDENCE_AUTHORITY_CAP:
            raise ValueError("production QC evidence cap is outside authority")
        if self.aggregate_evidence_max_bytes > _REPORT_EVIDENCE_AUTHORITY_CAP:
            raise ValueError("production QC aggregate evidence cap exceeds authority")
        if self.aggregate_evidence_max_bytes < 12 * 1024:
            raise ValueError("production QC aggregate cap cannot contain minimal check evidence")
        if not 1 <= self.lease_seconds <= 3600:
            raise ValueError("production QC lease_seconds is outside Store authority")
        # A process is intentionally not allowed to consume an entire lease.  The
        # runner renews immediately before each external operation, then retains
        # a scheduler/cleanup reserve so a late return cannot attach using an
        # expired fence token.
        max_process_milliseconds = (
            self.lease_seconds
            * 1000
            * _LEASE_PROCESS_BUDGET_NUMERATOR
            // _LEASE_PROCESS_BUDGET_DENOMINATOR
        )
        if (
            self.process_timeout_milliseconds > max_process_milliseconds
            or self.tool_probe_timeout_milliseconds > max_process_milliseconds
        ):
            raise ValueError(
                "production QC external process timeout exceeds its lease safety budget"
            )


@dataclass(frozen=True, slots=True)
class ProductionRenderQcPlanProjection:
    """Minimal exact Stage-4/renderer projection needed by collection-only QC."""

    render_facts_sha256: str
    junction_timeline_ticks: tuple[int, ...]

    def __post_init__(self) -> None:
        _require_sha256(self.render_facts_sha256, "plan projection render_facts_sha256")
        if (
            type(self.junction_timeline_ticks) is not tuple  # noqa: E721
            or any(type(value) is not int or value < 0 for value in self.junction_timeline_ticks)
            or tuple(sorted(self.junction_timeline_ticks)) != self.junction_timeline_ticks
            or len(set(self.junction_timeline_ticks)) != len(self.junction_timeline_ticks)
        ):
            raise ValueError("production QC junction projection must be sorted unique ticks")


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    device: int
    inode: int
    size: int
    mode: int
    sha256: str


@dataclass(slots=True)
class _BytesSink:
    maximum: int
    value: bytearray

    @classmethod
    def create(cls, maximum: int) -> _BytesSink:
        return cls(maximum, bytearray())

    def feed(self, data: bytes) -> None:
        if len(self.value) + len(data) > self.maximum:
            raise CollectorError("bounded collector output exceeds its cap")
        self.value.extend(data)


@dataclass(slots=True)
class _StreamSummary:
    digest: _Digest
    byte_length: int = 0

    @classmethod
    def create(cls) -> _StreamSummary:
        return cls(hashlib.sha256())

    def feed(self, data: bytes) -> None:
        self.digest.update(data)
        self.byte_length += len(data)

    @property
    def sha256(self) -> str:
        return "sha256:" + self.digest.hexdigest()


def _bounded_examples(*groups: Sequence[str]) -> tuple[str, ...]:
    """Merge per-stream reducer examples without retaining unbounded media data."""

    values = tuple(item for group in groups for item in group)
    if len(values) <= _EVIDENCE_EXAMPLE_CAP:
        return values
    half = _EVIDENCE_EXAMPLE_CAP // 2
    return (*values[:half], *values[-half:])


class _FrameTimelineReducer:
    """Add exact audio sample accounting to the shared strict frame reducer."""

    def __init__(self, indexes: Sequence[int]) -> None:
        self.timeline = CompactTimelineReducer("frame", indexes)
        self._tail = b""
        self.sample_count = 0

    def feed(self, data: bytes) -> None:
        self.timeline.feed(data)
        self._tail += data
        while b"\n" in self._tail:
            raw, self._tail = self._tail.split(b"\n", 1)
            if not raw:
                continue
            try:
                record = parse_compact_record(raw.decode("utf-8", "strict"))
            except UnicodeError as error:
                raise CollectorError("frame timeline is not UTF-8") from error
            samples = record.get("nb_samples")
            if samples not in (None, "N/A"):
                if not samples.isascii() or not samples.isdecimal():
                    raise CollectorError("frame timeline nb_samples is malformed")
                self.sample_count += int(samples)

    def complete(self) -> None:
        if self._tail:
            raise CollectorError("frame timeline is truncated")
        self.timeline.complete()


@dataclass(slots=True)
class _LeaseController:
    store: ProductionRenderQcStore
    lease: ProductionRenderQcLease
    limits: ProductionRenderQcExecutionLimits
    heartbeat: ProductionRenderQcHeartbeat | None
    cancelled: Callable[[], bool] | None
    operations: int = 0

    def checkpoint(self, *, force_renewal: bool = False) -> None:
        if self.cancelled is not None and self.cancelled():
            raise ProductionRenderQcCancelledError("production QC collection was cancelled")
        self.operations += 1
        if force_renewal or self.operations % self.limits.renew_every_operations == 0:
            if self.heartbeat is None:
                self.lease = self.store.renew_production_render_qc_lease(
                    self.lease, lease_seconds=self.limits.lease_seconds
                )
            else:
                self.lease = self.heartbeat(self.lease)
            if type(self.lease) is not ProductionRenderQcLease:  # noqa: E721
                raise ProductionRenderQcInputError("QC heartbeat returned an invalid lease")


@dataclass(slots=True)
class _CheckpointStreamingRunner:
    delegate: ProductionQcStreamingRunner
    controller: _LeaseController

    def __call__(
        self,
        argv: tuple[str, ...],
        *,
        timeout_milliseconds: int,
        stdout_diagnostic_max_bytes: int,
        stderr_diagnostic_max_bytes: int,
        progress_diagnostic_max_bytes: int,
        stdout_sink: Callable[[bytes], None] | None,
        stderr_sink: Callable[[bytes], None] | None,
        progress_sink: Callable[[bytes], None] | None,
        progress_pipe: ProductionProcessPipe | None,
        pass_fds: tuple[int, ...],
        environment: Mapping[str, str] | None,
        terminate_on_diagnostic_limit: bool,
    ) -> ProductionStreamingProcessResult:
        # The concrete process runner owns the pipe once called.  Renew before
        # every bounded external scan, rather than merely every N Python calls:
        # a single full-file FFmpeg/FFprobe pass can be the longest operation.
        self.controller.checkpoint(force_renewal=True)
        return self.delegate(
            argv,
            timeout_milliseconds=timeout_milliseconds,
            stdout_diagnostic_max_bytes=stdout_diagnostic_max_bytes,
            stderr_diagnostic_max_bytes=stderr_diagnostic_max_bytes,
            progress_diagnostic_max_bytes=progress_diagnostic_max_bytes,
            stdout_sink=stdout_sink,
            stderr_sink=stderr_sink,
            progress_sink=progress_sink,
            progress_pipe=progress_pipe,
            pass_fds=pass_fds,
            environment=environment,
            terminate_on_diagnostic_limit=terminate_on_diagnostic_limit,
        )


@dataclass(slots=True)
class _RunnerState:
    topology: Topology | None = None
    decoded_frame_record_count: int = 0


def run_production_render_qc(
    store: ProductionRenderQcStore,
    *,
    job: Job,
    attempt: ProductionRenderQcAttempt,
    lease: ProductionRenderQcLease,
    plan: ProductionRenderQcPlanProjection,
    materialization_limits: MaterializationLimits,
    ffmpeg_path: str | os.PathLike[str],
    ffprobe_path: str | os.PathLike[str],
    capability: ProductionRenderQcRuntimeCapability,
    execution_limits: ProductionRenderQcExecutionLimits,
    heartbeat: ProductionRenderQcHeartbeat | None = None,
    cancelled: Callable[[], bool] | None = None,
    streaming_runner: ProductionQcStreamingRunner = run_streaming_process,
    bounded_runner: ProductionProcessRunner = run_bounded_process,
    executable_reverifier: Callable[[PinnedExecutable], None] = reverify_pinned_executable,
) -> ProductionRenderQcAttempt:
    """Collect and atomically attach one complete ordered evidence journal."""

    if type(capability) is not ProductionRenderQcRuntimeCapability:  # noqa: E721
        raise ProductionRenderQcInputError("production QC requires an exact runtime capability")
    # Re-read before materialization, lease renewal, probing, process startup or
    # evidence writes. Never call a reader obtained from the supplied capability.
    fresh = resolve_production_render_qc_runtime_capability(
        store, capability.persisted.request.live_profile
    )
    if fresh.persisted != capability.persisted:
        raise ProductionRenderQcInputError("production QC supplied capability differs from Store")
    profile = fresh.profile
    _validate_inputs(job, attempt, lease, plan, materialization_limits, profile, execution_limits)
    controller = _LeaseController(store, lease, execution_limits, heartbeat, cancelled)
    materialized: VerifiedMaterializedBlob | None = None
    verified_media_identity: _FileIdentity | None = None
    pinned: list[PinnedExecutable] = []
    temporary: tempfile.TemporaryDirectory[str] | None = None
    primary_error: BaseException | None = None
    try:
        controller.checkpoint(force_renewal=True)
        try:
            materialized = store.materialize_immutable_blob(
                job, attempt.output_blob, materialization_limits
            )
        except MaterializationError as error:
            if error.code not in {
                "COMMITTED_SOURCE_BLOB_INTEGRITY_FAILED",
                "MEDIA_SOURCE_BYTE_LIMIT_EXCEEDED",
            }:
                raise ProductionRenderQcRetryableError(
                    "production QC materialization infrastructure is unavailable"
                ) from error
            observations = _dependency_blocked_observations(
                _diagnostic_code(error.code), first_status="incomplete"
            )
            return _persist_observations(store, job, attempt, controller, profile, observations)
        if materialized.reference != attempt.output_blob or not materialized.path.is_absolute():
            raise ProductionRenderQcInputError(
                "materialization did not return the exact requested BlobRef"
            )

        initial = _scan_file_identity(materialized.path)
        identity_matches = (
            stat.S_ISREG(initial.mode)
            and initial.size == attempt.output_blob.byte_length
            and initial.sha256 == attempt.output_blob.content_hash
        )
        observations: list[CollectionObservation] = [
            _identity_observation(PRODUCTION_QC_COLLECTORS[0], initial)
        ]
        if not identity_matches:
            # The descriptor scan itself finished, but it was not a scan of
            # the immutable bytes promised by the QC attempt.  Never label
            # substitute bytes as full-file coverage of that BlobRef.
            observations[0] = _incomplete(
                PRODUCTION_QC_COLLECTORS[0], b"", "exact_object_identity_mismatch"
            )
            observations.extend(
                _not_run(spec, "dependency_failed") for spec in PRODUCTION_QC_COLLECTORS[1:]
            )
            return _persist_observations(store, job, attempt, controller, profile, observations)
        verified_media_identity = initial

        temporary = tempfile.TemporaryDirectory(prefix="autocut-production-qc-")
        attempt_directory = Path(temporary.name).resolve(strict=True)
        controller.checkpoint(force_renewal=True)
        ffmpeg, ffmpeg_identity = _pin_tool(
            ffmpeg_path,
            "ffmpeg",
            attempt_directory / "ffmpeg",
            bounded_runner,
            execution_limits,
        )
        pinned.append(ffmpeg)
        controller.checkpoint(force_renewal=True)
        ffprobe, ffprobe_identity = _pin_tool(
            ffprobe_path,
            "ffprobe",
            attempt_directory / "ffprobe",
            bounded_runner,
            execution_limits,
        )
        pinned.append(ffprobe)
        if (
            ffmpeg_identity != profile.ffmpeg_identity
            or ffprobe_identity != profile.ffprobe_identity
        ):
            raise ProductionRenderQcRetryableError(
                "pinned production QC tool identity differs from the reserved profile"
            )

        state = _RunnerState()
        checked_streaming_runner = _CheckpointStreamingRunner(streaming_runner, controller)
        for spec in PRODUCTION_QC_COLLECTORS[1:]:
            controller.checkpoint()
            observations.append(
                _collect_check(
                    spec,
                    materialized.path,
                    ffmpeg,
                    ffprobe,
                    observations,
                    state,
                    plan,
                    checked_streaming_runner,
                    execution_limits,
                )
            )

        final_identity = _scan_file_identity(materialized.path)
        if final_identity != initial:
            raise ProductionRenderQcIdentityDriftError(
                "materialized production QC media identity changed during collection"
            )
        for executable in pinned:
            executable_reverifier(executable)
        return _persist_observations(store, job, attempt, controller, profile, observations)
    except ProductionProcessTimeoutError as error:
        primary_error = error
        raise ProductionRenderQcRetryableError(
            "production QC collector exceeded its timeout"
        ) from error
    except ProductionExecutableError as error:
        primary_error = error
        raise ProductionRenderQcRetryableError(
            "production QC executable could not be established"
        ) from error
    except BaseException as error:
        primary_error = error
        raise
    finally:
        cleanup_error: BaseException | None = None
        for executable in pinned:
            try:
                executable_reverifier(executable)
            except BaseException as error:
                cleanup_error = cleanup_error or error
        if materialized is not None:
            if verified_media_identity is not None:
                try:
                    if _scan_file_identity(materialized.path) != verified_media_identity:
                        raise ProductionRenderQcIdentityDriftError(
                            "materialized production QC media changed before cleanup"
                        )
                except BaseException as error:
                    cleanup_error = cleanup_error or error
            try:
                materialized.close()
            except BaseException as error:
                cleanup_error = cleanup_error or error
        if temporary is not None:
            try:
                temporary.cleanup()
            except BaseException as error:
                cleanup_error = cleanup_error or error
        if cleanup_error is not None:
            drift = ProductionRenderQcIdentityDriftError(
                "production QC cleanup or identity reverification failed"
            )
            if primary_error is not None:
                drift.add_note("cleanup identity failure replaced an earlier production QC failure")
            raise drift from cleanup_error


def _validate_inputs(
    job: Job,
    attempt: ProductionRenderQcAttempt,
    lease: ProductionRenderQcLease,
    plan: ProductionRenderQcPlanProjection,
    materialization_limits: MaterializationLimits,
    profile: ProductionRenderQcCollectorProfile,
    execution_limits: ProductionRenderQcExecutionLimits,
) -> None:
    if type(job) is not Job:  # noqa: E721
        raise ProductionRenderQcInputError("production QC requires an exact Job")
    if type(attempt) is not ProductionRenderQcAttempt:  # noqa: E721
        raise ProductionRenderQcInputError("production QC requires an exact attempt")
    if type(lease) is not ProductionRenderQcLease:  # noqa: E721
        raise ProductionRenderQcInputError("production QC requires an exact lease")
    if type(plan) is not ProductionRenderQcPlanProjection:  # noqa: E721
        raise ProductionRenderQcInputError("production QC requires an exact plan projection")
    if type(materialization_limits) is not MaterializationLimits:  # noqa: E721
        raise ProductionRenderQcInputError("production QC requires exact materialization limits")
    if type(profile) is not ProductionRenderQcCollectorProfile:  # noqa: E721
        raise ProductionRenderQcInputError("production QC requires an exact collector profile")
    if type(execution_limits) is not ProductionRenderQcExecutionLimits:  # noqa: E721
        raise ProductionRenderQcInputError("production QC requires exact execution limits")
    if attempt.state != "scanning" or attempt.version != lease.version:
        raise ProductionRenderQcInputError("production QC attempt and lease are not active")
    if (
        attempt.qc_attempt_id != lease.qc_attempt_id
        or attempt.render_attempt_id != lease.render_attempt_id
        or attempt.job_id != lease.job_id
        or attempt.command_slot_id != lease.command_slot_id
    ):
        raise ProductionRenderQcInputError("production QC attempt and lease identities differ")
    if attempt.render_facts_sha256 != plan.render_facts_sha256:
        raise ProductionRenderQcInputError("production QC plan projection is not exact")
    if attempt.required_check_set_version != PRODUCTION_RENDER_QC_CHECK_SET_VERSION:
        raise ProductionRenderQcInputError("production QC check-set version is unsupported")
    if attempt.qc_runner_identity_sha256 != profile.qc_runner_identity_sha256:
        raise ProductionRenderQcInputError("production QC runner identity is not reserved")


def _pin_tool(
    selected: str | os.PathLike[str],
    default_name: str,
    destination: Path,
    runner: ProductionProcessRunner,
    limits: ProductionRenderQcExecutionLimits,
) -> tuple[PinnedExecutable, ProductionExecutableIdentity]:
    source = resolve_executable(selected, default_name=default_name)
    pinned = copy_pin_executable(source, destination)
    version = probe_executable_version(
        pinned,
        runner=runner,
        timeout_milliseconds=limits.tool_probe_timeout_milliseconds,
        max_bytes=limits.tool_version_max_bytes,
    )
    return (
        pinned,
        ProductionExecutableIdentity(pinned.sha256, pinned.byte_length, version.output_sha256),
    )


def _scan_file_identity(path: Path) -> _FileIdentity:
    descriptor: int | None = None
    try:
        before = path.lstat()
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        opened = os.fstat(descriptor)
        digest = hashlib.sha256()
        length = 0
        while chunk := os.read(descriptor, _FILE_CHUNK_BYTES):
            digest.update(chunk)
            length += len(chunk)
        after = os.fstat(descriptor)
        final = path.lstat()
    except OSError as error:
        raise ProductionRenderQcIdentityDriftError(
            "materialized production QC media is not a stable regular file"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    stable = (
        stat.S_ISREG(opened.st_mode)
        and opened.st_nlink == 1
        and length == opened.st_size
        and (before.st_dev, before.st_ino, before.st_size, before.st_mode)
        == (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mode)
        == (after.st_dev, after.st_ino, after.st_size, after.st_mode)
        == (final.st_dev, final.st_ino, final.st_size, final.st_mode)
    )
    return _FileIdentity(
        opened.st_dev,
        opened.st_ino,
        length,
        opened.st_mode if stable else 0,
        "sha256:" + digest.hexdigest(),
    )


def _identity_observation(spec: CollectorSpec, identity: _FileIdentity) -> CollectionObservation:
    return CollectionObservation.completed(
        spec,
        {
            "file_byte_length": str(identity.size),
            "file_sha256": identity.sha256,
            "regular_file": "true" if stat.S_ISREG(identity.mode) else "false",
            "stable_file_identity": "true" if identity.mode != 0 else "false",
        },
        stream_byte_length=identity.size,
        stream_sha256=identity.sha256,
        record_count=1,
    )


def _collect_check(
    spec: CollectorSpec,
    exact_output: Path,
    ffmpeg: PinnedExecutable,
    ffprobe: PinnedExecutable,
    prior: Sequence[CollectionObservation],
    state: _RunnerState,
    plan: ProductionRenderQcPlanProjection,
    runner: ProductionQcStreamingRunner,
    limits: ProductionRenderQcExecutionLimits,
) -> CollectionObservation:
    topology = state.topology
    if spec.check_id == "container_stream_topology":
        observation, parsed = _collect_topology(spec, exact_output, ffprobe, runner, limits)
        if observation.collection_status == "completed":
            state.topology = parsed
        return observation
    if topology is None:
        return _not_run(spec, "dependency_failed")

    video_indexes = topology.indexes("video")
    audio_indexes = topology.indexes("audio")
    if spec.check_id == "packet_timeline_integrity":
        return _collect_timeline(spec, exact_output, ffprobe, topology, False, runner, limits)
    if spec.check_id == "decoded_frame_timeline":
        observation = _collect_timeline(spec, exact_output, ffprobe, topology, True, runner, limits)
        if observation.collection_status == "completed":
            state.decoded_frame_record_count = observation.record_count
        return observation
    if spec.check_id == "full_video_decode":
        if not video_indexes:
            return _not_applicable(spec)
        return _collect_decode(spec, exact_output, ffmpeg, video_indexes, runner, limits)
    if spec.check_id == "full_audio_decode":
        if not audio_indexes:
            return _not_applicable(spec)
        return _collect_decode(spec, exact_output, ffmpeg, audio_indexes, runner, limits)
    if spec.check_id in {"video_black_intervals", "video_freeze_intervals"}:
        if not video_indexes:
            return _not_applicable(spec)
        if not _dependency_completed(spec, prior):
            return _not_run(spec, "dependency_failed")
        detector = "black" if spec.check_id == "video_black_intervals" else "freeze"
        return _collect_detector(
            spec, exact_output, ffmpeg, video_indexes, detector, topology, runner, limits
        )
    if spec.check_id == "audio_silence_intervals":
        if not audio_indexes:
            return _not_applicable(spec)
        if not _dependency_completed(spec, prior):
            return _not_run(spec, "dependency_failed")
        return _collect_detector(
            spec, exact_output, ffmpeg, audio_indexes, "silence", topology, runner, limits
        )
    if spec.check_id == "audio_sample_health":
        if not audio_indexes:
            return _not_applicable(spec)
        if not _dependency_completed(spec, prior):
            return _not_run(spec, "dependency_failed")
        return _collect_astats(spec, exact_output, ffmpeg, topology, runner, limits)
    if spec.check_id == "av_presentation_envelope":
        if not video_indexes or not audio_indexes:
            return _not_applicable(spec)
        if not _dependency_completed(spec, prior):
            return _not_run(spec, "dependency_failed")
        return CollectionObservation.completed(
            spec,
            {
                "audio_stream_count": str(len(audio_indexes)),
                "video_stream_count": str(len(video_indexes)),
            },
            stream_byte_length=0,
            stream_sha256=_SHA256_EMPTY,
            record_count=state.decoded_frame_record_count,
        )
    if spec.check_id == "edit_junction_continuity":
        if not plan.junction_timeline_ticks:
            return _not_applicable(spec)
        if not _dependency_completed(spec, prior):
            return _not_run(spec, "dependency_failed")
        return CollectionObservation.completed(
            spec,
            {
                "junction_count": str(len(plan.junction_timeline_ticks)),
                "observation_count": str(state.decoded_frame_record_count),
            },
            stream_byte_length=0,
            stream_sha256=_SHA256_EMPTY,
            record_count=state.decoded_frame_record_count,
        )
    raise ProductionRenderQcInputError("production QC registry contains an unknown check")


def _collect_topology(
    spec: CollectorSpec,
    exact_output: Path,
    ffprobe: PinnedExecutable,
    runner: ProductionQcStreamingRunner,
    limits: ProductionRenderQcExecutionLimits,
) -> tuple[CollectionObservation, Topology | None]:
    sink = _BytesSink.create(limits.topology_max_bytes)
    argv = bind_collector_argv(spec, exact_output=str(exact_output))
    argv = (str(ffprobe.path), *argv[1:])
    try:
        result = _run_process(runner, argv, limits, stdout_sink=sink.feed)
        if result.returncode != 0:
            return _incomplete(spec, sink.value, "process_exit_nonzero"), None
        topology = parse_topology_json(bytes(sink.value), max_bytes=limits.topology_max_bytes)
    except CollectorError:
        return _incomplete(spec, sink.value, "parser_error"), None
    return (
        CollectionObservation.completed(
            spec,
            {
                "audio_stream_count": str(len(topology.indexes("audio"))),
                "stream_count": str(len(topology.streams)),
                "video_stream_count": str(len(topology.indexes("video"))),
            },
            stream_byte_length=len(sink.value),
            stream_sha256=_hash_bytes(sink.value),
            record_count=len(topology.streams),
        ),
        topology,
    )


def _collect_timeline(
    spec: CollectorSpec,
    exact_output: Path,
    ffprobe: PinnedExecutable,
    topology: Topology,
    frames: bool,
    runner: ProductionQcStreamingRunner,
    limits: ProductionRenderQcExecutionLimits,
) -> CollectionObservation:
    indexes = tuple(stream.index for stream in topology.streams)
    if not indexes:
        values = (
            {"frame_count": "0", "sample_count": "0", "timestamp_anomaly_count": "0"}
            if frames
            else {"packet_count": "0", "stream_count": "0", "timestamp_anomaly_count": "0"}
        )
        return CollectionObservation.completed(
            spec, values, stream_byte_length=0, stream_sha256=_SHA256_EMPTY, record_count=0
        )
    reducer: CompactTimelineReducer | _FrameTimelineReducer
    reducer = (
        _FrameTimelineReducer(indexes) if frames else CompactTimelineReducer("packet", indexes)
    )
    base: CompactTimelineReducer = (
        reducer.timeline if isinstance(reducer, _FrameTimelineReducer) else reducer
    )
    try:
        for index in indexes:
            argv = bind_collector_argv(spec, exact_output=str(exact_output), stream_index=index)
            result = _run_process(
                runner, (str(ffprobe.path), *argv[1:]), limits, stdout_sink=reducer.feed
            )
            if result.returncode != 0:
                return _incomplete_from_reducer(spec, base, "process_exit_nonzero")
        reducer.complete()
    except CollectorError:
        return _incomplete_from_reducer(spec, base, "parser_error")
    base_record_count = cast(int, base.record_count)
    if isinstance(reducer, _FrameTimelineReducer):
        values = {
            "frame_count": str(base_record_count),
            "sample_count": str(reducer.sample_count),
            "timestamp_anomaly_count": str(base.timestamp_anomaly_count),
        }
    else:
        values = {
            "packet_count": str(base_record_count),
            "stream_count": str(len(indexes)),
            "timestamp_anomaly_count": str(base.timestamp_anomaly_count),
        }
    return CollectionObservation.completed(
        spec,
        values,
        stream_byte_length=base.stream_byte_length,
        stream_sha256=base.stream_sha256,
        record_count=base_record_count,
        examples=base.examples,
    )


def _collect_decode(
    spec: CollectorSpec,
    exact_output: Path,
    ffmpeg: PinnedExecutable,
    indexes: tuple[int, ...],
    runner: ProductionQcStreamingRunner,
    limits: ProductionRenderQcExecutionLimits,
) -> CollectionObservation:
    summary = _StreamSummary.create()
    progress_summary = _StreamSummary.create()
    total_rows = 0
    examples: list[tuple[str, ...]] = []
    try:
        for index in indexes:
            reducer = FramehashReducer()
            progress = ProgressReducer()
            pipe = create_process_pipe()
            # Ownership transfers to the streaming runner.  It closes both
            # descriptors on all paths; a caller must never close them again
            # because a reused numeric FD could belong to an unrelated process.
            argv = bind_collector_argv(
                spec,
                exact_output=str(exact_output),
                stream_index=index,
                progress_fd=pipe.write_fd,
            )
            result = _run_process(
                runner,
                (str(ffmpeg.path), *argv[1:]),
                limits,
                stdout_sink=_fanout(reducer.feed, summary.feed),
                progress_sink=_fanout(progress.feed, progress_summary.feed),
                progress_pipe=pipe,
            )
            if result.returncode != 0:
                return _incomplete_summary(
                    spec,
                    summary,
                    total_rows,
                    "process_exit_nonzero",
                    examples=_bounded_examples(*examples, reducer.examples),
                    progress_summary=progress_summary,
                )
            progress.complete()
            if reducer.row_count == 0:
                return _incomplete_summary(
                    spec,
                    summary,
                    total_rows,
                    "empty_mapped_decode",
                    examples=_bounded_examples(*examples, reducer.examples),
                    progress_summary=progress_summary,
                )
            reducer.complete()
            # ``progress=end`` establishes process completion.  The explicit
            # framehash rows above establish per-stream mapped output; FFmpeg's
            # ``frame=`` progress field is not meaningful for audio-only maps.
            total_rows += reducer.row_count
            examples.append(reducer.examples)
    except CollectorError:
        return _incomplete_summary(
            spec,
            summary,
            total_rows,
            "parser_error",
            examples=_bounded_examples(*examples),
            progress_summary=progress_summary,
        )
    count_name = (
        "video_stream_count" if spec.check_id == "full_video_decode" else "audio_stream_count"
    )
    return CollectionObservation.completed(
        spec,
        {count_name: str(len(indexes)), "framehash_row_count": str(total_rows)},
        stream_byte_length=summary.byte_length,
        stream_sha256=summary.sha256,
        record_count=total_rows,
        examples=_bounded_examples(*examples),
        progress_stream_byte_length=progress_summary.byte_length,
        progress_stream_sha256=progress_summary.sha256,
    )


def _collect_detector(
    spec: CollectorSpec,
    exact_output: Path,
    ffmpeg: PinnedExecutable,
    indexes: tuple[int, ...],
    detector_name: str,
    topology: Topology,
    runner: ProductionQcStreamingRunner,
    limits: ProductionRenderQcExecutionLimits,
) -> CollectionObservation:
    summary = _StreamSummary.create()
    progress_summary = _StreamSummary.create()
    interval_count = 0
    censored_count = 0
    record_count = 0
    examples: list[tuple[str, ...]] = []
    try:
        for index in indexes:
            detector = DetectorIntervalReducer(detector_name)  # type: ignore[arg-type]
            metadata = MetadataPrintReducer(detector)
            progress = ProgressReducer()
            pipe = create_process_pipe()
            argv = bind_collector_argv(
                spec,
                exact_output=str(exact_output),
                stream_index=index,
                metadata_fd=1,
                progress_fd=pipe.write_fd,
            )
            result = _run_process(
                runner,
                (str(ffmpeg.path), *argv[1:]),
                limits,
                stdout_sink=_fanout(metadata.feed, summary.feed),
                progress_sink=_fanout(progress.feed, progress_summary.feed),
                progress_pipe=pipe,
            )
            if result.returncode != 0:
                return _incomplete_summary(
                    spec,
                    summary,
                    record_count,
                    "process_exit_nonzero",
                    examples=_bounded_examples(*examples, detector.examples),
                    progress_summary=progress_summary,
                )
            metadata.complete()
            progress.complete()
            interval_count += len(detector.channel_intervals)
            censored_count += detector.right_censored_count
            record_count += cast(int, metadata.record_count)
            examples.append(detector.examples)
    except CollectorError:
        return _incomplete_summary(
            spec,
            summary,
            record_count,
            "parser_error",
            examples=_bounded_examples(*examples),
            progress_summary=progress_summary,
        )
    values = {
        "interval_count": str(interval_count),
        "right_censored_interval_count": str(censored_count),
    }
    if detector_name == "silence":
        values["channel_count"] = str(
            sum(stream.channels or 0 for stream in topology.streams if stream.index in indexes)
        )
    return CollectionObservation.completed(
        spec,
        values,
        stream_byte_length=summary.byte_length,
        stream_sha256=summary.sha256,
        record_count=record_count,
        examples=_bounded_examples(*examples),
        progress_stream_byte_length=progress_summary.byte_length,
        progress_stream_sha256=progress_summary.sha256,
    )


def _collect_astats(
    spec: CollectorSpec,
    exact_output: Path,
    ffmpeg: PinnedExecutable,
    topology: Topology,
    runner: ProductionQcStreamingRunner,
    limits: ProductionRenderQcExecutionLimits,
) -> CollectionObservation:
    summary = _StreamSummary.create()
    progress_summary = _StreamSummary.create()
    channel_count = 0
    snapshot_count = 0
    nonfinite_count = 0
    record_count = 0
    examples: list[tuple[str, ...]] = []
    try:
        for stream in (item for item in topology.streams if item.codec_type == "audio"):
            channels = stream.channels or 0
            astats = AstatsReducer(channels)
            metadata = MetadataPrintReducer(astats)
            progress = ProgressReducer()
            pipe = create_process_pipe()
            argv = bind_collector_argv(
                spec,
                exact_output=str(exact_output),
                stream_index=stream.index,
                metadata_fd=1,
                progress_fd=pipe.write_fd,
            )
            result = _run_process(
                runner,
                (str(ffmpeg.path), *argv[1:]),
                limits,
                stdout_sink=_fanout(metadata.feed, summary.feed),
                progress_sink=_fanout(progress.feed, progress_summary.feed),
                progress_pipe=pipe,
            )
            if result.returncode != 0:
                return _incomplete_summary(
                    spec,
                    summary,
                    record_count,
                    "process_exit_nonzero",
                    examples=_bounded_examples(*examples, astats.examples),
                    progress_summary=progress_summary,
                )
            metadata.complete()
            progress.complete()
            channel_count += channels
            snapshot_count += astats.snapshot_count
            nonfinite_count += astats.nonfinite_value_count
            record_count += cast(int, metadata.record_count)
            examples.append(astats.examples)
    except CollectorError:
        return _incomplete_summary(
            spec,
            summary,
            record_count,
            "parser_error",
            examples=_bounded_examples(*examples),
            progress_summary=progress_summary,
        )
    return CollectionObservation.completed(
        spec,
        {
            "channel_count": str(channel_count),
            "nonfinite_value_count": str(nonfinite_count),
            "snapshot_count": str(snapshot_count),
        },
        stream_byte_length=summary.byte_length,
        stream_sha256=summary.sha256,
        record_count=record_count,
        examples=_bounded_examples(*examples),
        progress_stream_byte_length=progress_summary.byte_length,
        progress_stream_sha256=progress_summary.sha256,
    )


def _run_process(
    runner: ProductionQcStreamingRunner,
    argv: tuple[str, ...],
    limits: ProductionRenderQcExecutionLimits,
    *,
    stdout_sink: Callable[[bytes], None] | None,
    progress_sink: Callable[[bytes], None] | None = None,
    progress_pipe: ProductionProcessPipe | None = None,
) -> ProductionStreamingProcessResult:
    if any(item in {"-ss", "-t", "-to"} for item in argv):
        raise ProductionRenderQcInputError("production QC scans must cover the full file")
    try:
        return runner(
            argv,
            timeout_milliseconds=limits.process_timeout_milliseconds,
            stdout_diagnostic_max_bytes=limits.diagnostic_max_bytes,
            stderr_diagnostic_max_bytes=limits.diagnostic_max_bytes,
            progress_diagnostic_max_bytes=limits.diagnostic_max_bytes,
            stdout_sink=stdout_sink,
            stderr_sink=None,
            progress_sink=progress_sink,
            progress_pipe=progress_pipe,
            pass_fds=(),
            environment=FIXED_PROCESS_ENVIRONMENT,
            terminate_on_diagnostic_limit=False,
        )
    except ProductionProcessSinkError as error:
        cause: BaseException | None = error
        while cause is not None:
            if isinstance(cause, CollectorError):
                raise cause
            cause = cause.__cause__
        raise ProductionRenderQcRetryableError(
            "production QC process sink failed outside the parser"
        ) from error
    except ProductionProcessTimeoutError:
        raise
    except ProductionProcessError as error:
        raise ProductionRenderQcRetryableError(
            "production QC process infrastructure failed"
        ) from error
    except OSError as error:
        raise ProductionRenderQcRetryableError(
            "production QC process could not be started"
        ) from error


def _persist_observations(
    store: ProductionRenderQcStore,
    job: Job,
    attempt: ProductionRenderQcAttempt,
    controller: _LeaseController,
    profile: ProductionRenderQcCollectorProfile,
    observations: Sequence[CollectionObservation],
) -> ProductionRenderQcAttempt:
    if len(observations) != len(PRODUCTION_QC_COLLECTORS):
        raise ProductionRenderQcInputError("production QC observations are incomplete")
    checks: list[ProductionRenderQcCheckEvidence] = []
    aggregate = 0
    for spec, observation in zip(PRODUCTION_QC_COLLECTORS, observations, strict=True):
        controller.checkpoint(force_renewal=True)
        content = _evidence_bytes(spec, observation, profile)
        if len(content) > controller.limits.evidence_max_bytes:
            observation = _incomplete(spec, b"", "evidence_limit_exceeded")
            content = _evidence_bytes(spec, observation, profile)
        if len(content) > controller.limits.evidence_max_bytes:
            raise ProductionRenderQcRetryableError(
                "production QC minimal evidence exceeds its configured cap"
            )
        aggregate += len(content)
        if aggregate > controller.limits.aggregate_evidence_max_bytes:
            raise ProductionRenderQcRetryableError(
                "production QC aggregate evidence exceeds its configured cap"
            )
        reference = store.put_immutable_blob(
            job,
            content=content,
            content_hash=_hash_bytes(content),
            media_type="application/json",
        )
        checks.append(
            ProductionRenderQcCheckEvidence(
                check_ordinal=spec.ordinal,
                check_id=spec.check_id,
                collection_status=observation.collection_status,
                coverage=observation.coverage,
                parser_schema_version=spec.parser_schema_version,
                tool_identity_sha256=_tool_identity_for_check(spec, profile),
                argv_sha256=spec.canonical_argv_sha256,
                measurements=tuple(_store_measurement(item) for item in observation.measurements),
                evidence_blob=reference,
                diagnostic_code=observation.diagnostic_code,
            )
        )
    report = ProductionRenderQcEvidenceReport(
        qc_attempt_id=attempt.qc_attempt_id,
        render_attempt_id=attempt.render_attempt_id,
        job_id=attempt.job_id,
        command_slot_id=attempt.command_slot_id,
        output_blob=attempt.output_blob,
        render_facts_sha256=attempt.render_facts_sha256,
        qc_policy_sha256=attempt.qc_policy_sha256,
        required_check_set_version=attempt.required_check_set_version,
        qc_runner_identity_sha256=attempt.qc_runner_identity_sha256,
        checks=tuple(checks),
    )
    controller.checkpoint(force_renewal=True)
    return store.record_production_render_qc_evidence(controller.lease, report)


def _evidence_bytes(
    spec: CollectorSpec,
    observation: CollectionObservation,
    profile: ProductionRenderQcCollectorProfile,
) -> bytes:
    return _canonical_json_bytes(
        {
            "argv_sha256": spec.canonical_argv_sha256,
            "check_id": spec.check_id,
            "check_ordinal": spec.ordinal,
            "collection_status": observation.collection_status,
            "coverage": observation.coverage,
            "diagnostic_code": observation.diagnostic_code,
            "examples": list(observation.examples),
            "measurements": [
                {
                    "name": item.name,
                    "unit": item.unit,
                    "value": item.value,
                    "value_kind": item.value_kind,
                }
                for item in observation.measurements
            ],
            "parser_schema_version": spec.parser_schema_version,
            "profile_id": profile.profile_id,
            "record_count": observation.record_count,
            "progress_stream_byte_length": observation.progress_stream_byte_length,
            "progress_stream_sha256": observation.progress_stream_sha256,
            "stream_byte_length": observation.stream_byte_length,
            "stream_sha256": observation.stream_sha256,
            "tool_identity_sha256": _tool_identity_for_check(spec, profile),
        }
    )


def _tool_identity_for_check(
    spec: CollectorSpec, profile: ProductionRenderQcCollectorProfile
) -> str:
    first = spec.argv_template[0]
    if first == "ffmpeg":
        return _canonical_hash(profile.ffmpeg_identity.to_mapping())
    if first == "ffprobe":
        return _canonical_hash(profile.ffprobe_identity.to_mapping())
    return profile.qc_runner_identity_sha256


def _store_measurement(item: Measurement) -> ProductionRenderQcMeasurement:
    return ProductionRenderQcMeasurement(item.name, item.value_kind, item.value, item.unit)


def _dependency_completed(spec: CollectorSpec, prior: Sequence[CollectionObservation]) -> bool:
    by_id = {
        collector.check_id: observation
        for collector, observation in zip(PRODUCTION_QC_COLLECTORS, prior, strict=False)
    }
    return all(
        dependency in by_id and by_id[dependency].collection_status == "completed"
        for dependency in spec.dependencies
    )


def _not_run(spec: CollectorSpec, diagnostic: str) -> CollectionObservation:
    return CollectionObservation("not_run", "none", (), 0, _SHA256_EMPTY, 0, diagnostic)


def _not_applicable(spec: CollectorSpec) -> CollectionObservation:
    return CollectionObservation("not_applicable", "not_applicable", (), 0, _SHA256_EMPTY, 0)


def _incomplete(
    spec: CollectorSpec, content: bytes | bytearray, diagnostic: str
) -> CollectionObservation:
    value = bytes(content)
    return CollectionObservation(
        "incomplete",
        "partial" if value else "none",
        (),
        len(value),
        _hash_bytes(value),
        0,
        diagnostic,
    )


def _incomplete_from_reducer(
    spec: CollectorSpec, reducer: BoundedStreamReducer, diagnostic: str
) -> CollectionObservation:
    return CollectionObservation(
        "incomplete",
        "partial" if reducer.stream_byte_length else "none",
        (),
        reducer.stream_byte_length,
        reducer.stream_sha256,
        reducer.record_count,
        diagnostic,
        examples=reducer.examples,
    )


def _incomplete_summary(
    spec: CollectorSpec,
    summary: _StreamSummary,
    record_count: int,
    diagnostic: str,
    *,
    examples: tuple[str, ...] = (),
    progress_summary: _StreamSummary | None = None,
) -> CollectionObservation:
    progress = progress_summary or _StreamSummary.create()
    return CollectionObservation(
        "incomplete",
        "partial" if summary.byte_length else "none",
        (),
        summary.byte_length,
        summary.sha256,
        record_count,
        diagnostic,
        examples=examples,
        progress_stream_byte_length=progress.byte_length,
        progress_stream_sha256=progress.sha256,
    )


def _dependency_blocked_observations(
    diagnostic: str, *, first_status: str
) -> list[CollectionObservation]:
    observations = [
        CollectionObservation(
            "incomplete",
            "none",
            (),
            0,
            _SHA256_EMPTY,
            0,
            diagnostic,
        )
    ]
    if first_status != "incomplete":
        raise AssertionError("unsupported dependency-blocked status")
    observations.extend(
        _not_run(spec, "dependency_failed") for spec in PRODUCTION_QC_COLLECTORS[1:]
    )
    return observations


def _fanout(*sinks: Callable[[bytes], None]) -> Callable[[bytes], None]:
    def feed(data: bytes) -> None:
        for sink in sinks:
            sink(data)

    return feed


def _diagnostic_code(value: str) -> str:
    return value.lower()


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )


def _canonical_hash(value: object) -> str:
    return _hash_bytes(_canonical_json_bytes(value))


def _hash_bytes(value: bytes | bytearray) -> str:
    return "sha256:" + hashlib.sha256(bytes(value)).hexdigest()


def _require_sha256(value: object, label: str) -> None:
    if (
        type(value) is not str  # noqa: E721
        or len(value) != 71
        or not value.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise ValueError(f"production QC {label} is not a lowercase SHA-256")


__all__ = (
    "ProductionQcStreamingRunner",
    "ProductionRenderQcCancelledError",
    "ProductionRenderQcCollectorProfile",
    "ProductionRenderQcExecutionLimits",
    "ProductionRenderQcHeartbeat",
    "ProductionRenderQcIdentityDriftError",
    "ProductionRenderQcInputError",
    "ProductionRenderQcPlanProjection",
    "ProductionRenderQcRetryableError",
    "ProductionRenderQcRunnerError",
    "ProductionRenderQcRuntimeCapability",
    "ProductionRenderQcStore",
    "resolve_production_render_qc_runtime_capability",
    "run_production_render_qc",
)
