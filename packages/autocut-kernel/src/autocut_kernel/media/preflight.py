"""Fixture-only, fail-closed local media preflight orchestration."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .ffprobe_port import FFprobeError, FFprobePort
from .types import (
    MediaDomainError,
    MediaEvidence,
    MediaValidationError,
    SourceIdentity,
    TickRange,
    ValidityIntervals,
    canonical_sha256,
    require_pts,
    sha256_prefixed,
)

Profile = Literal["production", "test", "shadow"]


class PreflightError(MediaDomainError):
    """Expected closed preflight denial with a stable adapter-facing code."""

    code = "INVALID_EVIDENCE"


class FixtureProfileForbiddenError(PreflightError):
    code = "TEST_FIXTURE_PROFILE_FORBIDDEN"


class SourceIdentityMismatchError(PreflightError):
    code = "SOURCE_IDENTITY_MISMATCH"


class FixtureEvidenceError(PreflightError):
    code = "INVALID_EVIDENCE"


@dataclass(frozen=True, slots=True)
class MediaPreflightRequest:
    """Closed input to the test/shadow fixture evidence adapter."""

    profile: Profile
    source_path: Path
    fixture_id: str
    expected_source_sha256: str
    manifest_path: Path
    sidecar_path: Path

    def __post_init__(self) -> None:
        if self.profile not in {"production", "test", "shadow"}:
            raise PreflightError("unsupported_profile")
        if not isinstance(self.fixture_id, str) or not self.fixture_id:
            raise PreflightError("fixture_id must be a non-empty string")
        sha256_prefixed(self.expected_source_sha256, "expected_source_sha256")


@dataclass(frozen=True, slots=True)
class PreflightDenial:
    """A serializable expected refusal; callers need not parse exception text."""

    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class PreflightResult:
    """One of complete immutable evidence or a closed denial."""

    evidence: MediaEvidence | None = None
    denial: PreflightDenial | None = None

    def __post_init__(self) -> None:
        if (self.evidence is None) == (self.denial is None):
            raise ValueError("preflight result must contain exactly one evidence or denial")

    @property
    def is_complete(self) -> bool:
        return self.evidence is not None


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    byte_size = 0
    try:
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
                byte_size += len(block)
    except OSError as error:
        raise FixtureEvidenceError("source bytes could not be read") from error
    if byte_size == 0:
        raise FixtureEvidenceError("source bytes must not be empty")
    return f"sha256:{digest.hexdigest()}", byte_size


def _json_file(path: Path, field_name: str) -> tuple[dict[str, Any], str]:
    try:
        raw = path.read_bytes()
        value: object = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FixtureEvidenceError(f"{field_name} must be readable UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise FixtureEvidenceError(f"{field_name} must be a JSON object")
    return value, f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _identity(payload: dict[str, Any], field_name: str) -> str:
    value = payload.get("content_sha256")
    if type(value) is not str:
        raise FixtureEvidenceError(f"{field_name}.content_sha256 is required")
    try:
        return sha256_prefixed(value, f"{field_name}.content_sha256")
    except MediaValidationError as error:
        raise FixtureEvidenceError(str(error)) from error


def _ranges(sidecar: dict[str, Any], evidence: MediaEvidence | None, *, pts_index) -> ValidityIntervals:
    raw = sidecar.get("validity_intervals")
    if not isinstance(raw, list) or not raw:
        raise FixtureEvidenceError("sidecar.validity_intervals must be a non-empty list")
    ranges: list[TickRange] = []
    for position, item in enumerate(raw):
        if not isinstance(item, dict):
            raise FixtureEvidenceError(f"validity_intervals[{position}] must be an object")
        try:
            start = require_pts(item.get("start_pts"), f"validity_intervals[{position}].start_pts")
            end = require_pts(item.get("end_pts"), f"validity_intervals[{position}].end_pts")
            ranges.append(TickRange(start, end))
        except MediaValidationError as error:
            raise FixtureEvidenceError(str(error)) from error
    try:
        intervals = ValidityIntervals(tuple(ranges))
        intervals.require_indexed(pts_index)
        return intervals
    except MediaValidationError as error:
        raise FixtureEvidenceError(str(error)) from error


def _validate_fixture_files(
    request: MediaPreflightRequest,
    source: SourceIdentity,
    pts_index,
) -> tuple[dict[str, Any], dict[str, Any], str, str]:
    manifest, manifest_sha256 = _json_file(request.manifest_path, "manifest")
    sidecar, sidecar_sha256 = _json_file(request.sidecar_path, "sidecar")
    if manifest.get("fixture_id") != request.fixture_id or sidecar.get("fixture_id") != request.fixture_id:
        raise SourceIdentityMismatchError("fixture identifiers do not match the request")
    if manifest.get("profile") not in {"test", "shadow"} or sidecar.get("profile") not in {"test", "shadow"}:
        raise FixtureEvidenceError("fixture manifest and sidecar must be test/shadow only")
    if manifest.get("schema_version") != sidecar.get("schema_version"):
        raise FixtureEvidenceError("fixture manifest and sidecar schema versions must match")
    try:
        schema_version = require_pts(manifest.get("schema_version"), "manifest.schema_version")
    except MediaValidationError as error:
        raise FixtureEvidenceError(str(error)) from error
    if schema_version <= 0:
        raise FixtureEvidenceError("manifest.schema_version must be positive")
    manifest_source = manifest.get("source")
    sidecar_source = sidecar.get("source")
    manifest_sidecar = manifest.get("sidecar")
    if not isinstance(manifest_source, dict) or not isinstance(sidecar_source, dict) or not isinstance(manifest_sidecar, dict):
        raise FixtureEvidenceError("fixture provenance objects are required")
    if _identity(manifest_source, "manifest.source") != source.sha256 or _identity(sidecar_source, "sidecar.source") != source.sha256:
        raise SourceIdentityMismatchError("fixture source hash does not match local source bytes")
    if manifest_source.get("byte_size") not in {None, source.byte_size} or sidecar_source.get("byte_size") not in {None, source.byte_size}:
        raise SourceIdentityMismatchError("fixture source byte size does not match local source bytes")
    if manifest_sidecar.get("sha256") != sidecar_sha256:
        raise SourceIdentityMismatchError("manifest sidecar hash does not match sidecar bytes")
    declared_pts_hash = sidecar.get("pts_index_sha256")
    if declared_pts_hash != canonical_sha256(list(pts_index.ticks)):
        raise SourceIdentityMismatchError("sidecar PTS index hash does not match ffprobe frames")
    return manifest, sidecar, manifest_sha256, sidecar_sha256


def preflight_fixture(request: MediaPreflightRequest, *, port: FFprobePort | None = None) -> MediaEvidence:
    """Probe a registered local fixture, refusing all incomplete or mismatched evidence."""
    if request.profile == "production":
        raise FixtureProfileForbiddenError("fixture evidence is forbidden for production")
    source_sha256, byte_size = _sha256_file(request.source_path)
    source = SourceIdentity(source_sha256, byte_size)
    if source.sha256 != request.expected_source_sha256:
        raise SourceIdentityMismatchError("source SHA-256 does not match request identity")
    probe = (port or FFprobePort()).probe(request.source_path)
    manifest, sidecar, manifest_sha256, sidecar_sha256 = _validate_fixture_files(request, source, probe.pts_index)
    if sidecar.get("evidence_mode") != "fixture_ground_truth_v1":
        raise FixtureEvidenceError("sidecar must declare fixture_ground_truth_v1 evidence")
    intervals = _ranges(sidecar, None, pts_index=probe.pts_index)
    return MediaEvidence(
        source=source,
        video_stream=probe.video_stream,
        pts_index=probe.pts_index,
        validity_intervals=intervals,
        pts_index_sha256=canonical_sha256(list(probe.pts_index.ticks)),
        ffprobe=probe.tool,
        fixture_id=request.fixture_id,
        fixture_manifest_sha256=manifest_sha256,
        fixture_sidecar_sha256=sidecar_sha256,
        fixture_schema_version=manifest["schema_version"],
        evidence_mode=sidecar["evidence_mode"],
    )


def preflight(request: MediaPreflightRequest, *, port: FFprobePort | None = None) -> PreflightResult:
    """Map expected media/fixture failures to a closed outcome for upper adapters."""
    try:
        return PreflightResult(evidence=preflight_fixture(request, port=port))
    except FFprobeError as error:
        return PreflightResult(denial=PreflightDenial(error.code, str(error)))
    except PreflightError as error:
        return PreflightResult(denial=PreflightDenial(error.code, str(error)))
    except MediaValidationError as error:
        return PreflightResult(denial=PreflightDenial("INVALID_EVIDENCE", str(error)))
