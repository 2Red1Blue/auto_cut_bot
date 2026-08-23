"""Durable terminal boundary for the closed semantic narrative MVP."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Final, Literal, Protocol, runtime_checkable

from ..physical_edit import FixtureBeatInput
from ..semantic_chain import (
    CatalogCandidateRef,
    CatalogResolution,
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
    PostgresRuntimeStore,
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
    catalog_adapter: FixtureCatalogAdapter
    beat_resolver: FixtureBeatResolver
    artifact_scope: ArtifactScope

    def __post_init__(self) -> None:
        if type(self.semantic_input) is not SemanticChainInput:  # noqa: E721
            raise ValueError("semantic_input must be a SemanticChainInput")
        if type(self.candidate) is not CatalogCandidateRef:  # noqa: E721
            raise ValueError("candidate must be a CatalogCandidateRef")
        if type(self.catalog_adapter) is not FixtureCatalogAdapter:  # noqa: E721
            raise ValueError("catalog_adapter must be a FixtureCatalogAdapter")
        if not isinstance(self.beat_resolver, FixtureBeatResolver):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise ValueError("beat_resolver must resolve exact CatalogResolution values")
        if self.artifact_scope != canonical_recipe_scope(self.job):
            raise ValueError("artifact_scope must be the canonical recipe scope for job")
        if (
            self.job.profile != "production"
            and self.job.profile != self.semantic_input.profile.value
        ):
            raise ValueError("job.profile must match semantic_input.profile")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "artifact_scope": _scope(self.artifact_scope),
            "candidate": self.candidate.to_mapping(),
            "evidence": self.candidate.evidence.to_mapping(),
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
            return SemanticChainCommandResult(claimed, self._resolved.get(claimed.command_slot_id))
        if request.job.profile == "production":
            return SemanticChainCommandResult(self._reject(claimed, _PRODUCTION_DENIAL))
        try:
            chain = self._builder.build(request.semantic_input)
            beat = request.beat_resolver.resolve_beat(
                request.catalog_adapter.resolve(chain, request.candidate)
            )
            if type(beat) is not FixtureBeatInput:  # noqa: E721
                raise SemanticChainDenied("fixture beat resolver must return a FixtureBeatInput")
            artifacts = self._artifacts(request, chain)
            refs = SemanticArtifactReferences(
                *(SemanticArtifactReference.from_member(item) for item in artifacts)
            )
            resolved = ResolvedSemanticBeat(beat, refs)
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
