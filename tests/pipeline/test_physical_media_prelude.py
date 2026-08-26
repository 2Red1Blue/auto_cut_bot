"""Real Source/VLM resolver and Command/reader; synthetic Store, no native/DB."""

import asyncio
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import uuid4

import pytest
from autocut_kernel.media.physical_root import PhysicalRootMediaEvidence
from autocut_kernel.media.timed_evidence import CalibrationBinding
from autocut_kernel.media.types import canonical_sha256
from autocut_kernel.pipeline.committed_physical_media import (
    PhysicalMediaReadError,
    PhysicalMediaReadLimits,
    read_committed_physical_media_evidence,
)
from autocut_kernel.pipeline.physical_media_contract import (
    PHYSICAL_KIND_ORDER,
    PHYSICAL_PROVENANCE_SCHEMA,
    PreparePhysicalMediaEvidenceRequest,
    ProducedPhysicalMediaEvidence,
    physical_json,
)
from autocut_kernel.pipeline.prepare_physical_media_evidence_command import (
    PreparePhysicalMediaEvidenceCommand,
    resolve_physical_media_request,
)
from autocut_kernel.pipeline.prepare_timed_media_evidence_command import (
    TimedMediaEvidenceProducerError,
)
from autocut_kernel.store.models import (
    CommandOutcome,
    CommittedArtifactMemberReference,
    MaterializationLimits,
    PersistedCommittedArtifactMember,
    PersistedCommittedArtifactSet,
    artifact_set_hash,
    canonical_payload_hash,
)

from tests.media.test_prepare_timed_media_evidence_command import (
    HASH_A,
    HASH_B,
    HASH_C,
    _bundle,
    _rebind_root_bundle,
    _request,
    _Store,
)


