"""Committed reader coverage for the CUDA-only timed-media predecessor."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import autocut_kernel.pipeline.committed_runtime_timed_media as runtime_reader_module
import pytest
from autocut_kernel.pipeline.committed_runtime_timed_media import (
    RuntimeTimedMediaReadError,
    read_committed_runtime_timed_media_evidence,
)
from autocut_kernel.pipeline.committed_timed_media import TimedMediaReadLimits
from autocut_kernel.pipeline.prepare_runtime_timed_media_evidence_command import (
    PrepareRuntimeTimedMediaEvidenceCommand,
)
from autocut_kernel.pipeline.prepare_timed_media_evidence_command import (
    resolve_committed_timed_media_request,
)
from autocut_kernel.store.models import (
    CommandOutcome,
    CommittedArtifactMemberReference,
    PersistedCommittedArtifactMember,
    PersistedCommittedArtifactSet,
    artifact_set_hash,
)

from tests.media.test_prepare_runtime_timed_media_evidence_command import (
    _installed_resolver,
    _runtime_projection,
    _runtime_request,
    _RuntimeProducer,
)
from tests.media.test_prepare_timed_media_evidence_command import _bundle, _Producer, _Store


class _Lease:
    def __init__(self, reference, path: Path) -> None:  # type: ignore[no-untyped-def]
        self.reference, self.path = reference, path

    def close(self) -> None:
        self.path.unlink(missing_ok=True)


class _ReaderStore(_Store):
    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = root
        self.record: PersistedCommittedArtifactSet | None = None

    def materialize_immutable_blob(self, job, reference, limits):  # type: ignore[no-untyped-def]
        del job
        assert reference.byte_length <= limits.effective_max_source_bytes
        path = self.root / f"{reference.object_id}.blob"
        path.write_bytes(self.blobs[reference.object_id])
        return _Lease(reference, path)

    def read_committed_artifact_set(self, job, **expected):  # type: ignore[no-untyped-def]
        assert self.record is not None and self.record.job == job
        return self.record


def _limits(request) -> TimedMediaReadLimits:  # type: ignore[no-untyped-def]
    return TimedMediaReadLimits(
        100_000,
        500_000,
        8,
        replace(
            request.timed_media_request.materialization_limits,
            max_source_bytes=100_000,
            timed_speech_max_request_bytes=100_000,
            staging_quota_bytes=500_000,
        ),
    )


def runtime_reader_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    store = _ReaderStore(tmp_path)
    request = _runtime_request(store)
    projection = _runtime_projection(request.timed_media_request)
    resolver, _ = _installed_resolver(monkeypatch, projection)
    result = PrepareRuntimeTimedMediaEvidenceCommand(
        store, _RuntimeProducer(_Producer(_bundle())), resolver
    ).execute(request)
    assert result.outcome.state == "succeeded"
    success = store.successes[-1]
    job_id, receipt_id, artifact_set_id = uuid4(), uuid4(), uuid4()
    members = tuple(
        PersistedCommittedArtifactMember(
            CommittedArtifactMemberReference(
                receipt_id, artifact_set_id, ordinal, artifact.scope, artifact.artifact_type,
                artifact.logical_id, artifact.revision, artifact.content_hash,
            ),
            artifact.payload_json,
            success.command_slot_id,
        )
        for ordinal, artifact in enumerate(success.artifacts)
    )
    store.record = PersistedCommittedArtifactSet(
        request.job,
        job_id,
        success.command_slot_id,
        receipt_id,
        artifact_set_id,
        request.request_hash_for(projection),
        "PrepareRuntimeTimedMediaEvidence@1.0.0",
        "deterministic",
        artifact_set_hash(success.artifacts),
        members,
    )
    outcome = CommandOutcome(
        success.command_slot_id, "succeeded", receipt_id=receipt_id,
        artifact_set_id=artifact_set_id, job_id=job_id,
    )
    return store, request, outcome, resolver


def test_reader_recomputes_cuda_admission_and_rejects_cpu_member_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, request, outcome, resolver = runtime_reader_case(tmp_path, monkeypatch)

    value = read_committed_runtime_timed_media_evidence(
        store, request, outcome, authority_resolver=resolver, limits=_limits(request)
    )

    assert value.projection.runtime_capability_id == "pc_cuda"
    assert value.admission.canonical_hash
    assert value.record.command_name == "PrepareRuntimeTimedMediaEvidence@1.0.0"
    assert len(value.record.members) == 5

    assert store.record is not None
    store.record = replace(
        store.record,
        command_name="PrepareTimedMediaEvidence@2.1.3",
    )
    with pytest.raises(RuntimeTimedMediaReadError, match="Store record"):
        read_committed_runtime_timed_media_evidence(
            store, request, outcome, authority_resolver=resolver, limits=_limits(request)
        )


def _runtime_output_for_reader(
    store, request, resolver  # type: ignore[no-untyped-def]
):
    resolved = resolve_committed_timed_media_request(store, request.timed_media_request)
    projection = resolver.resolve(store, request.runtime_measurement_identity)
    produced = _RuntimeProducer(_Producer(_bundle())).prepare(
        resolved, SimpleNamespace(reference=resolved.source_blob), projection
    )
    return resolved, projection, produced


@pytest.mark.parametrize(
    ("mutate", "message"),
    (
        (lambda value: value.__setitem__("build_audit_sha256", "sha256:" + "f" * 64), "fresh Store projection"),
        (lambda value: value["source_clock"].__setitem__("clock_id", "forged-clock"), "source clock"),
        (lambda value: value["timing"].__setitem__("word_gap_policy_sha256", "sha256:" + "f" * 64), "timing policies"),
        (lambda value: value["timing"].__setitem__("vad_merge_gap_milliseconds", -1), "timing gaps"),
        (lambda value: value["timing"].__setitem__("utterance_gap_milliseconds", 999), "timing gaps"),
        (lambda value: value["timing"].__setitem__("vad_merge_gap_milliseconds", 999), "timing gaps"),
        (lambda value: value["operation"].__setitem__("endpoint_url", "http://127.0.0.1:8080/v1/timed-speech-evidence"), "legacy"),
        (lambda value: value["operation"].__setitem__("provider_id", " "), "provider identity"),
        (lambda value: value["operation"].__setitem__("max_response_bytes", 0), "operation limits"),
        (lambda value: value.__setitem__("producers", []), "producers differ"),
    ),
)
def test_reader_replays_the_full_closed_cuda_authority_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate,
    message: str,
) -> None:  # type: ignore[no-untyped-def]
    store, request, _, resolver = runtime_reader_case(tmp_path, monkeypatch)
    _, projection, produced = _runtime_output_for_reader(store, request, resolver)
    authority = json.loads(json.dumps(produced.runtime_authority_mapping))
    mutate(authority)

    with pytest.raises(RuntimeTimedMediaReadError, match=message):
        runtime_reader_module._validate_runtime_authority_mapping(
            authority,
            projection,
            static_policy_sha256=resolver.static_operation_policy_sha256,
        )
