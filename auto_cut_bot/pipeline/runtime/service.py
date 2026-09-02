"""Durable, reconstructible pipeline run application service."""

from __future__ import annotations

import hashlib
from uuid import uuid4

from .errors import (
    IdempotencyConflictError,
    PipelineRunNotFoundError,
    PipelineRunValidationError,
    SourceDeniedError,
    StaleRunVersionError,
)
from .models import (
    MediaPreflightRecomputeRequest,
    PipelineExecutionProfile,
    PipelineRunRequest,
    PipelineRunSnapshot,
    RunClaim,
    VlmFullStageRecomputeRequest,
    validate_idempotency_key,
    validate_run_id,
)
from .ports import PipelineRunStore, PipelineSchedulerPort, SourceAuthorizationPort
from .recompute import FullStageVlmRecomputeBinderPort, MediaPreflightRecomputeBinderPort


class DurablePipelineRunService:
    """Persist intent first, then enqueue an opaque run identity.

    Neither the scheduler call nor an in-memory task is treated as completion
    authority. A restarted composition can replay reconstructible rows through
    :meth:`reconstruct`.
    """

    def __init__(
        self,
        store: PipelineRunStore,
        scheduler: PipelineSchedulerPort,
        source_authority: SourceAuthorizationPort,
        *,
        execution_profile: PipelineExecutionProfile | None = None,
        full_stage_vlm_recompute_binder: FullStageVlmRecomputeBinderPort | None = None,
        media_preflight_recompute_binder: MediaPreflightRecomputeBinderPort | None = None,
    ) -> None:
        self._store = store
        self._scheduler = scheduler
        self._source_authority = source_authority
        self._execution_profile = (
            PipelineExecutionProfile.legacy_unresolved()
            if execution_profile is None
            else execution_profile
        )
        if type(self._execution_profile) is not PipelineExecutionProfile:  # noqa: E721
            raise TypeError("execution_profile must be a PipelineExecutionProfile")
        if full_stage_vlm_recompute_binder is not None and not callable(
            getattr(full_stage_vlm_recompute_binder, "bind", None)
        ):
            raise TypeError("full_stage_vlm_recompute_binder must implement bind")
        self._full_stage_vlm_recompute_binder = full_stage_vlm_recompute_binder
        if media_preflight_recompute_binder is not None and not callable(
            getattr(media_preflight_recompute_binder, "bind", None)
        ):
            raise TypeError("media_preflight_recompute_binder must implement bind")
        self._media_preflight_recompute_binder = media_preflight_recompute_binder

    async def submit(self, request: PipelineRunRequest, idempotency_key: str) -> RunClaim:
        if type(request) is not PipelineRunRequest:  # noqa: E721
            raise TypeError("submit accepts only PipelineRunRequest")
        validate_idempotency_key(idempotency_key)
        if not self._execution_profile.has_executable_plan:
            raise PipelineRunValidationError(
                "new pipeline runs require a frozen media-preflight or semantic-only execution profile"
            )
        if not self._source_authority.allows(request):
            raise SourceDeniedError("source is outside the configured authority")
        claim = await self._store.claim_run(
            run_id=f"pipeline_run_{uuid4().hex}",
            idempotency_key=idempotency_key,
            request=request,
            request_hash=request.request_hash,
            execution_profile=self._execution_profile,
        )
        if not claim.snapshot.execution_profile.has_executable_plan:
            raise PipelineRunValidationError(
                "persisted pipeline run has no frozen media-preflight or semantic-only execution profile"
            )
        if claim.snapshot.request != request or claim.snapshot.request_hash != request.request_hash:
            raise PipelineRunValidationError(
                "claimed run does not bind the submitted canonical request"
            )
        # The durable scheduler is idempotent. Re-enqueue every replay so a
        # prior claim-success/enqueue-failure window is repairable by retrying
        # the same HTTP idempotency key.
        await self._scheduler.enqueue(claim.snapshot.run_id)
        return claim

    async def status(self, run_id: str) -> PipelineRunSnapshot:
        validate_run_id(run_id)
        snapshot = await self._store.read_run(run_id)
        if snapshot is None:
            raise PipelineRunNotFoundError(run_id)
        return snapshot

    async def resume(self, run_id: str, *, expected_version: int) -> PipelineRunSnapshot:
        validate_run_id(run_id)
        if type(expected_version) is not int or expected_version < 0:  # noqa: E721
            raise ValueError("expected_version must be a non-negative integer")
        persisted = await self._store.read_run(run_id)
        if persisted is None:
            raise PipelineRunNotFoundError(run_id)
        if not persisted.execution_profile.has_executable_plan:
            raise PipelineRunValidationError(
                "persisted pipeline run has no frozen media-preflight or semantic-only execution profile"
            )
        snapshot = await self._store.claim_resume(run_id, expected_version=expected_version)
        if (
            snapshot.run_id != run_id
            or snapshot.version != expected_version + 1
            or snapshot.status not in ("accepted", "running")
            or not any(
                command.status in ("pending", "indeterminate") for command in snapshot.commands
            )
        ):
            raise PipelineRunValidationError("resume claim returned an invalid CAS projection")
        await self._scheduler.enqueue(snapshot.run_id)
        return snapshot

    async def recompute_full_vlm_stage(
        self,
        request: VlmFullStageRecomputeRequest,
        idempotency_key: str,
    ) -> RunClaim:
        """Create a new complete VLM Run from exact source evidence.

        It is intentionally not a resume: the base Run and all of its Receipt
        history stay immutable.  The target command keys include a new Run ID,
        so provider attempts and resulting semantic artifacts are distinct.
        """

        if type(request) is not VlmFullStageRecomputeRequest:  # noqa: E721
            raise TypeError("recompute accepts an exact VlmFullStageRecomputeRequest")
        validate_idempotency_key(idempotency_key)
        binder = self._full_stage_vlm_recompute_binder
        if binder is None:
            raise PipelineRunValidationError("full VLM recompute is not enabled for this runtime")
        base = await self.status(request.base_run_id)
        if base.version != request.expected_version:
            raise StaleRunVersionError(request.base_run_id)
        if base.status not in ("succeeded", "denied", "failed"):
            raise PipelineRunValidationError("base run must be terminal before VLM recompute")
        if base.execution_profile != self._execution_profile:
            raise PipelineRunValidationError(
                "installed execution profile differs from the base run; exact recompute is unsafe"
            )
        if not (
            base.execution_profile.is_semantic_only
            or base.execution_profile.is_semantic_story
        ):
            raise PipelineRunValidationError(
                "VLM recompute requires a semantic-only or semantic-story base run"
            )
        target_run_id = _recompute_run_id(idempotency_key, request.request_hash)
        # Binding happens before the target control-plane Run exists.  Thus a
        # concurrent worker can never observe a new Run before it owns exact
        # source claims.  The binder is idempotent for the deterministic target.
        await binder.bind(base=base, target_run_id=target_run_id, request=request)
        claim = await self._store.claim_run(
            run_id=target_run_id,
            idempotency_key=idempotency_key,
            request=base.request,
            request_hash=base.request.request_hash,
            execution_profile=self._execution_profile,
            recompute_request=request,
        )
        if claim.snapshot.run_id != target_run_id:
            raise IdempotencyConflictError(
                "idempotency key already binds another recompute request"
            )
        if claim.snapshot.execution_profile != self._execution_profile:
            raise PipelineRunValidationError("target run persisted another execution profile")
        await self._scheduler.enqueue(target_run_id)
        return claim

    async def recompute_media_preflight_stage(
        self,
        request: MediaPreflightRecomputeRequest,
        idempotency_key: str,
    ) -> RunClaim:
        """Create or replay a media-only successor Run.

        The target is reserved with an ``awaiting_binding`` command before the binder is
        called.  A failed bind therefore leaves a durable, retryable target
        instead of creating an untracked evidence side effect.  The binder is
        required to be idempotent for the deterministic target identity.
        """
        if type(request) is not MediaPreflightRecomputeRequest:  # noqa: E721
            raise TypeError("media recompute accepts an exact MediaPreflightRecomputeRequest")
        validate_idempotency_key(idempotency_key)
        binder = self._media_preflight_recompute_binder
        # Media targets are keyed by the idempotency key alone.  Probe first so
        # an exact replay does not depend on mutable base-run or local-profile
        # state.  The store remains the authority for the race with a writer
        # that inserts the target after this probe.
        target_run_id = _recompute_run_id(
            idempotency_key, "", namespace="media-preflight"
        )
        existing = await self._store.read_run(target_run_id)
        if existing is not None:
            return await self._replay_or_activate_media(
                existing, request, replayed=True
            )

        if binder is None:
            raise PipelineRunValidationError(
                "media-preflight recompute is not enabled for this runtime"
            )
        base = await self._validated_media_base(request)
        claim = await self._store.claim_run(
            run_id=target_run_id,
            idempotency_key=idempotency_key,
            request=base.request,
            request_hash=base.request.request_hash,
            execution_profile=self._execution_profile,
            recompute_request=request,
            defer_activation=True,
        )
        if claim.snapshot.recompute_request != request:
            raise IdempotencyConflictError(
                "idempotency key already binds another media recompute request"
            )
        if claim.replayed:
            return await self._replay_or_activate_media(
                claim.snapshot, request, replayed=True
            )

        return await self._bind_and_activate_media(
            claim.snapshot, request, base=base, replayed=False
        )

    async def _validated_media_base(
        self, request: MediaPreflightRecomputeRequest
    ) -> PipelineRunSnapshot:
        base = await self.status(request.base_run_id)
        if base.version != request.expected_version:
            raise StaleRunVersionError(request.base_run_id)
        if base.status not in ("succeeded", "denied", "failed"):
            raise PipelineRunValidationError("base run must be terminal before media recompute")
        if base.execution_profile != self._execution_profile:
            raise PipelineRunValidationError(
                "installed execution profile differs from the base run; exact recompute is unsafe"
            )
        if not base.execution_profile.has_media_preflight_policy:
            raise PipelineRunValidationError(
                "media-preflight recompute requires an execution profile with a media-preflight policy"
            )
        return base

    async def _replay_or_activate_media(
        self,
        existing: PipelineRunSnapshot,
        request: MediaPreflightRecomputeRequest,
        *,
        replayed: bool,
    ) -> RunClaim:
        if existing.recompute_request != request:
            raise IdempotencyConflictError(
                "idempotency key already binds another media recompute request"
            )
        media_commands = tuple(
            command for command in existing.commands if command.stage == "media_preflight"
        )
        if len(media_commands) != 1:
            raise PipelineRunValidationError(
                "media recompute target must contain exactly one media-preflight command"
            )
        command = media_commands[0]
        if command.status == "awaiting_binding":
            binder = self._media_preflight_recompute_binder
            if binder is None:
                raise PipelineRunValidationError(
                    "media-preflight recompute is not enabled for this runtime"
                )
            base = await self._validated_media_base(request)
            return await self._bind_and_activate_media(
                existing, request, base=base, replayed=replayed
            )

        if command.status == "binding":
            # Try the same atomic claim operation: an unexpired lease returns
            # without provider work, while an expired lease is safely
            # reclaimed after a crashed binder.
            return await self._bind_and_activate_media(
                existing, request, base=None, replayed=replayed
            )

        if command.status not in (
            "pending",
            "running",
            "succeeded",
            "denied",
            "failed",
            "indeterminate",
            "awaiting_calibration",
            "recompute_needed",
            "awaiting_binding",
            "binding",
        ):
            raise PipelineRunValidationError(
                "media recompute target has an unsupported command state"
            )
        # Re-enqueue every exact replay to repair a claim-success/enqueue-
        # failure window.  The durable scheduler collapses duplicate work.
        await self._scheduler.enqueue(existing.run_id)
        return RunClaim(existing, replayed=replayed)

    async def _bind_and_activate_media(
        self,
        reserved: PipelineRunSnapshot,
        request: MediaPreflightRecomputeRequest,
        *,
        base: PipelineRunSnapshot | None,
        replayed: bool,
    ) -> RunClaim:
        binder = self._media_preflight_recompute_binder
        if binder is None:
            raise PipelineRunValidationError(
                "media-preflight recompute is not enabled for this runtime"
            )
        binding_id = "media-binding-" + uuid4().hex
        try:
            bound = await self._store.claim_recompute_binding(
                reserved.run_id,
                expected_version=reserved.version,
                binding_id=binding_id,
            )
        except StaleRunVersionError:
            refreshed = await self._store.read_run(reserved.run_id)
            if refreshed is None or refreshed.recompute_request != request:
                raise IdempotencyConflictError(
                    "idempotency key already binds another media recompute request"
                )
            return await self._replay_or_activate_media(
                refreshed, request, replayed=True
            )
        if bound is None:
            refreshed = await self._store.read_run(reserved.run_id)
            return RunClaim(refreshed or reserved, replayed=True)
        if base is None:
            base = await self._validated_media_base(request)
        try:
            await binder.bind(base=base, target_run_id=reserved.run_id, request=request)
        except Exception:
            try:
                await self._store.release_recompute_binding(
                    reserved.run_id,
                    expected_version=bound.version,
                    binding_id=binding_id,
                )
            except (StaleRunVersionError, PipelineRunValidationError):
                # A lease may have expired and been reclaimed while the
                # provider was failing.  Keep the provider failure as the
                # primary diagnostic; the durable owner will settle the run.
                pass
            raise
        try:
            activated = await self._store.activate_recompute(
                reserved.run_id,
                expected_version=bound.version,
                binding_id=binding_id,
            )
        except StaleRunVersionError:
            refreshed = await self._store.read_run(reserved.run_id)
            if refreshed is None or refreshed.recompute_request != request:
                raise IdempotencyConflictError(
                    "idempotency key already binds another media recompute request"
                )
            active_command = next(
                command
                for command in refreshed.commands
                if command.stage == "media_preflight"
            )
            if active_command.status in ("awaiting_binding", "binding"):
                raise
            activated = refreshed
        await self._scheduler.enqueue(activated.run_id)
        return RunClaim(activated, replayed=replayed)

    async def reconstruct(self) -> tuple[str, ...]:
        snapshots = await self._store.list_reconstructible_runs()
        run_ids: list[str] = []
        for snapshot in snapshots:
            if snapshot.status not in ("accepted", "running") or not any(
                command.status in ("pending", "running", "indeterminate")
                for command in snapshot.commands
            ):
                continue
            if not snapshot.execution_profile.has_executable_plan:
                raise PipelineRunValidationError(
                    "reconstructible run has no frozen media-preflight or semantic-only execution profile"
                )
            await self._scheduler.enqueue(snapshot.run_id)
            run_ids.append(snapshot.run_id)
        return tuple(run_ids)


def _recompute_run_id(
    idempotency_key: str, request_hash: str, *, namespace: str = "vlm"
) -> str:
    """Make binding and control-plane replay converge on one target Job."""

    encoded = (namespace + "-recompute-v2\0" + idempotency_key + "\0" + request_hash).encode(
        "utf-8"
    )
    return "pipeline_run_" + hashlib.sha256(encoded).hexdigest()[:32]
