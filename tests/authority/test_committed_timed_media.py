"""Pure exact-reader coverage for committed five-member timed-media evidence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from authority.local_run_resource import emit_locked_local_run_resource
from autocut_kernel.contracts.compiler.canonical import sha256_bytes
from autocut_kernel.media import CalibrationBinding
from autocut_kernel.media.root_evidence_codec import decode_root_media_evidence_bundle
from autocut_kernel.media.stage4_predecessor import admit_timed_speech_profile
from autocut_kernel.media.timed_evidence_codec import decode_calibration_binding
from autocut_kernel.media.types import canonical_sha256
from autocut_kernel.pipeline.committed_timed_media import (
    TimedMediaReadError,
    TimedMediaReadLimits,
    read_committed_timed_media_evidence,
)
from autocut_kernel.pipeline.prepare_timed_media_evidence_command import (
    PrepareTimedMediaEvidenceCommand,
    ProducedTimedMediaEvidence,
    resolve_committed_timed_media_request,
    timed_media_request_hash,
)
from autocut_kernel.registry.installed_local_run import decode_local_run_resource
from autocut_kernel.registry.installed_runtime import InstalledLocalRunProfileResolver
from autocut_kernel.registry.timed_speech import StoreAnchoredTimedSpeechProfileResolver
from autocut_kernel.store.models import (
    ArtifactMember,
    BlobRef,
    CommandOutcome,
    CommittedArtifactMemberReference,
    Job,
    PersistedCommittedArtifactMember,
    PersistedCommittedArtifactSet,
    artifact_set_hash,
)

from tests.authority.test_installed_runtime import _bootstrapped
from tests.authority.test_local_run_calibration import FakeAcceptedAnchorReader
from tests.authority.test_local_run_resource import _synthetic_accepted_sources
from tests.authority.test_shadow_context import _shadow_mapping
from tests.media.test_prepare_timed_media_evidence_command import (
    _bundle,
    _Producer,
    _request,
    _Store,
)


class _Lease:
    def __init__(self, reference: BlobRef, path: Path, owner: "_ReaderStore") -> None:
        self.reference = reference
        self.path = path
        self._owner = owner
        self._closed = False

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._owner.closed += 1
            self.path.unlink(missing_ok=True)


class _ReaderStore(_Store, FakeAcceptedAnchorReader):
    def __init__(self, anchor: object, root: Path) -> None:
        _Store.__init__(self)
        FakeAcceptedAnchorReader.__init__(self, anchor)  # type: ignore[arg-type]
        self.root = root
        self.record: PersistedCommittedArtifactSet | None = None
        self.closed = 0
        self.materialization_attempts = 0
        self.corrupt = False
        self.foreign_lease_reference = False

    def materialize_immutable_blob(self, job, reference, limits):  # type: ignore[no-untyped-def]
        del job
        self.materialization_attempts += 1
        assert reference.byte_length <= limits.effective_max_source_bytes
        raw = self.blobs[reference.object_id]
        path = self.root / f"{reference.object_id}.blob"
        path.write_bytes(b"corrupt" if self.corrupt else raw)
        leased = replace(reference, object_id=uuid4()) if self.foreign_lease_reference else reference
        return _Lease(leased, path, self)

    def read_committed_artifact_set(self, job, **expected):  # type: ignore[no-untyped-def]
        record = self.record
        assert record is not None
        self.read_call = (job, expected)
        return record


def _installed_resource(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import tests.authority.test_local_run_context as local_run_context
    import tests.authority.test_shadow_context as shadow_context
    from tests.authority.test_authority_profile_sources import _run_mapping

    original = _shadow_mapping

    def compatible(narrative: dict[str, Any]) -> dict[str, Any]:
        value = original(narrative)
        value["source_clock_policy"].update({
            "clock_id": "audio-stream-1",
            "time_base": {"numerator": 1, "denominator": 48_000},
        })
        return value

    def compatible_run(narrative: dict[str, Any], shadow: dict[str, Any]) -> dict[str, Any]:
        value = _run_mapping(narrative, shadow)
        guard = value["timed_speech_registry_entry"]["guard_policy"]
        assert isinstance(guard, dict)
        guard["word_gap_tick"] = 180 * 48
        guard["vad_merge_gap_tick"] = 120 * 48
        return value

    with monkeypatch.context() as context:
        context.setattr(shadow_context, "_shadow_mapping", compatible)
        context.setattr(local_run_context, "_run_mapping", compatible_run)
        sources, anchor = _synthetic_accepted_sources(tmp_path)
    raw = emit_locked_local_run_resource(
        **sources.options, store=FakeAcceptedAnchorReader(anchor)
    )
    return decode_local_run_resource(raw, expected_sha256=sha256_bytes(raw)), anchor


def _installed_producer(resource, *, chinese: bool = False):  # type: ignore[no-untyped-def]
    class InstalledProducer(_Producer):
        def prepare(self, request, source):  # type: ignore[no-untyped-def]
            produced = super().prepare(request, source)
            entry = resource.local_run.timed_speech_registry_entry
            native = {item.producer_kind: item for item in resource.local_run.native_timed_speech.producers}
            transcript_requirement = entry.transcript_requirement
            vad_requirement = entry.vad_requirement
            root = produced.root_bundle

            def context(value, requirement):  # type: ignore[no-untyped-def]
                return replace(
                    value,
                    clock_id=requirement.clock_id,
                    time_base=requirement.time_base,
                    producer_id=requirement.producer_id,
                    generation_policy_sha256=requirement.generation_policy_sha256,
                )

            transcript_context = context(root.transcript.context, transcript_requirement)
            vad_context = context(root.speech_activity.context, vad_requirement)
            transcript = replace(
                root.transcript,
                context=transcript_context,
                coverage=replace(
                    root.transcript.coverage,
                    clock_id=transcript_context.clock_id,
                    time_base=transcript_context.time_base,
                ),
                segments=tuple(replace(item, clock_id=transcript_context.clock_id) for item in root.transcript.segments),
                words=tuple(replace(item, clock_id=transcript_context.clock_id) for item in root.transcript.words),
                sentences=tuple(replace(item, clock_id=transcript_context.clock_id) for item in root.transcript.sentences),
            )
            if chinese:
                transcript = replace(
                    transcript,
                    words=(replace(transcript.words[0], text="中文证据") , *transcript.words[1:]),
                )
            speech = replace(
                root.speech_activity,
                context=vad_context,
                coverage=replace(
                    root.speech_activity.coverage,
                    clock_id=vad_context.clock_id,
                    time_base=vad_context.time_base,
                ),
                segments=tuple(replace(item, clock_id=vad_context.clock_id) for item in root.speech_activity.segments),
            )
            root = replace(root, transcript=transcript, speech_activity=speech)
            bindings: list[CalibrationBinding] = []
            for binding in produced.calibration_bindings:
                kind = None
                if binding.producer_id == produced.root_bundle.transcript.context.producer_id:
                    kind = "asr"
                elif binding.producer_id == produced.root_bundle.speech_activity.context.producer_id:
                    kind = "vad"
                if kind is None:
                    bindings.append(binding)
                    continue
                profile = native[kind]
                bindings.append(replace(
                    binding,
                    policy_sha256=profile.generation_policy_sha256,
                    detector_sha256=profile.detector_sha256,
                    calibration_record_sha256=profile.producer_record_sha256,
                    producer_id=profile.producer_id,
                    producer_version=profile.producer_version,
                    time_base=resource.local_run.source_clock_policy.time_base,
                    timing_error_bound_tick=profile.timing_error_bound_tick,
                    adapter_sha256=resource.local_run.native_timed_speech.native_port_identity_sha256,
                ))
            provenance = json.loads(produced.producer_provenance_json)
            for item, binding in zip(provenance["producer_identities"], bindings, strict=True):
                native_item = next(
                    (candidate for candidate in native.values()
                     if candidate.producer_id == binding.producer_id),
                    None,
                )
                item.update({
                    "producer_id": binding.producer_id,
                    "producer_policy_sha256": binding.policy_sha256,
                    "detector_sha256": binding.detector_sha256,
                    "calibration_record_sha256": binding.calibration_record_sha256,
                    "producer_version": binding.producer_version,
                    "timing_error_bound_tick": binding.timing_error_bound_tick,
                    "adapter_sha256": binding.adapter_sha256,
                })
                if native_item is not None:
                    item["calibration_policy_sha256"] = native_item.calibration_policy_sha256
            return ProducedTimedMediaEvidence(
                produced.producer_policy_sha256,
                root,
                tuple(bindings),
                produced.producer_policy_json,
                json.dumps(provenance, separators=(",", ":"), sort_keys=True),
            )

    return InstalledProducer(_bundle())


def _record(
    store: _ReaderStore,
    request,
    resource,
    *,
    chinese: bool = False,
) -> tuple[CommandOutcome, InstalledLocalRunProfileResolver]:  # type: ignore[no-untyped-def]
    resolver = InstalledLocalRunProfileResolver(resource)
    bootstrapped = _bootstrapped(resource)
    store.bootstrapped_reference = bootstrapped.reference
    store.bootstrapped_entry = bootstrapped.entry
    command = PrepareTimedMediaEvidenceCommand(
        store, _installed_producer(resource, chinese=chinese), StoreAnchoredTimedSpeechProfileResolver(resolver.snapshot)
    )
    result = command.execute(request)
    assert result.outcome.state == "succeeded"
    success = store.successes[-1]
    job_id, receipt_id, artifact_set_id = uuid4(), uuid4(), uuid4()
    members = tuple(
        PersistedCommittedArtifactMember(
            reference=CommittedArtifactMemberReference(
                receipt_id, artifact_set_id, ordinal, artifact.scope, artifact.artifact_type,
                artifact.logical_id, artifact.revision, artifact.content_hash,
            ),
            payload_json=artifact.payload_json,
            command_slot_id=success.command_slot_id,
        )
        for ordinal, artifact in enumerate(success.artifacts)
    )
    resolved = resolve_committed_timed_media_request(store, request)
    store.record = PersistedCommittedArtifactSet(
        request.job, job_id, success.command_slot_id, receipt_id, artifact_set_id,
        timed_media_request_hash(resolved, resolver.snapshot),
        "PrepareTimedMediaEvidence@2.1.3", "deterministic", artifact_set_hash(success.artifacts), members,
    )
    return CommandOutcome(success.command_slot_id, "succeeded", receipt_id=receipt_id,
                          artifact_set_id=artifact_set_id, job_id=job_id), resolver


def _limits(request) -> TimedMediaReadLimits:  # type: ignore[no-untyped-def]
    materialization = replace(
        request.materialization_limits,
        max_source_bytes=100_000,
        timed_speech_max_request_bytes=100_000,
        staging_quota_bytes=500_000,
    )
    return TimedMediaReadLimits(100_000, 500_000, 8, materialization)


def _case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, with_candidates: bool = True, chinese: bool = False):
    resource, anchor = _installed_resource(tmp_path, monkeypatch)
    store = _ReaderStore(anchor, tmp_path)
    request = _request(store, with_candidates=with_candidates)
    outcome, resolver = _record(store, request, resource, chinese=chinese)
    return store, request, outcome, resolver


def _rehash_member(record: PersistedCommittedArtifactSet, ordinal: int, payload: dict[str, object]) -> PersistedCommittedArtifactSet:
    members = list(record.members)
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    member = members[ordinal]
    members[ordinal] = replace(
        member,
        reference=replace(member.reference, content_hash="sha256:" + hashlib.sha256(raw.encode()).hexdigest()),
        payload_json=raw,
    )
    changed = tuple(members)
    artifacts = tuple(
        ArtifactMember(item.reference.artifact_type, item.reference.logical_id,
                       item.reference.revision, item.reference.scope,
                       item.reference.content_hash, item.payload_json)
        for item in changed
    )
    return replace(record, members=changed, set_hash=artifact_set_hash(artifacts))


def _blob(store: _ReaderStore, value: object, media_type: str) -> BlobRef:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    reference = BlobRef(uuid4(), "sha256:" + hashlib.sha256(raw).hexdigest(), len(raw), media_type)
    store.blobs[reference.object_id] = raw
    return reference


def _blob_mapping(reference: BlobRef) -> dict[str, object]:
    return {
        "object_id": str(reference.object_id),
        "content_hash": reference.content_hash,
        "byte_length": reference.byte_length,
        "media_type": reference.media_type,
    }


@pytest.mark.parametrize("with_candidates", (False, True))
def test_reader_replays_exact_producer_shaped_five_member_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_candidates: bool,
) -> None:
    store, request, outcome, resolver = _case(
        tmp_path, monkeypatch, with_candidates=with_candidates
    )
    before_read = store.closed

    value = read_committed_timed_media_evidence(
        store, request, outcome, authority_profile_resolver=resolver, limits=_limits(request)
    )

    assert value.record is store.record
    assert len(value.candidates) == (1 if with_candidates else 0)
    assert value.profile.entry == resolver.resource.local_run.timed_speech_registry_entry
    assert store.closed - before_read == 4 + len(value.candidates)


@pytest.mark.parametrize("ordinal", range(5))
def test_rehashed_each_fixed_member_is_independently_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ordinal: int,
) -> None:
    store, request, outcome, resolver = _case(tmp_path, monkeypatch)
    assert store.record is not None
    payload = json.loads(store.record.members[ordinal].payload_json)
    if ordinal == 0:
        payload["root_bundle_sha256"] = "sha256:" + "f" * 64
    elif ordinal == 1:
        payload["semantic_pack_sha256"] = "sha256:" + "f" * 64
    elif ordinal == 2:
        payload["transcript_calibration_sha256"] = "sha256:" + "f" * 64
    elif ordinal == 3:
        payload["facts_compiler_id"] = "forged-presentation-probe"
    else:
        payload["facts_sha256"] = "sha256:" + "f" * 64
    store.record = _rehash_member(store.record, ordinal, payload)

    with pytest.raises(TimedMediaReadError):
        read_committed_timed_media_evidence(
            store, request, outcome, authority_profile_resolver=resolver, limits=_limits(request)
        )


@pytest.mark.parametrize("field,value", [
    ("command_name", "ForeignCommand"),
    ("execution_kind", "generation"),
    ("request_hash", "sha256:" + "f" * 64),
    ("job", Job("foreign-job", "shadow")),
])
def test_reader_rejects_rehashed_foreign_record_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: object,
) -> None:
    store, request, outcome, resolver = _case(tmp_path, monkeypatch)
    assert store.record is not None
    store.record = replace(store.record, **{field: value})

    with pytest.raises(TimedMediaReadError):
        read_committed_timed_media_evidence(
            store, request, outcome, authority_profile_resolver=resolver, limits=_limits(request)
        )


def test_reader_rejects_declared_root_blob_ceiling_before_any_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, request, outcome, resolver = _case(tmp_path, monkeypatch)
    limits = _limits(request)
    assert store.record is not None
    root = json.loads(store.record.members[0].payload_json)
    root["blob"]["byte_length"] = limits.max_blob_bytes + 1
    store.record = _rehash_member(store.record, 0, root)
    before = store.closed
    before_attempts = store.materialization_attempts

    with pytest.raises(TimedMediaReadError, match="exceed byte ceilings"):
        read_committed_timed_media_evidence(
            store, request, outcome, authority_profile_resolver=resolver, limits=limits
        )

    assert store.closed == before
    assert store.materialization_attempts == before_attempts


def test_reader_rejects_total_blob_ceiling_before_any_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, request, outcome, resolver = _case(tmp_path, monkeypatch)
    limits = replace(_limits(request), max_total_blob_bytes=1)
    before = store.closed
    before_attempts = store.materialization_attempts

    with pytest.raises(TimedMediaReadError, match="exceed byte ceilings"):
        read_committed_timed_media_evidence(
            store, request, outcome, authority_profile_resolver=resolver, limits=limits
        )

    assert store.closed == before
    assert store.materialization_attempts == before_attempts


def test_corrupt_materialized_blob_is_rejected_and_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, request, outcome, resolver = _case(tmp_path, monkeypatch)
    store.corrupt = True
    before = store.closed
    before_attempts = store.materialization_attempts

    with pytest.raises(TimedMediaReadError, match="raw hash"):
        read_committed_timed_media_evidence(
            store, request, outcome, authority_profile_resolver=resolver, limits=_limits(request)
        )

    assert store.closed == before + 1
    assert store.materialization_attempts == before_attempts + 1
    assert not tuple(tmp_path.glob("*.blob"))


def test_foreign_materialized_reference_is_rejected_and_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, request, outcome, resolver = _case(tmp_path, monkeypatch)
    store.foreign_lease_reference = True
    before = store.closed
    before_attempts = store.materialization_attempts

    with pytest.raises(TimedMediaReadError, match="lease differs"):
        read_committed_timed_media_evidence(
            store, request, outcome, authority_profile_resolver=resolver, limits=_limits(request)
        )

    assert store.closed == before + 1
    assert store.materialization_attempts == before_attempts + 1
    assert not tuple(tmp_path.glob("*.blob"))


@pytest.mark.parametrize("field", ("plan_blob", "candidate_blobs"))
def test_rehashed_plan_or_candidate_blob_cannot_replace_replayed_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str,
) -> None:
    store, request, outcome, resolver = _case(tmp_path, monkeypatch)
    assert store.record is not None
    index = json.loads(store.record.members[1].payload_json)
    if field == "plan_blob":
        old = index["plan_blob"]
        raw = json.loads(store.blobs[UUID(old["object_id"])])
        raw["plans"][0]["windows"][0]["source_sha256"] = "sha256:" + "f" * 64
        ref = _blob(store, raw, old["media_type"])
        index["plan_blob"] = _blob_mapping(ref)
        index["plan_set_sha256"] = "sha256:" + hashlib.sha256(
            json.dumps(raw, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()
    else:
        old = index["candidate_blobs"][0]
        raw = json.loads(store.blobs[UUID(old["object_id"])])
        raw["candidate_window"]["source_sha256"] = "sha256:" + "f" * 64
        ref = _blob(store, raw, old["media_type"])
        index["candidate_blobs"] = [_blob_mapping(ref)]
    store.record = _rehash_member(store.record, 1, index)

    with pytest.raises(TimedMediaReadError):
        read_committed_timed_media_evidence(
            store, request, outcome, authority_profile_resolver=resolver, limits=_limits(request)
        )


def test_reader_wraps_foreign_accepted_anchor_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, request, outcome, resolver = _case(tmp_path, monkeypatch)
    anchor = store.anchor
    store.anchor = replace(
        anchor,
        aggregate=replace(
            anchor.aggregate,
            reference=replace(anchor.aggregate.reference, receipt_id=UUID(int=99)),
        ),
    )

    with pytest.raises(TimedMediaReadError, match="invalid"):
        read_committed_timed_media_evidence(
            store, request, outcome, authority_profile_resolver=resolver, limits=_limits(request)
        )


@pytest.mark.parametrize(
    "field",
    ("timing_error_bound_tick", "producer_version", "calibration_policy_sha256"),
)
def test_zero_candidate_rehashed_asr_identity_still_requires_installed_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str,
) -> None:
    store, request, outcome, resolver = _case(tmp_path, monkeypatch, with_candidates=False)
    assert store.record is not None
    root = json.loads(store.record.members[0].payload_json)
    binding = next(item for item in root["calibration_bindings"] if item["producer_id"] == "native-asr")
    if field == "timing_error_bound_tick":
        binding["timing_error_bound_tick"] += 1
    elif field == "producer_version":
        binding["producer_version"] = "forged-version"
    provenance_ref = root["producer_provenance_blob"]
    provenance = json.loads(store.blobs[UUID(provenance_ref["object_id"])])
    identity = next(item for item in provenance["producer_identities"] if item["producer_id"] == "native-asr")
    if field == "timing_error_bound_tick":
        identity["timing_error_bound_tick"] += 1
    elif field == "producer_version":
        identity["producer_version"] = "forged-version"
    else:
        identity["calibration_policy_sha256"] = "sha256:" + "f" * 64
    provenance_blob = _blob(store, provenance, provenance_ref["media_type"])
    root["producer_provenance_blob"] = _blob_mapping(provenance_blob)
    root["producer_provenance_sha256"] = canonical_sha256(provenance)
    store.record = _rehash_member(store.record, 0, root)

    profile = resolver.resolve(store)
    root_bundle = decode_root_media_evidence_bundle(
        json.loads(store.blobs[UUID(root["blob"]["object_id"])])
    )
    bindings = tuple(decode_calibration_binding(item) for item in root["calibration_bindings"])
    admission = admit_timed_speech_profile(
        profile.entry, profile.reference.content_hash, root_bundle, bindings
    )
    admission_payload = json.loads(store.record.members[2].payload_json)
    store.record = _rehash_member(
        store.record,
        2,
        {
            **admission.to_mapping(),
            "registry_member_reference": admission_payload["registry_member_reference"],
        },
    )

    with pytest.raises(TimedMediaReadError, match="installed accepted"):
        read_committed_timed_media_evidence(
            store, request, outcome, authority_profile_resolver=resolver, limits=_limits(request)
        )


def test_chinese_transcript_bytes_replay_without_collapsing_to_root_canonical_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, request, outcome, resolver = _case(tmp_path, monkeypatch, chinese=True)
    value = read_committed_timed_media_evidence(
        store, request, outcome, authority_profile_resolver=resolver, limits=_limits(request)
    )
    assert store.record is not None
    root = json.loads(store.record.members[0].payload_json)
    raw = store.blobs[UUID(root["blob"]["object_id"])]

    assert "中文证据".encode() in raw
    assert "sha256:" + hashlib.sha256(raw).hexdigest() != value.produced.root_bundle.canonical_hash
