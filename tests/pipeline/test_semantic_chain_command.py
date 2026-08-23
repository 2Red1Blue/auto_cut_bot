from __future__ import annotations

import hashlib
from dataclasses import replace
from uuid import uuid4

import pytest
from autocut_kernel.media.types import (
    MediaEvidence,
    PTSIndex,
    SourceIdentity,
    TickRange,
    TimeBase,
    ToolEvidence,
    ValidityIntervals,
    VideoStreamEvidence,
    canonical_sha256,
)
from autocut_kernel.physical_edit import FixtureBeatInput
from autocut_kernel.pipeline import SemanticChainCommand, SemanticChainCommandRequest
from autocut_kernel.semantic_chain import (
    CatalogCandidateRef,
    CatalogResolution,
    EvidenceRef,
    FactKind,
    FixtureCatalogAdapter,
    RegisteredFact,
    SemanticChainBuilder,
    SemanticChainInput,
    SemanticProfile,
)
from autocut_kernel.store import (
    ArtifactScope,
    CommandClaim,
    CommandOutcome,
    CommandRejection,
    CommandSuccess,
    Job,
)


class _Store:
    def __init__(self) -> None:
        self.successes: list[CommandSuccess] = []
        self.rejections: list[CommandRejection] = []
        self.outcomes: dict[tuple[str, str], CommandOutcome] = {}

    def claim_command(self, claim: CommandClaim) -> CommandOutcome:
        key = (claim.job.job_key, claim.idempotency_key)
        if key in self.outcomes:
            return self.outcomes[key]
        self.outcomes[key] = result = CommandOutcome(uuid4(), "running", is_fresh_claim=True)
        return result

    def commit_command_success(self, success: CommandSuccess) -> CommandOutcome:
        self.successes.append(success)
        return self._close(success.command_slot_id, "succeeded", artifact_set=True)

    def commit_command_rejection(self, rejection: CommandRejection) -> CommandOutcome:
        self.rejections.append(rejection)
        return self._close(
            rejection.command_slot_id, rejection.outcome, failure_code=rejection.failure_code
        )

    def _close(
        self,
        slot_id: object,
        state: str,
        *,
        artifact_set: bool = False,
        failure_code: str | None = None,
    ) -> CommandOutcome:
        for key, current in self.outcomes.items():
            if current.command_slot_id == slot_id:
                self.outcomes[key] = result = CommandOutcome(
                    current.command_slot_id,
                    state,
                    artifact_set_id=uuid4() if artifact_set else None,
                    failure_code=failure_code,
                )  # type: ignore[arg-type]
                return result
        raise AssertionError("unknown command slot")


class _Loader:
    def __init__(self, media: MediaEvidence) -> None:
        self.media = media

    def load_exact(self, _: EvidenceRef) -> MediaEvidence:
        return self.media


class _Registry:
    def resolve_exact(self, candidate: CatalogCandidateRef, _: MediaEvidence) -> CatalogResolution:
        return CatalogResolution(candidate, candidate.evidence)


class _BeatResolver:
    def __init__(self) -> None:
        self.calls = 0

    def resolve_beat(self, _: CatalogResolution) -> FixtureBeatInput:
        self.calls += 1
        return FixtureBeatInput(0, 10, 20, 40, 10)


class _Builder:
    def __init__(self) -> None:
        self.calls = 0
        self.delegate = SemanticChainBuilder()

    def build(self, source: SemanticChainInput):
        self.calls += 1
        return self.delegate.build(source)


def _hash(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _request(*, profile: str = "test") -> SemanticChainCommandRequest:
    media = MediaEvidence(
        SourceIdentity(_hash(b"source"), 100),
        VideoStreamEvidence(0, "mpeg4", 64, 48, TimeBase(1, 10)),
        PTSIndex((0, 10, 20, 30, 40)),
        ValidityIntervals((TickRange(0, 40),)),
        canonical_sha256([0, 10, 20, 30, 40]),
        ToolEvidence("ffprobe", "fixture", _hash(b"ffprobe")),
        "fixture",
        _hash(b"manifest"),
        _hash(b"sidecar"),
        1,
        "fixture_ground_truth_v1",
    )
    evidence = EvidenceRef("media_evidence_v1", canonical_sha256(media.to_json()))
    candidate = CatalogCandidateRef(
        "candidate_v1", "fixture_catalog_v1", _hash(b"catalog"), evidence, SemanticProfile.TEST
    )
    source = SemanticChainInput(
        SemanticProfile.TEST,
        (evidence,),
        (RegisteredFact("fact_v1", FactKind.OBSERVATION, evidence, candidate),),
    )
    job = Job("semantic-command-job", profile)  # type: ignore[arg-type]
    return SemanticChainCommandRequest(
        job,
        "semantic-chain-v1",
        source,
        candidate,
        FixtureCatalogAdapter(_Loader(media), _Registry()),
        _BeatResolver(),
        ArtifactScope("pipeline", "job", job.job_key),
    )


def test_success_persists_three_hash_bound_artifacts_and_replay_does_not_rebuild() -> None:
    store, builder = _Store(), _Builder()
    command = SemanticChainCommand(store, builder=builder)  # type: ignore[arg-type]
    first, replay = command.execute(_request()), command.execute(_request())
    assert first.outcome.state == replay.outcome.state == "succeeded"
    assert builder.calls == 1
    assert first.resolved_beat is not None and replay.resolved_beat == first.resolved_beat
    assert first.resolved_beat.beat == FixtureBeatInput(0, 10, 20, 40, 10)
    assert [item.artifact_type for item in store.successes[0].artifacts] == [
        "narrative_graph",
        "story_set",
        "editorial_blueprint",
    ]
    assert all("pts" not in item.payload_json.lower() for item in store.successes[0].artifacts)


def test_expected_adapter_denial_closes_a_receipt_without_artifacts() -> None:
    store, request = _Store(), _request()
    result = SemanticChainCommand(store).execute(
        replace(request, candidate=replace(request.candidate, candidate_id="foreign"))
    )  # type: ignore[arg-type]
    assert result.outcome.state == "denied" and result.outcome.artifact_set_id is None
    assert result.resolved_beat is None and not store.successes
    assert store.rejections[0].failure_code == "SEMANTIC_CHAIN_DENIED"


def test_production_job_is_durably_denied_before_build_or_resolution() -> None:
    store, builder = _Store(), _Builder()
    result = SemanticChainCommand(store, builder=builder).execute(_request(profile="production"))  # type: ignore[arg-type]
    assert (
        result.outcome.state == "denied"
        and result.outcome.failure_code == "PRODUCTION_PROFILE_FORBIDDEN"
    )
    assert builder.calls == 0


def test_request_hash_binds_catalog_identity_and_scope() -> None:
    request = _request()
    assert (
        replace(
            request, candidate=replace(request.candidate, catalog_source_hash=_hash(b"other"))
        ).request_hash
        != request.request_hash
    )
    with pytest.raises(ValueError, match="canonical recipe scope"):
        replace(request, artifact_scope=ArtifactScope("pipeline", "job", "other"))
