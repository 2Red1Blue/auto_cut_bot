from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from uuid import uuid4

import autocut_kernel.pipeline.collect_shadow_bootstrap_observation_command as command_module
import pytest
from autocut_kernel.media.shadow_bootstrap_observation import (
    ShadowBootstrapObservationRequest,
    ShadowBootstrapObservationResult,
    ShadowBootstrapObservationSource,
    decode_shadow_bootstrap_observation_response,
    encode_shadow_bootstrap_observation_response,
    project_shadow_bootstrap_observation,
)
from autocut_kernel.media.types import TickRange, TimeBase
from autocut_kernel.pipeline.collect_shadow_bootstrap_observation_command import (
    CollectShadowBootstrapObservationCommand,
    CollectShadowBootstrapObservationError,
    CollectShadowBootstrapObservationRequest,
    ShadowBootstrapObservationDispatchUnknownError,
)
from autocut_kernel.store import (
    ArtifactScope,
    BlobRef,
    CommandClaim,
    CommandOutcome,
    CommandRejection,
    CommandSuccess,
    Job,
    PersistedWholeSeriesSourceManifest,
    WholeSeriesSourceManifestReference,
)
from autocut_kernel.store.models import MaterializationLimits, canonical_payload_hash


@dataclass
class _Lease:
    reference: BlobRef
    store: _Store
    closed: bool = False

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.store.closed_materializations += 1


@dataclass
class _Store:
    persisted: PersistedWholeSeriesSourceManifest
    outcomes: dict[str, CommandOutcome] = field(default_factory=dict)
    claims: list[CommandClaim] = field(default_factory=list)
    materializations: list[BlobRef] = field(default_factory=list)
    blobs: dict[object, bytes] = field(default_factory=dict)
    successes: list[CommandSuccess] = field(default_factory=list)
    rejections: list[CommandRejection] = field(default_factory=list)
    closed_materializations: int = 0

    def read_whole_series_source_manifest(
        self, job: Job, artifact_set_id: object
    ) -> PersistedWholeSeriesSourceManifest:
        assert job == self.persisted.source_job
        assert artifact_set_id == self.persisted.artifact_set_id
        return self.persisted

    def claim_command(self, claim: CommandClaim) -> CommandOutcome:
        self.claims.append(claim)
        if claim.idempotency_key in self.outcomes:
            return self.outcomes[claim.idempotency_key]
        outcome = CommandOutcome(uuid4(), "running", is_fresh_claim=True)
        self.outcomes[claim.idempotency_key] = outcome
        return outcome

    def materialize_immutable_blob(
        self, job: Job, reference: BlobRef, limits: MaterializationLimits
    ) -> _Lease:
        assert reference.byte_length <= limits.effective_max_source_bytes
        self.materializations.append(reference)
        return _Lease(reference, self)

    def put_immutable_blob(
        self, job: Job, *, content: bytes, content_hash: str, media_type: str
    ) -> BlobRef:
        del job
        assert content_hash == "sha256:" + hashlib.sha256(content).hexdigest()
        result = BlobRef(uuid4(), content_hash, len(content), media_type)
        self.blobs[result.object_id] = content
        return result

    def commit_command_success(self, success: CommandSuccess) -> CommandOutcome:
        self.successes.append(success)
        result = CommandOutcome(success.command_slot_id, "succeeded", receipt_id=uuid4(), artifact_set_id=uuid4())
        self._replace(success.command_slot_id, result)
        return result

    def commit_command_rejection(self, rejection: CommandRejection) -> CommandOutcome:
        self.rejections.append(rejection)
        result = CommandOutcome(
            rejection.command_slot_id,
            rejection.outcome,
            receipt_id=uuid4(),
            failure_code=rejection.failure_code,
            failure_detail_json=rejection.failure_detail_json,
        )
        self._replace(rejection.command_slot_id, result)
        return result

    def _replace(self, command_slot_id: object, result: CommandOutcome) -> None:
        for key, value in self.outcomes.items():
            if value.command_slot_id == command_slot_id:
                self.outcomes[key] = result
                return
        raise AssertionError("unknown command slot")


@dataclass
class _Port:
    raw: bytes
    calls: int = 0

    def observe(
        self, request: ShadowBootstrapObservationRequest, source: _Lease
    ) -> ShadowBootstrapObservationResult:
        assert source.reference.content_hash == request.source.source_sha256
        self.calls += 1
        return project_shadow_bootstrap_observation(
            decode_shadow_bootstrap_observation_response(self.raw, request)
        )


