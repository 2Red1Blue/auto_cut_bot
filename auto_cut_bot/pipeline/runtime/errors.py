"""Closed failures exposed by the pipeline HTTP control plane."""

from __future__ import annotations

import json
import re
from typing import Mapping

_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
VLM_BATCH_POLICY_MISMATCH_CODE = "VLM_BATCH_CHILD_REQUEST_POLICY_MISMATCH"


class PipelineRunError(Exception):
    """Base class for typed pipeline run failures."""


class PipelineRunValidationError(PipelineRunError, ValueError):
    """A request or persisted projection violates the closed contract."""


class IdempotencyConflictError(PipelineRunError):
    """An idempotency key is already bound to another canonical request."""


class PipelineRunNotFoundError(PipelineRunError):
    """The requested durable run does not exist."""


class ResumeNotAllowedError(PipelineRunError):
    """The run has no pending command that this slice may resume safely."""


class StaleRunVersionError(PipelineRunError):
    """A caller attempted a run transition with a stale expected version."""


class SourceDeniedError(PipelineRunError):
    """The configured source authority rejected the request."""


class PipelineStageIsolationError(PipelineRunError):
    """A closed persisted-input incompatibility must terminate only its run."""

    def __init__(
        self,
        *,
        command_id: str,
        command_version: int,
        stage: str,
        failure_code: str,
        failure_detail: Mapping[str, object],
    ) -> None:
        if type(command_id) is not str or not command_id.strip():  # noqa: E721
            raise PipelineRunValidationError("isolated failure requires a command_id")
        if type(command_version) is not int or command_version < 0:  # noqa: E721
            raise PipelineRunValidationError("isolated failure requires a command version")
        if stage != "vlm" or failure_code != VLM_BATCH_POLICY_MISMATCH_CODE:
            raise PipelineRunValidationError("pipeline stage isolation is not allowlisted")
        detail = dict(failure_detail)
        if set(detail) != {
            "declared_episode_count",
            "distinct_policy_count",
            "ordered_policy_hashes_sha256",
            "schema_version",
        }:
            raise PipelineRunValidationError("isolated failure detail is not closed")
        if (
            type(detail["declared_episode_count"]) is not int  # noqa: E721
            or detail["declared_episode_count"] < 1
            or type(detail["distinct_policy_count"]) is not int  # noqa: E721
            or detail["distinct_policy_count"] < 2
            or detail["distinct_policy_count"] > detail["declared_episode_count"]
            or detail["schema_version"] != "vlm-batch-policy-mismatch-v1"
            or type(detail["ordered_policy_hashes_sha256"]) is not str  # noqa: E721
            or _SHA256.fullmatch(detail["ordered_policy_hashes_sha256"]) is None
        ):
            raise PipelineRunValidationError("isolated failure detail is invalid")
        self.command_id = command_id
        self.command_version = command_version
        self.stage = stage
        self.failure_code = failure_code
        self.failure_detail = detail
        self.failure_detail_json = json.dumps(
            detail,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        super().__init__(failure_code)
