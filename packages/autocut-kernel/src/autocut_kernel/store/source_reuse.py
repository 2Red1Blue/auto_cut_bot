"""Closed records for binding immutable prepared sources to a new Pipeline Job.

The binding is deliberately narrower than a cross-Job Blob read API.  It is
created once by the Store-owned transaction, gives the target Job a claim to
the already immutable proxy objects, and records exactly which successful
SourcePrep Receipt authorized that projection.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import cast

from ..source_manifest import SourceOperationPolicy, SourceOperationPurpose
from .errors import StoreValidationError
from .models import (
    ArtifactMember,
    ArtifactScope,
    Job,
    PersistedWholeSeriesSourceManifest,
    canonical_payload_hash,
    canonical_recipe_scope,
)

SOURCE_REUSE_COMMAND_NAME = "BindWholeSeriesSourcesCommand"
SOURCE_REUSE_BINDING_SCHEMA_VERSION = "source-reuse-binding-v1"
SOURCE_REUSE_BINDING_ARTIFACT_TYPE = "source_reuse_binding"
SOURCE_REUSE_BINDING_LOGICAL_ID = "source_reuse_binding"


def _closed_mapping(value: object, fields: set[str], field_name: str) -> dict[str, object]:
    if type(value) is not dict:  # noqa: E721
        raise StoreValidationError(f"{field_name} must be a closed object")
    mapping: dict[str, object] = {}
    for key, item in cast(dict[object, object], value).items():
        if type(key) is not str:  # noqa: E721
            raise StoreValidationError(f"{field_name} must use text keys")
        mapping[key] = item
    if set(mapping) != fields:
        raise StoreValidationError(f"{field_name} must be a closed object")
    return mapping


def _policy_from_mapping(value: object) -> SourceOperationPolicy:
    mapping = _closed_mapping(
        value,
        {
            "authorization_id",
            "authorized_purposes",
            "expected_source_count",
            "schema_version",
            "series_id",
        },
        "source reuse target_policy",
    )
    purposes = mapping["authorized_purposes"]
    if type(purposes) is not list:  # noqa: E721
        raise StoreValidationError("source reuse target_policy purposes are invalid")
    purpose_values = cast(list[object], purposes)
    if any(type(item) is not str for item in purpose_values):
        raise StoreValidationError("source reuse target_policy purposes are invalid")
    try:
        policy = SourceOperationPolicy(
            cast(str, mapping["authorization_id"]),
            cast(str, mapping["series_id"]),
            cast(int, mapping["expected_source_count"]),
            cast(
                tuple[SourceOperationPurpose, ...],
                tuple(cast(SourceOperationPurpose, item) for item in purpose_values),
            ),
        )
    except (TypeError, ValueError) as error:
        raise StoreValidationError("source reuse target_policy is invalid") from error
    if mapping["schema_version"] != policy.schema_version or mapping != policy.to_mapping():
        raise StoreValidationError("source reuse target_policy is not canonical")
    return policy


@dataclass(frozen=True, slots=True)
class SourceReuseBinding:
    """One auditable grant of prepared source evidence to a new target Job."""

    origin: PersistedWholeSeriesSourceManifest
    target_job: Job
    target_policy: SourceOperationPolicy

    def __post_init__(self) -> None:
        if type(self.origin) is not PersistedWholeSeriesSourceManifest:  # noqa: E721
            raise StoreValidationError("source reuse origin must be a persisted source manifest")
        if self.origin.source_job is None:
            raise StoreValidationError("source reuse origin must retain its source Job")
        if type(self.target_job) is not Job:  # noqa: E721
            raise StoreValidationError("source reuse target Job is invalid")
        if type(self.target_policy) is not SourceOperationPolicy:  # noqa: E721
            raise StoreValidationError("source reuse target policy is invalid")
        if self.origin.source_job == self.target_job:
            raise StoreValidationError("source reuse requires distinct origin and target Jobs")
        if self.target_policy.expected_source_count < 1:
            raise StoreValidationError("source reuse target policy has invalid source count")

    @property
    def target_scope(self) -> ArtifactScope:
        return canonical_recipe_scope(self.target_job)

    def to_mapping(self) -> dict[str, object]:
        return {
            "origin": self.origin.provenance_mapping(),
            "origin_source_manifest_sha256": self.origin.reference.content_hash,
            "origin_source_provenance_sha256": self.origin.canonical_hash,
            "schema_version": SOURCE_REUSE_BINDING_SCHEMA_VERSION,
            "target_job": {
                "job_key": self.target_job.job_key,
                "profile": self.target_job.profile,
            },
            "target_policy": self.target_policy.to_mapping(),
            "target_policy_sha256": self.target_policy.policy_sha256,
        }

    def artifact(self, revision: int) -> ArtifactMember:
        payload_json = json.dumps(
            self.to_mapping(), ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        return ArtifactMember(
            SOURCE_REUSE_BINDING_ARTIFACT_TYPE,
            SOURCE_REUSE_BINDING_LOGICAL_ID,
            revision,
            self.target_scope,
            canonical_payload_hash(payload_json),
            payload_json,
        )

    @classmethod
    def validate_payload(
        cls,
        payload_json: str,
        *,
        origin: PersistedWholeSeriesSourceManifest,
        target_job: Job,
        target_policy: SourceOperationPolicy | None = None,
    ) -> SourceOperationPolicy:
        """Verify a stored binding against independently-read origin evidence."""

        try:
            raw = json.loads(payload_json)
        except (TypeError, ValueError) as error:
            raise StoreValidationError("source reuse binding payload must contain JSON") from error
        mapping = _closed_mapping(
            raw,
            {
                "origin",
                "origin_source_manifest_sha256",
                "origin_source_provenance_sha256",
                "schema_version",
                "target_job",
                "target_policy",
                "target_policy_sha256",
            },
            "source reuse binding",
        )
        if mapping["schema_version"] != SOURCE_REUSE_BINDING_SCHEMA_VERSION:
            raise StoreValidationError("source reuse binding schema version is unsupported")
        if mapping["origin"] != origin.provenance_mapping():
            raise StoreValidationError("source reuse binding origin provenance is invalid")
        if (
            mapping["origin_source_manifest_sha256"] != origin.reference.content_hash
            or mapping["origin_source_provenance_sha256"] != origin.canonical_hash
        ):
            raise StoreValidationError("source reuse binding origin hashes are invalid")
        expected_target = {"job_key": target_job.job_key, "profile": target_job.profile}
        if mapping["target_job"] != expected_target:
            raise StoreValidationError("source reuse binding target Job is invalid")
        policy = _policy_from_mapping(mapping["target_policy"])
        if mapping["target_policy_sha256"] != policy.policy_sha256:
            raise StoreValidationError("source reuse binding target policy hash is invalid")
        if target_policy is not None and policy != target_policy:
            raise StoreValidationError("source reuse binding target policy does not match request")
        return policy


__all__ = [
    "SOURCE_REUSE_BINDING_ARTIFACT_TYPE",
    "SOURCE_REUSE_BINDING_LOGICAL_ID",
    "SOURCE_REUSE_BINDING_SCHEMA_VERSION",
    "SOURCE_REUSE_COMMAND_NAME",
    "SourceReuseBinding",
]