def _fixture(monkeypatch: pytest.MonkeyPatch) -> tuple[_Store, CollectShadowBootstrapObservationRequest, _Port]:
    source_job = Job("source-run-001", "shadow")
    source_bytes = b"committed source"
    source_blob = BlobRef(
        uuid4(), "sha256:" + hashlib.sha256(source_bytes).hexdigest(), len(source_bytes), "video/mp4"
    )
    reference = WholeSeriesSourceManifestReference(
        ArtifactScope("pipeline", "job", source_job.job_key),
        "whole_series_source_manifest",
        1,
        canonical_payload_hash("{}"),
    )
    persisted = PersistedWholeSeriesSourceManifest(
        reference, "{}", (source_blob,), uuid4(), uuid4(), uuid4(), uuid4(), source_job
    )
    store = _Store(persisted, blobs={source_blob.object_id: source_bytes})
    # Mirrors EvidenceContext: an origin tick and a derived end tick, no
    # full_range accessor.
    clock = SimpleNamespace(
        clock_id="audio-30k",
        time_base=TimeBase(1, 30_000),
        origin_tick=0,
        end_tick=6_000,
    )
    clock_range = TickRange(clock.origin_tick, clock.end_tick)
    episode = SimpleNamespace(
        proxy_blob=source_blob,
        media_probe=SimpleNamespace(
            source=SimpleNamespace(source_id="episode-001", content_sha256=source_blob.content_hash),
            audio_sample_boundaries=SimpleNamespace(context=clock),
        ),
    )
    monkeypatch.setattr(command_module, "decode_source_manifest", lambda *_: SimpleNamespace(episodes=(episode,)))
    limits = MaterializationLimits(1024, 1024, 128, 1024)
    observation = ShadowBootstrapObservationRequest(
        ShadowBootstrapObservationSource(
            "episode-001", source_blob.content_hash, clock.clock_id, clock.time_base, clock_range
        ),
        clock_range,
        limits.max_source_bytes,
        limits.timed_speech_max_request_bytes,
        16_384,
    )
    request = CollectShadowBootstrapObservationRequest(
        Job("bootstrap-observation-001", "shadow"),
        source_job,
        0,
        source_blob,
        reference,
        persisted.receipt_id,
        persisted.artifact_set_id,
        persisted.command_slot_id,
        observation,
        limits,
    )
    raw = encode_shadow_bootstrap_observation_response(
        observation,
        [{"text": "你好", "words": ["你", "好"], "timestamp": [[10, 40], [50, 100]]}],
        [{"value": [[5, 110], [120, 150]]}],
    )
    return store, request, _Port(raw)


def test_collects_exact_source_once_and_commits_raw_and_projection_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, request, port = _fixture(monkeypatch)

    outcome = CollectShadowBootstrapObservationCommand(store, port).execute(request)

    assert outcome.state == "succeeded"
    assert port.calls == 1
    assert store.materializations == [request.source_blob]
    assert store.closed_materializations == 1
    assert len(store.successes) == 1
    assert len(store.successes[0].artifacts) == 2
    assert len(store.blobs) == 3  # source plus immutable raw and projection blobs
    raw_member, projection_member = store.successes[0].artifacts
    assert raw_member.artifact_type == "shadow_bootstrap_observation_raw_response"
    assert projection_member.artifact_type == "shadow_bootstrap_observation_projection"
    assert raw_member.scope.namespace == "autocut_observation"
    assert raw_member.scope.kind == "shadow_bootstrap"
    raw_payload = json.loads(raw_member.payload_json)
    projection_payload = json.loads(projection_member.payload_json)
    assert raw_payload["source_manifest"]["artifact_set_id"] == str(request.source_manifest_artifact_set_id)
    assert raw_payload["source_blob"]["object_id"] == str(request.source_blob.object_id)
    assert projection_payload["projection"]["trust_status"] == "untrusted"
    assert projection_payload["projection"]["authority_eligible"] is False
    assert projection_payload["projection"]["independent_anchor_count"] == 0
    assert projection_payload["raw_response_sha256"] == "sha256:" + hashlib.sha256(port.raw).hexdigest()


def test_replay_returns_receipt_without_materializing_or_dispatching_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, request, port = _fixture(monkeypatch)
    command = CollectShadowBootstrapObservationCommand(store, port)

    first = command.execute(request)
    replay = command.execute(request)

    assert first == replay
    assert port.calls == 1
    assert store.materializations == [request.source_blob]
    assert len(store.successes) == 1


def test_source_blob_substitution_is_rejected_before_claim_or_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, request, port = _fixture(monkeypatch)
    foreign = BlobRef(uuid4(), request.source_blob.content_hash, request.source_blob.byte_length, "video/mp4")

    with pytest.raises(CollectShadowBootstrapObservationError, match="source episode"):
        CollectShadowBootstrapObservationCommand(store, port).execute(
            dataclasses.replace(request, source_blob=foreign)
        )

    assert not store.claims
    assert not store.materializations
    assert port.calls == 0


def test_malformed_response_is_terminally_rejected_after_the_single_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, request, port = _fixture(monkeypatch)
    port.raw = b"not-json"

    outcome = CollectShadowBootstrapObservationCommand(store, port).execute(request)

    assert outcome.state == "denied"
    assert outcome.failure_code == "SHADOW_BOOTSTRAP_OBSERVATION_INVALID"
    assert port.calls == 1
    assert len(store.rejections) == 1
    assert not store.successes
    assert store.closed_materializations == 1


def test_unknown_dispatch_stays_running_without_a_terminal_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, request, port = _fixture(monkeypatch)

    def unknown(*_args: object) -> ShadowBootstrapObservationResult:
        port.calls += 1
        raise ShadowBootstrapObservationDispatchUnknownError("provider outcome unknown")

    port.observe = unknown  # type: ignore[method-assign]
    outcome = CollectShadowBootstrapObservationCommand(store, port).execute(request)

    assert outcome.state == "running"
    assert port.calls == 1
    assert not store.successes
    assert not store.rejections
    assert store.closed_materializations == 1


def test_non_shadow_jobs_are_rejected_before_claim_or_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, request, port = _fixture(monkeypatch)

    with pytest.raises(CollectShadowBootstrapObservationError, match="requires shadow"):
        CollectShadowBootstrapObservationCommand(store, port).execute(
            dataclasses.replace(request, job=Job("not-shadow", "test"))
        )

    assert not store.claims
    assert port.calls == 0
