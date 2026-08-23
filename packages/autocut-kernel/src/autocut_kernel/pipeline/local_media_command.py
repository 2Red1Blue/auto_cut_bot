"""Durable adapter for the fixture-backed local media command."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, cast

from ..media.ffprobe_port import FFprobePort
from ..media.preflight import MediaPreflightRequest, PreflightDenial, preflight
from ..physical_edit import (
    CandidatePairLimitError,
    ExactSpanCompiler,
    ExactSpanValidationError,
    FixtureBeatInput,
    NoLegalSpanError,
    SpanSelectionPolicy,
)
from ..store import (
    ArtifactMember,
    ArtifactScope,
    CommandClaim,
    CommandOutcome,
    CommandRejection,
    CommandSuccess,
    Job,
    PostgresRuntimeStore,
)
from ..store.models import canonical_recipe_scope

_COMMAND_NAME = "local_media_command"
_UNEXPECTED_FAILURE_CODE = "UNEXPECTED_INFRASTRUCTURE_ERROR"


def _canonical_json(value: object) -> str:
    """Encode the same compact, key-sorted JSON form used by store members."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sha256(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class LocalMediaCommandRequest:
    """All typed input required for one idempotent local media command."""

    job: Job
    idempotency_key: str
    preflight_request: MediaPreflightRequest
    beat: FixtureBeatInput
    policy: SpanSelectionPolicy
    artifact_scope: ArtifactScope

    def __post_init__(self) -> None:
        if self.job.profile != self.preflight_request.profile:
            raise ValueError("job.profile must match preflight_request.profile")
        if self.artifact_scope != canonical_recipe_scope(self.job):
            raise ValueError("artifact_scope must be the canonical recipe scope for job")

    def canonical_payload(self) -> dict[str, object]:
        """Return the full command intent as an immutable JSON-ready mapping."""
        return {
            "artifact_scope": _scope_json(self.artifact_scope),
            "beat": _beat_json(self.beat),
            "idempotency_key": self.idempotency_key,
            "job": {"job_key": self.job.job_key, "profile": self.job.profile},
            "policy": _policy_json(self.policy),
            "preflight_request": {
                "expected_source_sha256": self.preflight_request.expected_source_sha256,
                "fixture_id": self.preflight_request.fixture_id,
                "manifest_path": str(self.preflight_request.manifest_path),
                "profile": self.preflight_request.profile,
                "sidecar_path": str(self.preflight_request.sidecar_path),
                "source_path": str(self.preflight_request.source_path),
            },
        }

    @property
    def request_hash(self) -> str:
        return _sha256(self.canonical_payload())


