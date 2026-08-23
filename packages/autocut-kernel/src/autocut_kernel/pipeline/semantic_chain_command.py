"""Durable terminal boundary for the closed semantic narrative MVP."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Final, Literal, Protocol, cast, runtime_checkable

from ..media.types import (
    MediaEvidence,
    PTSIndex,
    SourceIdentity,
    TickRange,
    TimeBase,
    ToolEvidence,
    ValidityIntervals,
    VideoStreamEvidence,
)
from ..physical_edit import FixtureBeatInput
from ..semantic_chain import (
    CatalogCandidateRef,
    CatalogResolution,
    EvidenceRef,
    FixtureCandidateRegistry,
    FixtureCatalogAdapter,
    SemanticChain,
    SemanticChainBuilder,
    SemanticChainDenied,
    SemanticChainInput,
)
from ..semantic_chain.models import canonical_sha256
from ..store import (
    ArtifactMember,
    ArtifactScope,
    CommandClaim,
    CommandOutcome,
    CommandRejection,
    CommandSuccess,
    Job,
    MediaEvidenceReference,
    PostgresRuntimeStore,
    RuntimeStoreError,
)
from ..store.models import canonical_recipe_scope

_COMMAND_NAME: Final = "semantic_chain_command"
_PRODUCTION_DENIAL: Final = "PRODUCTION_PROFILE_FORBIDDEN"
_SEMANTIC_DENIAL: Final = "SEMANTIC_CHAIN_DENIED"
_UNEXPECTED_FAILURE: Final = "UNEXPECTED_INFRASTRUCTURE_ERROR"


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _scope(scope: ArtifactScope) -> dict[str, str]:
    return {"key": scope.key, "kind": scope.kind, "namespace": scope.namespace}


@runtime_checkable
class FixtureBeatResolver(Protocol):
    """Catalog-owned physical bridge; accepts only an exact opaque resolution."""

    def resolve_beat(self, resolution: CatalogResolution) -> FixtureBeatInput: ...


@dataclass(frozen=True, slots=True)
class _StoreEvidenceLoader:
    """Load only the exact Store artifact named by the command request."""

    store: PostgresRuntimeStore
    media_job: Job
    reference: MediaEvidenceReference
    expected_evidence: EvidenceRef

    def load_exact(self, evidence_artifact: EvidenceRef) -> MediaEvidence:
        if evidence_artifact != self.expected_evidence:
            raise SemanticChainDenied("catalog requested evidence outside the command identity")
        try:
            persisted = self.store.read_media_evidence(self.media_job, self.reference)
            payload = json.loads(persisted.payload_json)
            return _media_evidence_from_mapping(payload)
        except (RuntimeStoreError, TypeError, ValueError, KeyError) as error:
            raise SemanticChainDenied(
                "persisted media evidence is not a valid MediaEvidence"
            ) from error


@dataclass(frozen=True, slots=True)
class SemanticArtifactReference:
    artifact_type: str
    logical_id: str
    revision: int
    scope: ArtifactScope
    content_hash: str

    @classmethod
    def from_member(cls, member: ArtifactMember) -> SemanticArtifactReference:
        return cls(
            member.artifact_type,
            member.logical_id,
            member.revision,
            member.scope,
            member.content_hash,
        )


@dataclass(frozen=True, slots=True)
class SemanticArtifactReferences:
    narrative_graph: SemanticArtifactReference
    story_set: SemanticArtifactReference
    editorial_blueprint: SemanticArtifactReference


@dataclass(frozen=True, slots=True)
class ResolvedSemanticBeat:
    """Validated PTS input for a caller-created, separate LocalMediaCommand Job."""

    beat: FixtureBeatInput
    semantic_artifacts: SemanticArtifactReferences

    @property
    def fixture_beat(self) -> FixtureBeatInput:
        return self.beat


@dataclass(frozen=True, slots=True)
class SemanticChainCommandResult:
    outcome: CommandOutcome
    resolved_beat: ResolvedSemanticBeat | None = None


@dataclass(frozen=True, slots=True)
class SemanticChainCommandRequest:
    """Canonical semantic intent; it carries no caller-controlled PTS data."""

    job: Job
    idempotency_key: str
    semantic_input: SemanticChainInput
    candidate: CatalogCandidateRef
    media_job: Job
    media_evidence_reference: MediaEvidenceReference
    registry: FixtureCandidateRegistry
    beat_resolver: FixtureBeatResolver
    artifact_scope: ArtifactScope

    def __post_init__(self) -> None:
        if type(self.semantic_input) is not SemanticChainInput:  # noqa: E721
            raise ValueError("semantic_input must be a SemanticChainInput")
        if type(self.candidate) is not CatalogCandidateRef:  # noqa: E721
            raise ValueError("candidate must be a CatalogCandidateRef")
        if type(self.media_evidence_reference) is not MediaEvidenceReference:  # noqa: E721
            raise ValueError("media_evidence_reference must be a MediaEvidenceReference")
        if not callable(getattr(self.registry, "resolve_exact", None)):
            raise ValueError("registry must resolve exact candidate evidence")
        if not isinstance(self.beat_resolver, FixtureBeatResolver):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise ValueError("beat_resolver must resolve exact CatalogResolution values")
        if self.artifact_scope != canonical_recipe_scope(self.job):
            raise ValueError("artifact_scope must be the canonical recipe scope for job")
        if (
            self.job.profile != "production"
            and self.job.profile != self.semantic_input.profile.value
        ):
            raise ValueError("job.profile must match semantic_input.profile")
        if self.media_job.profile != self.job.profile:
            raise ValueError("media_job.profile must match job.profile")
        if (
            self.candidate.evidence.artifact_id != self.media_evidence_reference.logical_id
            or self.candidate.evidence.content_hash != self.media_evidence_reference.content_hash
        ):
            raise ValueError("candidate evidence must match the exact media_evidence_reference")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "artifact_scope": _scope(self.artifact_scope),
            "candidate": self.candidate.to_mapping(),
            "evidence": self.candidate.evidence.to_mapping(),
            "media_evidence_reference": {
                "logical_id": self.media_evidence_reference.logical_id,
                "revision": self.media_evidence_reference.revision,
                "scope": _scope(self.media_evidence_reference.scope),
                "content_hash": self.media_evidence_reference.content_hash,
            },
            "media_job": {"job_key": self.media_job.job_key, "profile": self.media_job.profile},
            "idempotency_key": self.idempotency_key,
            "job": {"job_key": self.job.job_key, "profile": self.job.profile},
            "semantic_input": self.semantic_input.to_mapping(),
        }

    @property
    def request_hash(self) -> str:
        return canonical_sha256(self.canonical_payload())


class SemanticChainCommand:
    """Claim fresh work only, then build, resolve, and persist exactly once."""

    def __init__(
        self, store: PostgresRuntimeStore, *, builder: SemanticChainBuilder | None = None
    ) -> None:
        self._store = store
        self._builder = builder or SemanticChainBuilder()
        self._resolved: dict[object, ResolvedSemanticBeat] = {}

    def execute(self, request: SemanticChainCommandRequest) -> SemanticChainCommandResult:
        claimed = self._store.claim_command(
            CommandClaim(request.job, request.idempotency_key, _COMMAND_NAME, request.request_hash)
        )
        if not claimed.is_fresh_claim:
            if claimed.state != "succeeded":
                return SemanticChainCommandResult(claimed)
            cached = self._resolved.get(claimed.command_slot_id)
            if cached is not None:
                return SemanticChainCommandResult(claimed, cached)
            try:
                _, resolved = self._resolve(request)
                return SemanticChainCommandResult(claimed, resolved)
            except (SemanticChainDenied, RuntimeStoreError, TypeError, ValueError, KeyError):
                # A closed succeeded receipt is immutable.  Do not reopen it or
                # execute downstream work when its trusted bridge is unavailable.
                return SemanticChainCommandResult(claimed)
        if request.job.profile == "production":
            return SemanticChainCommandResult(self._reject(claimed, _PRODUCTION_DENIAL))
        try:
            chain, resolved = self._resolve(request)
            artifacts = self._artifacts(request, chain)
        except SemanticChainDenied as error:
            return SemanticChainCommandResult(self._reject(claimed, _SEMANTIC_DENIAL, str(error)))
        except Exception:
            return SemanticChainCommandResult(
                self._reject(claimed, _UNEXPECTED_FAILURE, outcome="failed")
            )
        outcome = self._store.commit_command_success(
            CommandSuccess(claimed.command_slot_id, _set_hash(artifacts), artifacts)
        )
        self._resolved[claimed.command_slot_id] = resolved
        return SemanticChainCommandResult(outcome, resolved)

    def _resolve(
        self, request: SemanticChainCommandRequest
    ) -> tuple[SemanticChain, ResolvedSemanticBeat]:
        chain = self._builder.build(request.semantic_input)
        adapter = FixtureCatalogAdapter(
            _StoreEvidenceLoader(
                self._store,
                request.media_job,
                request.media_evidence_reference,
                request.candidate.evidence,
            ),
            request.registry,
        )
        beat = request.beat_resolver.resolve_beat(adapter.resolve(chain, request.candidate))
        if type(beat) is not FixtureBeatInput:  # noqa: E721
            raise SemanticChainDenied("fixture beat resolver must return a FixtureBeatInput")
        refs = SemanticArtifactReferences(
            *(
                SemanticArtifactReference.from_member(item)
                for item in self._artifacts(request, chain)
            )
        )
        return chain, ResolvedSemanticBeat(beat, refs)

    @staticmethod
    def _artifacts(
        request: SemanticChainCommandRequest, chain: SemanticChain
    ) -> tuple[ArtifactMember, ArtifactMember, ArtifactMember]:
        payloads = (
            ("narrative_graph", chain.narrative.narrative_id, chain.narrative.to_mapping()),
            ("story_set", "story_set", {"stories": [chain.story.to_mapping()]}),
            ("editorial_blueprint", chain.blueprint.blueprint_id, chain.blueprint.to_mapping()),
        )
        return tuple(
            ArtifactMember(
                kind,
                logical_id,
                1,
                request.artifact_scope,
                canonical_sha256(payload),
                _json(payload),
            )
            for kind, logical_id, payload in payloads
        )  # type: ignore[return-value]

    def _reject(
        self,
        claimed: CommandOutcome,
        code: str,
        detail: str | None = None,
        *,
        outcome: Literal["denied", "failed"] = "denied",
    ) -> CommandOutcome:
        return self._store.commit_command_rejection(
            CommandRejection(
                claimed.command_slot_id,
                code,
                _json({"code": code, "detail": detail or code}),
                outcome=outcome,
            )
        )


def _set_hash(artifacts: tuple[ArtifactMember, ...]) -> str:
    return canonical_sha256(
        [
            {
                "artifact_type": item.artifact_type,
                "content_hash": item.content_hash,
                "logical_id": item.logical_id,
                "payload_json": json.loads(item.payload_json),
                "revision": item.revision,
                "scope": _scope(item.scope),
            }
            for item in artifacts
        ]
    )


def _media_evidence_from_mapping(value: object) -> MediaEvidence:
    """Parse the Store's verified JSON into the closed media evidence value."""

    if not isinstance(value, dict):
        raise ValueError("media evidence payload must be an object")
    payload = cast(dict[str, object], value)
    source = _mapping(payload["source"])
    stream = _mapping(payload["video_stream"])
    time_base = _mapping(stream["time_base"])
    intervals = payload["validity_intervals"]
    ffprobe = _mapping(payload["ffprobe"])
    if not isinstance(intervals, list):
        raise ValueError("media evidence intervals are invalid")
    interval_maps = tuple(_mapping(item) for item in cast(list[object], intervals))
    return MediaEvidence(
        SourceIdentity(_str(source["sha256"]), _int(source["byte_size"])),
        VideoStreamEvidence(
            _int(stream["stream_index"]),
            _str(stream["codec_name"]),
            _int(stream["width"]),
            _int(stream["height"]),
            TimeBase(_int(time_base["numerator"]), _int(time_base["denominator"])),
        ),
        PTSIndex(_int_tuple(payload["pts_index"])),
        ValidityIntervals(
            tuple(
                TickRange(_int(item["start_pts"]), _int(item["end_pts"])) for item in interval_maps
            )
        ),
        _str(payload["pts_index_sha256"]),
        ToolEvidence(
            _str(ffprobe["executable"]), _str(ffprobe["version"]), _str(ffprobe["stderr_sha256"])
        ),
        _str(payload["fixture_id"]),
        _str(payload["fixture_manifest_sha256"]),
        _str(payload["fixture_sidecar_sha256"]),
        _int(payload["fixture_schema_version"]),
        _str(payload["evidence_mode"]),
    )


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("media evidence object member is invalid")
    return cast(dict[str, object], value)


def _str(value: object) -> str:
    if type(value) is not str:  # noqa: E721
        raise ValueError("media evidence string field is invalid")
    return value


def _int(value: object) -> int:
    if type(value) is not int:  # noqa: E721
        raise ValueError("media evidence integer field is invalid")
    return value


def _int_tuple(value: object) -> tuple[int, ...]:
    if not isinstance(value, list):
        raise ValueError("media evidence PTS index is invalid")
    items = cast(list[object], value)
    if any(type(item) is not int for item in items):  # noqa: E721
        raise ValueError("media evidence PTS index is invalid")
    return tuple(cast(int, item) for item in items)