def _sha(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


class Lease:
    def __init__(self, store, reference, path):
        self.reference, self.path, self.store, self.closed = reference, path, store, False

    def close(self):
        if not self.closed:
            self.closed = True
            self.path.unlink()
            self.store.closed_materializations += 1


class Store(_Store):
    def __init__(self, directory):
        super().__init__()
        self.directory, self.lock = directory, threading.Lock()
        self.claim_by_slot, self.records = {}, {}
        self.record_override = None
        self.commit_fault = None
        self.puts = []
        self.reads = []
        self.lease_corrupt = False
        self.lease_foreign = False

    def read_bootstrapped_timed_speech_profile(self, _snapshot):
        raise AssertionError("physical prelude cannot resolve speech authority")

    def claim_command(self, claim):
        with self.lock:
            self.claims.append(claim)
            existing = self.outcomes.get(claim.idempotency_key)
            if existing is not None:
                assert self.claim_by_slot[existing.command_slot_id] == claim
                return replace(existing, is_fresh_claim=False)
            outcome = CommandOutcome(uuid4(), "running", is_fresh_claim=True, job_id=self.source_manifest.job_id)
            self.outcomes[claim.idempotency_key] = outcome
            self.claim_by_slot[outcome.command_slot_id] = claim
            return outcome

    def materialize_immutable_blob(self, job, reference, limits):
        assert job == self.source_manifest.source_job
        assert reference.byte_length <= limits.effective_max_source_bytes
        self.materializations.append(reference)
        path = self.directory / f"{uuid4()}.private"
        path.write_bytes(b"corrupt" if self.lease_corrupt else self.blobs[reference.object_id])
        return Lease(self, replace(reference, object_id=uuid4()) if self.lease_foreign else reference, path)

    def put_immutable_blob(self, job, *, content, content_hash, media_type):
        self.puts.append(content)
        return super().put_immutable_blob(job, content=content, content_hash=content_hash, media_type=media_type)

    def commit_command_success(self, success):
        if self.commit_fault == "running":
            raise OSError("ambiguous commit")
        claim = self.claim_by_slot[success.command_slot_id]
        outcome = CommandOutcome(success.command_slot_id, "succeeded", receipt_id=uuid4(),
                                 artifact_set_id=uuid4(), job_id=self.source_manifest.job_id)
        members = tuple(PersistedCommittedArtifactMember(CommittedArtifactMemberReference(
            outcome.receipt_id, outcome.artifact_set_id, ordinal, member.scope, member.artifact_type,
            member.logical_id, member.revision, member.content_hash), member.payload_json, outcome.command_slot_id)
            for ordinal, member in enumerate(success.artifacts))
        self.records[outcome.artifact_set_id] = PersistedCommittedArtifactSet(claim.job, outcome.job_id,
            outcome.command_slot_id, outcome.receipt_id, outcome.artifact_set_id, claim.request_hash,
            claim.command_name, claim.execution_kind, success.set_hash, members)
        self.successes.append(success)
        self.outcomes[claim.idempotency_key] = outcome
        if self.commit_fault == "committed":
            raise OSError("ambiguous commit")
        return outcome

    def commit_command_rejection(self, rejection):
        result = super().commit_command_rejection(rejection)
        result = replace(result, job_id=self.source_manifest.job_id)
        self._replace_slot(result)
        return result

    def read_committed_artifact_set(self, job, **expected):
        self.reads.append((job, expected))
        return self.record_override or self.records[expected["artifact_set_id"]]


class Producer:
    def __init__(self, produced):
        self.produced, self.calls, self.error, self.hook = produced, 0, None, None

    def prepare(self, resolved, lease):
        self.calls += 1
        assert lease.reference == resolved.source_blob and lease.path.read_bytes() == b"committed source"
        if self.hook:
            self.hook()
        if self.error:
            raise self.error
        return self.produced


def physical_case(tmp_path):
    store = Store(tmp_path)
    parent = _request(store)
    # Reuse only six physical facts from the existing Source fixture. No
    # speech producer/admission is invoked by this test or the new Command.
    template = _rebind_root_bundle(_bundle(), parent.frame_pts_index, parent.audio_sample_boundaries)
    sets = (template.frame_pts_index, template.audio_sample_boundaries, template.shot_boundaries,
            template.scene_boundaries, template.visual_validity, template.subtitle_cues)
    bindings = tuple(CalibrationBinding(item.context.generation_policy_sha256, HASH_B, HASH_C,
        item.context.producer_id, "1.0.0", item.context.time_base, 1, True, None) for item in sets)
    calibrations = [{"producer_kind": kind, "producer_id": item.producer_id, "producer_version": item.producer_version,
        "generation_policy_sha256": item.policy_sha256, "detector_sha256": item.detector_sha256,
        "calibration_policy_sha256": HASH_A, "calibration_record_sha256": item.calibration_record_sha256,
        "timing_error_bound_microseconds": 1} for kind, item in zip(PHYSICAL_KIND_ORDER, bindings, strict=True)]
    policy = {"policy_id": "synthetic-physical-策略", "calibrations": calibrations}
    request = PreparePhysicalMediaEvidenceRequest(parent, canonical_sha256(policy), 1_000_000, 1_000_000)
    resolved = resolve_physical_media_request(store, request)
    root = PhysicalRootMediaEvidence(resolved.physical_root_id, template.source_id, template.source_sha256,
        resolved.source_manifest_sha256, resolved.root_input_manifest_sha256, template.frame_pts_index,
        template.shot_boundaries, template.scene_boundaries, template.audio_sample_boundaries,
        template.visual_validity, template.subtitle_cues)
    identities = [{"producer_kind": kind, "producer_id": item.producer_id, "producer_version": item.producer_version,
        "producer_policy_sha256": item.policy_sha256, "detector_sha256": item.detector_sha256,
        "calibration_policy_sha256": HASH_A, "calibration_record_sha256": item.calibration_record_sha256,
        "timing_error_bound_tick": item.timing_error_bound_tick, "adapter_sha256": item.adapter_sha256}
        for kind, item in zip(PHYSICAL_KIND_ORDER, bindings, strict=True)]
    trace = [{"producer_kind": "probe", "executable": "synthetic-ffprobe", "executable_sha256": HASH_A,
              "version_evidence_sha256": HASH_A, "argv_sha256": HASH_A, "stdout_sha256": HASH_A, "stderr_sha256": HASH_A}]
    provenance = {"schema_version": PHYSICAL_PROVENANCE_SCHEMA, "source_provenance_sha256": resolved.source_provenance_sha256,
                  "producer_identities": identities, "tool_invocations": trace, "tool_trace_sha256": canonical_sha256(trace)}
    produced = ProducedPhysicalMediaEvidence(root, bindings, physical_json(policy), physical_json(provenance))
    return store, request, Producer(produced)


def _limits():
    return PhysicalMediaReadLimits(1_000_000, 1_000_000, MaterializationLimits(1_000_000, 1_000_000, 1024, 2_000_000))


def _committed(tmp_path):
    store, request, producer = physical_case(tmp_path)
    command = PreparePhysicalMediaEvidenceCommand(store, producer)
    result = command.execute(request)
    assert result.outcome.state == "succeeded", result.outcome.failure_detail_json
    return store, request, producer, command, result.outcome


def _rewrite(record, artifacts):
    members = tuple(PersistedCommittedArtifactMember(CommittedArtifactMemberReference(
        record.receipt_id, record.artifact_set_id, ordinal, item.scope, item.artifact_type,
        item.logical_id, item.revision, item.content_hash), item.payload_json, record.command_slot_id)
        for ordinal, item in enumerate(artifacts))
    return replace(record, members=members, set_hash=artifact_set_hash(tuple(artifacts)))


def _rewrite_payload(record, ordinal, mapping):
    artifacts = list(record.artifacts)
    raw = physical_json(mapping)
    artifacts[ordinal] = replace(artifacts[ordinal], payload_json=raw, content_hash=canonical_payload_hash(raw))
    return _rewrite(record, artifacts)


def test_success_three_members_one_root_blob_exact_reader_and_replay(tmp_path):
    store, request, producer, command, outcome = _committed(tmp_path)
    resolved = resolve_physical_media_request(store, request)
    record = store.records[outcome.artifact_set_id]
    assert len(record.members) == 3 and len(store.puts) == 1
    assert all(member.reference.revision == 1 and resolved.request_hash[7:] in member.reference.logical_id for member in record.members)
    payload = json.loads(record.members[0].payload_json)
    assert payload["request"] == resolved.canonical_payload()
    assert payload["request"]["request"]["parent"] == request.parent.canonical_payload()
    assert "producer_policy" in payload and "producer_provenance" in payload
    assert not any(key.endswith("_blob") for key in payload)
    assert store.closed_materializations == 1
    before = len(store.materializations)
    replay = command.execute(request)
    assert replay.outcome == outcome and producer.calls == 1 and len(store.materializations) == before
    evidence = read_committed_physical_media_evidence(store, request, outcome, limits=_limits())
    assert evidence.produced == producer.produced
    assert evidence.certificate.root_evidence_sha256 == producer.produced.physical_root.canonical_hash
    assert len(store.materializations) == before + 1
    assert store.materializations[-1].object_id != request.parent.source_blob.object_id
    assert store.closed_materializations == 2 and producer.calls == 1 and not store.rejections


def test_same_request_concurrent_nonfresh_running_never_dispatches_twice(tmp_path):
    store, request, producer = physical_case(tmp_path)
    command = PreparePhysicalMediaEvidenceCommand(store, producer)
    started, release = threading.Event(), threading.Event()
    producer.hook = lambda: (started.set(), release.wait(3))
    with ThreadPoolExecutor(max_workers=1) as pool:
        winner = pool.submit(command.execute, request)
        assert started.wait(2)
        try:
            loser = command.execute(request)
            assert loser.outcome.state == "running" and not loser.outcome.is_fresh_claim
            assert producer.calls == 1 and len(store.materializations) == 1
        finally:
            release.set()
        success = winner.result()
    assert command.execute(request).outcome == success.outcome and producer.calls == 1


@pytest.mark.parametrize("fault", ["committed", "running"])
def test_ambiguous_commit_propagates_without_rejection_or_redispatch(tmp_path, fault):
    store, request, producer = physical_case(tmp_path)
    store.commit_fault = fault
    command = PreparePhysicalMediaEvidenceCommand(store, producer)
    with pytest.raises(OSError, match="ambiguous commit"):
        command.execute(request)
    assert store.closed_materializations == 1 and not store.rejections
    replay = command.execute(request)
    assert replay.outcome.state == ("succeeded" if fault == "committed" else "running")
    assert producer.calls == 1 and len(store.materializations) == 1


@pytest.mark.parametrize("error,state", [
    (ValueError("invalid physical result"), "denied"), (OSError("lost detector"), "failed"),
    (TimedMediaEvidenceProducerError("BUSY", "busy", outcome="failed"), "failed"),
    (asyncio.CancelledError(), "running"),
])
def test_failure_and_unknown_claim_never_redispatch_and_always_close(tmp_path, error, state):
    store, request, producer = physical_case(tmp_path)
    producer.error = error
    command = PreparePhysicalMediaEvidenceCommand(store, producer)
    if isinstance(error, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            command.execute(request)
    else:
        assert command.execute(request).outcome.state == state
    assert command.execute(request).outcome.state == state
    assert producer.calls == 1 and store.closed_materializations == 1 and not store.successes


@pytest.mark.parametrize("cap", ["source", "evidence", "metadata"])
def test_write_caps_refuse_before_relevant_io(tmp_path, cap):
    store, request, producer = physical_case(tmp_path)
    if cap == "source":
        request = replace(request, parent=replace(request.parent, materialization_limits=MaterializationLimits(1, 1, 1, 1)))
    else:
        request = replace(request, **{f"max_{cap}_bytes": 1})
        resolved = resolve_physical_media_request(store, request)
        producer.produced = replace(producer.produced, physical_root=replace(producer.produced.physical_root,
            physical_root_id=resolved.physical_root_id, root_input_manifest_sha256=resolved.root_input_manifest_sha256))
    result = PreparePhysicalMediaEvidenceCommand(store, producer).execute(request)
    assert result.outcome.state == "denied" and not store.puts
    assert producer.calls == (0 if cap == "source" else 1)
    assert len(store.materializations) == (0 if cap == "source" else 1)


@pytest.mark.parametrize("role", range(6))
@pytest.mark.parametrize("field", ["producer_id", "producer_version", "producer_policy_sha256", "detector_sha256",
                                    "calibration_policy_sha256", "calibration_record_sha256", "timing_error_bound_tick", "adapter_sha256"])
def test_every_role_identity_must_match_frozen_policy_context_and_binding(tmp_path, role, field):
    store, request, producer = physical_case(tmp_path)
    value = json.loads(producer.produced.producer_provenance_json)
    identity = value["producer_identities"][role]
    identity[field] = (2 if field == "timing_error_bound_tick" else
                       "foreign" if field in {"producer_id", "producer_version"} else "sha256:" + "9" * 64)
    producer.produced = replace(producer.produced, producer_provenance_json=physical_json(value))
    result = PreparePhysicalMediaEvidenceCommand(store, producer).execute(request)
    assert result.outcome.state == "denied" and not store.puts and store.closed_materializations == 1


@pytest.mark.parametrize("mutation", ["root_id", "root_input", "manifest", "policy", "calibration", "trace_hash", "trace_extra"])
def test_invalid_root_policy_calibration_or_trace_cannot_commit(tmp_path, mutation):
    store, request, producer = physical_case(tmp_path)
    produced = producer.produced
    if mutation in {"root_id", "root_input", "manifest"}:
        field = {"root_id": "physical_root_id", "root_input": "root_input_manifest_sha256", "manifest": "source_manifest_sha256"}[mutation]
        produced = replace(produced, physical_root=replace(produced.physical_root, **{field: HASH_A}))
    elif mutation == "policy":
        policy = json.loads(produced.producer_policy_json)
        policy["policy_id"] = "changed"
        produced = replace(produced, producer_policy_json=physical_json(policy))
    elif mutation == "calibration":
        produced = replace(produced, calibration_bindings=tuple(reversed(produced.calibration_bindings)))
    else:
        provenance = json.loads(produced.producer_provenance_json)
        if mutation == "trace_hash":
            provenance["tool_trace_sha256"] = HASH_A
        else:
            provenance["tool_invocations"][0]["untrusted"] = True
            provenance["tool_trace_sha256"] = canonical_sha256(provenance["tool_invocations"])
        with pytest.raises(ValueError):
            replace(produced, producer_provenance_json=physical_json(provenance))
        return
    producer.produced = produced
    assert PreparePhysicalMediaEvidenceCommand(store, producer).execute(request).outcome.state == "denied"
    assert not store.puts


@pytest.mark.parametrize("mutation", ["source", "vlm"])
def test_actual_committed_input_reread_rejects_before_claim_or_producer(tmp_path, mutation):
    store, request, producer = physical_case(tmp_path)
    if mutation == "source":
        store.source_manifest = replace(store.source_manifest, command_slot_id=uuid4())
    else:
        pack = request.parent.semantic_pack
        request = replace(request, parent=replace(request.parent, semantic_pack=replace(pack, raw_response_sha256=HASH_A)))
    with pytest.raises(ValueError):
        PreparePhysicalMediaEvidenceCommand(store, producer).execute(request)
    assert not store.claims and not store.materializations and not producer.calls


@pytest.mark.parametrize("field", ["command_slot_id", "receipt_id", "artifact_set_id", "job_id"])
def test_reader_checks_all_outcome_identifiers_explicitly(tmp_path, field):
    store, request, _producer, _command, outcome = _committed(tmp_path)
    store.record_override = store.records[outcome.artifact_set_id]
    count = len(store.materializations)
    with pytest.raises(PhysicalMediaReadError):
        read_committed_physical_media_evidence(store, request, replace(outcome, **{field: uuid4()}), limits=_limits())
    assert len(store.materializations) == count


@pytest.mark.parametrize("state", ["running", "failed", "denied", "succeeded"])
def test_invalid_outcomes_reject_before_source_or_store_read(tmp_path, state):
    store, request, producer = physical_case(tmp_path)
    reads = store.semantic_reads
    with pytest.raises(PhysicalMediaReadError):
        read_committed_physical_media_evidence(store, request, CommandOutcome(uuid4(), state), limits=_limits())
    assert store.semantic_reads == reads and not store.reads and not producer.calls


@pytest.mark.parametrize("mutation", ["type", "logical_id", "scope", "revision", "missing", "reversed", "request", "request_bool", "policy", "provenance", "probe", "certificate"])
def test_rehashed_committed_member_tampering_is_independently_rejected(tmp_path, mutation):
    store, request, producer, _command, outcome = _committed(tmp_path)
    record = store.records[outcome.artifact_set_id]
    artifacts = list(record.artifacts)
    if mutation in {"type", "logical_id", "scope", "revision"}:
        field = "artifact_type" if mutation == "type" else mutation
        value = (replace(artifacts[0].scope, key="foreign") if mutation == "scope" else
                 2 if mutation == "revision" else "foreign")
        artifacts[0] = replace(artifacts[0], **{field: value})
        changed = _rewrite(record, artifacts)
    elif mutation in {"missing", "reversed"}:
        changed = _rewrite(record, artifacts[:2] if mutation == "missing" else list(reversed(artifacts)))
    else:
        ordinal = 1 if mutation == "probe" else 2 if mutation == "certificate" else 0
        payload = json.loads(artifacts[ordinal].payload_json)
        if mutation in {"probe", "certificate"}:
            payload["foreign"] = True
        elif mutation == "request":
            payload["request"]["request"]["parent"]["episode_index"] = 99
        elif mutation == "request_bool":
            payload["request"]["request"]["parent"]["artifact_revision"] = True
        else:
            payload[f"producer_{mutation}"]["foreign"] = True
        changed = _rewrite_payload(record, ordinal, payload)
    store.record_override = changed
    with pytest.raises(PhysicalMediaReadError):
        read_committed_physical_media_evidence(store, request, outcome, limits=_limits())
    assert producer.calls == 1 and store.closed_materializations == len(store.materializations)


@pytest.mark.parametrize("cap", ["evidence", "metadata", "total_metadata"])
def test_reader_caps_precede_blob_materialization(tmp_path, cap):
    store, request, _producer, _command, outcome = _committed(tmp_path)
    limits = _limits()
    if cap == "evidence":
        limits = replace(limits, max_evidence_bytes=1)
    else:
        sizes = [len(member.payload_json.encode()) for member in store.records[outcome.artifact_set_id].members]
        limits = replace(limits, max_metadata_bytes=1 if cap == "metadata" else max(sizes))
    count = len(store.materializations)
    with pytest.raises(PhysicalMediaReadError):
        read_committed_physical_media_evidence(store, request, outcome, limits=limits)
    assert len(store.materializations) == count


@pytest.mark.parametrize("foreign", [False, True])
def test_reader_corrupt_or_foreign_lease_is_closed(tmp_path, foreign):
    store, request, _producer, _command, outcome = _committed(tmp_path)
    store.lease_foreign, store.lease_corrupt = foreign, not foreign
    with pytest.raises(PhysicalMediaReadError):
        read_committed_physical_media_evidence(store, request, outcome, limits=_limits())
    assert store.closed_materializations == 2


@pytest.mark.parametrize("value", [0, -1, True, 1.0, 2**53])
def test_limits_are_exact_positive_and_no_hidden_reader_default(tmp_path, value):
    _store, request, _producer = physical_case(tmp_path)
    with pytest.raises(ValueError):
        replace(request, max_evidence_bytes=value)
    with pytest.raises(ValueError):
        replace(_limits(), max_metadata_bytes=value)
    with pytest.raises(TypeError):
        PhysicalMediaReadLimits(100, 100)


@pytest.mark.parametrize("raw", ['{"x":1,"x":1}', '{"x":1.0}', '{"x":true}\n', '{"x":NaN}'])
def test_producer_json_is_strict_canonical(tmp_path, raw):
    _store, _request_value, producer = physical_case(tmp_path)
    with pytest.raises(ValueError):
        replace(producer.produced, producer_policy_json=raw)


def test_parent_revision_never_reused_for_new_logical_chain(tmp_path):
    store, request, producer = physical_case(tmp_path)
    request = replace(request, parent=replace(request.parent, artifact_revision=8))
    resolved = resolve_physical_media_request(store, request)
    producer.produced = replace(producer.produced, physical_root=replace(producer.produced.physical_root,
        physical_root_id=resolved.physical_root_id, root_input_manifest_sha256=resolved.root_input_manifest_sha256))
    result = PreparePhysicalMediaEvidenceCommand(store, producer).execute(request)
    assert result.outcome.state == "succeeded"
    assert all(item.revision == 1 for item in store.successes[0].artifacts)