class LocalMediaCommand:
    """Claim, preflight, compile, and durably persist one source-span recipe."""

    def __init__(self, store: PostgresRuntimeStore, *, port: FFprobePort | None = None) -> None:
        self._store = store
        self._port = port

    def execute(self, request: LocalMediaCommandRequest) -> CommandOutcome:
        """Execute once for a fresh claim, otherwise return its durable outcome."""
        claimed = self._store.claim_command(
            CommandClaim(
                job=request.job,
                idempotency_key=request.idempotency_key,
                command_name=_COMMAND_NAME,
                request_hash=request.request_hash,
            )
        )
        if not claimed.is_fresh_claim:
            return claimed

        try:
            result = preflight(request.preflight_request, port=self._port)
            if result.denial is not None:
                return self._commit_denial(claimed, result.denial)
            evidence = result.evidence
            if evidence is None:  # Defensive; PreflightResult enforces this invariant.
                raise RuntimeError("preflight returned no terminal result")
            span = ExactSpanCompiler(
                evidence.pts_index, evidence.validity_intervals, request.policy
            ).compile(request.beat)
            artifacts = self._artifacts(request, evidence.to_json(), span.start_pts, span.end_pts)
        except (CandidatePairLimitError, ExactSpanValidationError, NoLegalSpanError) as error:
            return self._commit_expected_span_failure(claimed, error)
        except Exception:
            # This work has a durable fresh owner.  Record a closed failure rather
            # than allowing a same-process retry to execute the probe again.
            return self._store.commit_command_rejection(
                CommandRejection(
                    claimed.command_slot_id,
                    _UNEXPECTED_FAILURE_CODE,
                    _canonical_json({"stage": _COMMAND_NAME}),
                    outcome="failed",
                )
            )

        return self._store.commit_command_success(
            CommandSuccess(claimed.command_slot_id, _set_hash(artifacts), artifacts)
        )

    def _commit_denial(self, claimed: CommandOutcome, denial: PreflightDenial) -> CommandOutcome:
        return self._store.commit_command_rejection(
            CommandRejection(
                claimed.command_slot_id,
                denial.code,
                _canonical_json({"code": denial.code, "detail": denial.detail}),
            )
        )

    def _commit_expected_span_failure(
        self,
        claimed: CommandOutcome,
        error: CandidatePairLimitError | ExactSpanValidationError | NoLegalSpanError,
    ) -> CommandOutcome:
        if isinstance(error, CandidatePairLimitError):
            code = "CANDIDATE_PAIR_LIMIT_EXCEEDED"
        elif isinstance(error, NoLegalSpanError):
            code = "NO_LEGAL_SPAN"
        else:
            code = "INVALID_SPAN_REQUEST"
        return self._store.commit_command_rejection(
            CommandRejection(
                claimed.command_slot_id,
                code,
                _canonical_json({"code": code, "detail": str(error)}),
            )
        )

    @staticmethod
    def _artifacts(
        request: LocalMediaCommandRequest,
        evidence: dict[str, object],
        start_pts: int,
        end_pts: int,
    ) -> tuple[ArtifactMember, ArtifactMember]:
        evidence_payload = _canonical_json(evidence)
        span = {"end_pts": end_pts, "start_pts": start_pts}
        selection_key = [
            request.beat.anchor_start_pts - start_pts,
            end_pts - request.beat.anchor_end_pts,
            end_pts - start_pts,
            start_pts,
            end_pts,
        ]
        video_stream = evidence["video_stream"]
        assert isinstance(video_stream, dict)
        timebase = cast(dict[str, object], video_stream["time_base"])
        recipe: dict[str, Any] = {
            "beat": _beat_json(request.beat),
            "evidence": evidence,
            "policy": _policy_json(request.policy),
            "selection_key": selection_key,
            "source": evidence["source"],
            "span": span,
            "timebase": timebase,
        }
        recipe_payload = _canonical_json(recipe)
        return (
            ArtifactMember(
                artifact_type="media_evidence",
                logical_id="media_evidence",
                revision=1,
                scope=request.artifact_scope,
                content_hash=_sha256(evidence),
                payload_json=evidence_payload,
            ),
            ArtifactMember(
                artifact_type="recipe",
                logical_id="recipe",
                revision=1,
                scope=request.artifact_scope,
                content_hash=_sha256(recipe),
                payload_json=recipe_payload,
            ),
        )


def _scope_json(scope: ArtifactScope) -> dict[str, str]:
    return {"key": scope.key, "kind": scope.kind, "namespace": scope.namespace}


def _beat_json(beat: FixtureBeatInput) -> dict[str, int]:
    return {
        "anchor_end_pts": beat.anchor_end_pts,
        "anchor_start_pts": beat.anchor_start_pts,
        "desired_end_pts": beat.desired_end_pts,
        "desired_start_pts": beat.desired_start_pts,
        "minimum_duration_pts": beat.minimum_duration_pts,
    }


def _policy_json(policy: SpanSelectionPolicy) -> dict[str, object]:
    return {
        "candidate_pair_limit": policy.candidate_pair_limit,
        "forbidden_ranges": [
            {"end_pts": item.end_pts, "start_pts": item.start_pts}
            for item in policy.forbidden_ranges
        ],
    }


def _set_hash(artifacts: tuple[ArtifactMember, ...]) -> str:
    """Bind members exactly as :class:`CommandSuccess` does."""
    return _sha256(
        [
            {
                "artifact_type": item.artifact_type,
                "content_hash": item.content_hash,
                "logical_id": item.logical_id,
                "payload_json": json.loads(item.payload_json),
                "revision": item.revision,
                "scope": _scope_json(item.scope),
            }
            for item in artifacts
        ]
    )
