from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from types import SimpleNamespace
from uuid import UUID, uuid4

import autocut_kernel.pipeline.collect_shadow_bootstrap_observation_command as kernel_command
import pytest
from autocut_kernel.media.shadow_bootstrap_observation import (
    decode_shadow_bootstrap_observation_response,
    encode_shadow_bootstrap_observation_response,
    project_shadow_bootstrap_observation,
)
from autocut_kernel.media.types import TickRange, TimeBase
from autocut_kernel.pipeline.collect_shadow_bootstrap_observation_command import (
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

import auto_cut_bot.pipeline.runtime.shadow_bootstrap_entry as entry_module
from auto_cut_bot.pipeline.runtime.shadow_bootstrap_entry import (
    ShadowBootstrapEpisodeOutOfRangeError,
    ShadowBootstrapObservationEntryService,
    ShadowBootstrapSourceNotFoundError,
    ShadowBootstrapSourceNotReadyError,
    ShadowBootstrapSourceProfileError,
)
from auto_cut_bot.pipeline.runtime.source_prep_stage import source_prep_kernel_idempotency_key
from auto_cut_bot.pipeline.source_prep import PersistedPreparedSources

RUN_ID = "pipeline_run_" + "a" * 32
LIMITS = MaterializationLimits(1_024, 1_024, 128, 4_096)
_DEFAULT_OUTCOME = object()


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


@dataclass
class Lease:
    reference: BlobRef
    closed: bool = False

    def close(self) -> None:
        self.closed = True


@dataclass
class Store:
    source_outcome: CommandOutcome | None
    persisted: PersistedWholeSeriesSourceManifest
    reads: list[tuple[Job, str]] = field(default_factory=list)
    claims: list[CommandClaim] = field(default_factory=list)
    successes: list[CommandSuccess] = field(default_factory=list)
    rejections: list[CommandRejection] = field(default_factory=list)
    materializations: list[Lease] = field(default_factory=list)

    def read_outcome(self, job: Job, key: str) -> CommandOutcome | None:
        self.reads.append((job, key))
        return self.source_outcome

    def read_whole_series_source_manifest(
        self, job: Job, artifact_set_id: UUID
    ) -> PersistedWholeSeriesSourceManifest:
        assert artifact_set_id == self.persisted.artifact_set_id
        return self.persisted

    def claim_command(self, claim: CommandClaim) -> CommandOutcome:
        self.claims.append(claim)
        return CommandOutcome(uuid4(), "running", is_fresh_claim=True)

    def materialize_immutable_blob(
        self, job: Job, reference: BlobRef, limits: MaterializationLimits
    ) -> Lease:
        assert job == self.persisted.source_job
        assert limits == LIMITS
        lease = Lease(reference)
        self.materializations.append(lease)
        return lease

    def put_immutable_blob(
        self, job: Job, *, content: bytes, content_hash: str, media_type: str
    ) -> BlobRef:
        assert job == self.persisted.source_job
        assert content_hash == _digest(content)
        return BlobRef(uuid4(), content_hash, len(content), media_type)

    def commit_command_success(self, success: CommandSuccess) -> CommandOutcome:
        self.successes.append(success)
        return CommandOutcome(success.command_slot_id, "succeeded", receipt_id=uuid4(), artifact_set_id=uuid4())

    def commit_command_rejection(self, rejection: CommandRejection) -> CommandOutcome:
        self.rejections.append(rejection)
        return CommandOutcome(rejection.command_slot_id, rejection.outcome, receipt_id=uuid4())


class Port:
    def __init__(self, *, unknown: bool = False) -> None:
        self.unknown = unknown
        self.requests: list[object] = []

    def observe(self, request: object, source: Lease) -> object:
        self.requests.append(request)
        if self.unknown:
            raise ShadowBootstrapObservationDispatchUnknownError("lost response")
        assert getattr(request, "source").source_sha256 == source.reference.content_hash
        raw = encode_shadow_bootstrap_observation_response(
            request,
            [{"text": "hello", "words": ["hello"], "timestamp": [[10, 40]]}],
            [{"value": [[5, 50]]}],
        )
        return project_shadow_bootstrap_observation(
            decode_shadow_bootstrap_observation_response(raw, request)
        )


def _bundle(
    *, source_job: Job, source_blob: BlobRef, reference: WholeSeriesSourceManifestReference,
    receipt_id: UUID, artifact_set_id: UUID, command_slot_id: UUID, with_episode: bool = True,
) -> tuple[PersistedPreparedSources, object]:
    clock = SimpleNamespace(
        clock_id="audio-30k",
        time_base=TimeBase(1, 30_000),
        origin_tick=0,
        end_tick=6_000,
        full_range=TickRange(0, 6_000),
    )
    episode = SimpleNamespace(
        proxy_blob=source_blob,
        media_probe=SimpleNamespace(
            source=SimpleNamespace(source_id="episode-001", content_sha256=source_blob.content_hash),
            audio_sample_boundaries=SimpleNamespace(context=clock),
        ),
    )
    # The source-prep reader is a separately tested strict decoder.  This entry
    # test uses an exact bundle shell to isolate whether the entry preserves its
    # already-verified provenance when it builds the Kernel command request.
    bundle = object.__new__(PersistedPreparedSources)
    object.__setattr__(bundle, "prepared", SimpleNamespace(episodes=(episode,) if with_episode else ()))
    object.__setattr__(bundle, "source_job", source_job)
    object.__setattr__(bundle, "kernel_job_id", uuid4())
    object.__setattr__(bundle, "receipt_id", receipt_id)
    object.__setattr__(bundle, "artifact_set_id", artifact_set_id)
    object.__setattr__(bundle, "command_slot_id", command_slot_id)
    object.__setattr__(bundle, "artifact_reference", reference)
    return bundle, episode


def _case(
    *,
    source_outcome: CommandOutcome | None | object = _DEFAULT_OUTCOME,
    with_episode: bool = True,
):
    job = Job(RUN_ID, "shadow")
    source_bytes = b"committed-shadow-source"
    blob = BlobRef(uuid4(), _digest(source_bytes), len(source_bytes), "video/mp4")
    reference = WholeSeriesSourceManifestReference(
        ArtifactScope("pipeline", "job", RUN_ID),
        "whole_series_source_manifest",
        1,
        canonical_payload_hash("{}"),
    )
    receipt_id, artifact_set_id, command_slot_id = uuid4(), uuid4(), uuid4()
    outcome = (
        CommandOutcome(command_slot_id, "succeeded", receipt_id=receipt_id, artifact_set_id=artifact_set_id)
        if source_outcome is _DEFAULT_OUTCOME
        else source_outcome
    )
    persisted = PersistedWholeSeriesSourceManifest(
        reference, "{}", (blob,), uuid4(), receipt_id, artifact_set_id, command_slot_id, job
    )
    bundle, episode = _bundle(
        source_job=job,
        source_blob=blob,
        reference=reference,
        receipt_id=receipt_id,
        artifact_set_id=artifact_set_id,
        command_slot_id=command_slot_id,
        with_episode=with_episode,
    )
    return Store(outcome, persisted), bundle, episode  # type: ignore[arg-type]


def _service(store: Store, port: Port) -> ShadowBootstrapObservationEntryService:
    return ShadowBootstrapObservationEntryService(
        store, port, materialization_limits=LIMITS, max_response_bytes=16_384
    )


def test_rebuilds_complete_source_manifest_provenance_before_one_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, bundle, episode = _case()
    port = Port()
    reader_calls: list[dict[str, object]] = []

    def read_bundle(*_args: object, **kwargs: object) -> PersistedPreparedSources:
        reader_calls.append(kwargs)
        return bundle

    monkeypatch.setattr(entry_module, "read_persisted_prepared_sources_bundle", read_bundle)
    monkeypatch.setattr(
        kernel_command, "decode_source_manifest", lambda *_args: SimpleNamespace(episodes=(episode,))
    )

    outcome = _service(store, port).collect(RUN_ID, 0)

    assert outcome.state == "succeeded"
    assert store.reads == [(Job(RUN_ID, "shadow"), source_prep_kernel_idempotency_key(RUN_ID))]
    assert reader_calls == [{
        "job": Job(RUN_ID, "shadow"),
        "outcome": store.source_outcome,
        "artifact_scope": ArtifactScope("pipeline", "job", RUN_ID),
        "artifact_revision": 1,
    }]
    assert len(port.requests) == 1
    request = port.requests[0]
    assert request.source.source_id == "episode-001"
    assert request.source.source_sha256 == bundle.prepared.episodes[0].proxy_blob.content_hash
    assert request.source.clock_id == "audio-30k"
    assert request.source.time_base == TimeBase(1, 30_000)
    assert request.source.source_range == TickRange(0, 6_000)
    assert store.claims[0].job == Job(RUN_ID, "shadow")
    assert store.materializations[0].closed is True


@pytest.mark.parametrize(
    ("outcome", "error"),
    [
        (None, ShadowBootstrapSourceNotFoundError),
        (CommandOutcome(uuid4(), "pending"), ShadowBootstrapSourceNotReadyError),
        (CommandOutcome(uuid4(), "running"), ShadowBootstrapSourceNotReadyError),
        (CommandOutcome(uuid4(), "denied", receipt_id=uuid4()), ShadowBootstrapSourceNotReadyError),
    ],
)
def test_missing_or_non_success_source_never_dispatches(
    outcome: CommandOutcome | None, error: type[Exception]
) -> None:
    store, _bundle_value, _episode = _case(source_outcome=outcome)
    port = Port()

    with pytest.raises(error):
        _service(store, port).collect(RUN_ID, 0)

    assert port.requests == []
    assert store.claims == []


def test_non_shadow_persisted_provenance_never_dispatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, bundle, _episode = _case()
    object.__setattr__(store.persisted, "source_job", Job(RUN_ID, "test"))
    monkeypatch.setattr(entry_module, "read_persisted_prepared_sources_bundle", lambda *_a, **_k: bundle)
    port = Port()

    with pytest.raises(ShadowBootstrapSourceProfileError):
        _service(store, port).collect(RUN_ID, 0)

    assert port.requests == []
    assert store.claims == []


def test_out_of_range_episode_never_dispatches(monkeypatch: pytest.MonkeyPatch) -> None:
    store, bundle, _episode = _case(with_episode=False)
    monkeypatch.setattr(entry_module, "read_persisted_prepared_sources_bundle", lambda *_a, **_k: bundle)
    port = Port()

    with pytest.raises(ShadowBootstrapEpisodeOutOfRangeError):
        _service(store, port).collect(RUN_ID, 0)

    assert port.requests == []
    assert store.claims == []


def test_unknown_dispatch_returns_running_without_terminal_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, bundle, episode = _case()
    port = Port(unknown=True)
    monkeypatch.setattr(entry_module, "read_persisted_prepared_sources_bundle", lambda *_a, **_k: bundle)
    monkeypatch.setattr(
        kernel_command, "decode_source_manifest", lambda *_args: SimpleNamespace(episodes=(episode,))
    )

    outcome = _service(store, port).collect(RUN_ID, 0)

    assert outcome.state == "running"
    assert outcome.receipt_id is None
    assert len(port.requests) == 1
    assert store.successes == []
    assert store.rejections == []
