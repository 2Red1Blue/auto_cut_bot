"""Deterministic committed command for the exact V4 candidate aggregate."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import cast
from uuid import UUID

import pytest
from autocut_kernel.contracts.compiler.canonical import (
    canonical_json_bytes,
    canonical_json_hash,
)
from autocut_kernel.media import V23CandidateWindowCompilePolicy
from autocut_kernel.pipeline.compile_v23_candidate_decision_set_command import (
    COMPILE_V23_CANDIDATE_DECISION_SET_COMMAND,
    CompileV23CandidateDecisionSetCommand,
    CompileV23CandidateDecisionSetError,
    CompileV23CandidateDecisionSetRequest,
    PersistedV23CandidateDecisionSet,
    read_committed_v23_candidate_decision_set,
)
from autocut_kernel.store import (
    ArtifactMember,
    BlobRef,
    CommandClaim,
    CommandOutcome,
    CommandRejection,
    CommandSuccess,
    CommittedArtifactMemberReference,
    CommittedSemanticInputs,
    CommittedSemanticInputsRequest,
    CommittedVlmSemanticInput,
    PersistedVlmGenerationChild,
    PersistedVlmSemanticPackV4,
    SourceWindowIdentity,
    VlmRequestRecordReference,
    VlmSemanticPackReference,
)
from autocut_kernel.store.models import (
    VLM_BATCH_FINALIZER_STRATEGY_VERSION_V4,
    PersistedCommittedArtifactMember,
    PersistedCommittedArtifactSet,
    artifact_set_hash,
    canonical_payload_hash,
)
from autocut_kernel.vlm import VlmRequestIdentity
from autocut_kernel.vlm.semantic_parser_v4 import parse_vlm_response_v4

from tests.media.test_v23_candidate_evidence_window import _policy
from tests.semantic_chain.test_material_support import _long_material_inputs
from tests.semantic_chain.test_stage1_draft import _synthetic_inputs
from tests.vlm.test_semantic_pack_v4 import _raw, _v4_context, _wire


class _Store:
    def __init__(self, inputs: CommittedSemanticInputs) -> None:
        self.inputs = inputs
        self.claims: list[CommandClaim] = []
        self.successes: list[CommandSuccess] = []
        self.rejections: list[CommandRejection] = []
        self.semantic_reads = 0
        self.artifact_reads = 0
        self.next_claim: CommandOutcome | None = None
        self.record: PersistedCommittedArtifactSet | None = None
        self.job_id = UUID(int=9101)
        self.slot_id = UUID(int=9102)
        self.receipt_id = UUID(int=9103)
        self.set_id = UUID(int=9104)

    def read_committed_semantic_inputs(
        self, request: CommittedSemanticInputsRequest
    ) -> CommittedSemanticInputs:
        self.semantic_reads += 1
        return self.inputs

    def claim_command(self, claim: CommandClaim) -> CommandOutcome:
        self.claims.append(claim)
        if self.next_claim is not None:
            return self.next_claim
        return CommandOutcome(self.slot_id, "running", is_fresh_claim=True, job_id=self.job_id)

    def commit_command_success(self, success: CommandSuccess) -> CommandOutcome:
        self.successes.append(success)
        members = tuple(
            PersistedCommittedArtifactMember(
                CommittedArtifactMemberReference(
                    self.receipt_id,
                    self.set_id,
                    ordinal,
                    artifact.scope,
                    artifact.artifact_type,
                    artifact.logical_id,
                    artifact.revision,
                    artifact.content_hash,
                ),
                artifact.payload_json,
                success.command_slot_id,
            )
            for ordinal, artifact in enumerate(success.artifacts)
        )
        self.record = PersistedCommittedArtifactSet(
            self.inputs.source_manifest.source_job,
            self.job_id,
            success.command_slot_id,
            self.receipt_id,
            self.set_id,
            self.claims[-1].request_hash,
            self.claims[-1].command_name,
            self.claims[-1].execution_kind,
            success.set_hash,
            members,
        )
        return CommandOutcome(
            success.command_slot_id,
            "succeeded",
            receipt_id=self.receipt_id,
            artifact_set_id=self.set_id,
            job_id=self.job_id,
        )

    def commit_command_rejection(self, rejection: CommandRejection) -> CommandOutcome:
        self.rejections.append(rejection)
        return CommandOutcome(
            rejection.command_slot_id,
            rejection.outcome,
            receipt_id=self.receipt_id,
            failure_code=rejection.failure_code,
            failure_detail_json=rejection.failure_detail_json,
            job_id=self.job_id,
        )

    def read_committed_artifact_set(
        self,
        job,
        *,
        command_slot_id,
        receipt_id,
        artifact_set_id,
        expected_request_hash,
        expected_command_name,
        expected_execution_kind,
    ) -> PersistedCommittedArtifactSet:
        self.artifact_reads += 1
        assert self.record is not None
        return self.record


def _blob_mapping(reference: BlobRef) -> dict[str, object]:
    return {
        "byte_length": reference.byte_length,
        "content_hash": reference.content_hash,
        "media_type": reference.media_type,
        "object_id": str(reference.object_id),
    }


def _real_v4_inputs() -> CommittedSemanticInputs:
    """Reparse V4 over a strict, fully decodable SourceManifest fixture."""

    from autocut_kernel.source_manifest import decode_source_manifest

    base = _long_material_inputs()
    source = base.source_manifest
    episode = decode_source_manifest(source.payload_json, source.proxy_blobs).episodes[0]
    manifest, manifest_set = episode.manifest, episode.manifest_set
    _unused_manifest, _unused_set, parse_policy, template = _v4_context()
    identity = VlmRequestIdentity.from_manifest(
        manifest,
        manifest_set,
        prompt_template_sha256=template.prompt_template_sha256,
        prompt_version=template.prompt_version,
        response_schema_sha256=template.response_schema_sha256,
        model_id=template.model_id,
        provider_id=template.provider_id,
        request_parameters_sha256=template.request_parameters_sha256,
        request_payload_sha256=template.request_payload_sha256,
        parse_policy=parse_policy,
    )
    raw = _raw(_wire())
    pack = parse_vlm_response_v4(
        raw,
        manifest=manifest,
        manifest_set=manifest_set,
        request_identity=identity,
        policy=parse_policy,
    )
    old = base.inputs[0]
    old_child = old.semantic_pack.source_child
    request_blob = replace(old_child.request_payload, content_hash=identity.request_payload_sha256)
    request_value = {
        "attempt_id": str(old_child.attempt_id),
        "episode_index": 0,
        "idempotency_key": old_child.idempotency_key,
        "provider_idempotency_key": old_child.provider_idempotency_key,
        "proxy_blob": _blob_mapping(source.proxy_blobs[0]),
        "request_hash": old_child.request_hash,
        "request_identity": identity.to_mapping(),
        "request_identity_sha256": identity.canonical_hash,
        "request_payload_blob": _blob_mapping(request_blob),
        "source_manifest_sha256": source.reference.content_hash,
        "source_provenance_sha256": source.canonical_hash,
        "window_manifest_set_sha256": manifest_set.canonical_hash,
        "window_manifest_sha256": manifest.canonical_hash,
    }
    request_json = canonical_json_bytes(request_value).decode("utf-8")
    child = PersistedVlmGenerationChild(
        VlmRequestRecordReference(
            source.reference.scope,
            f"vlm_request_{manifest.canonical_hash[7:31]}",
            1,
            canonical_payload_hash(request_json),
        ),
        request_json,
        source.source_job,
        source.job_id,
        old_child.command_slot_id,
        old_child.idempotency_key,
        old_child.request_hash,
        old_child.attempt_id,
        old_child.provider_idempotency_key,
        request_blob,
        old_child.receipt_id,
        old_child.artifact_set_id,
        0,
        manifest.canonical_hash,
        manifest_set.canonical_hash,
        source.reference.content_hash,
        source.canonical_hash,
        identity.canonical_hash,
        "strict-semantic-pack-v4",
        4,
    )
    pack_json = canonical_json_bytes(pack.to_mapping()).decode("utf-8")
    persisted = PersistedVlmSemanticPackV4(
        VlmSemanticPackReference(
            source.reference.scope,
            f"semantic_pack_{manifest.canonical_hash[7:39]}",
            1,
            canonical_payload_hash(pack_json),
        ),
        pack_json,
        pack,
        child,
    )
    window = SourceWindowIdentity(
        0,
        manifest.stream_index,
        manifest.core_range.start_pts,
        manifest.core_range.end_pts,
        manifest.canonical_hash,
        manifest.source_id,
        manifest.source_sha256,
        manifest.source_clock_id,
        manifest_set.canonical_hash,
        source.proxy_blobs[0],
    )
    response = replace(
        old.response_record,
        logical_id=f"vlm_response_{manifest.canonical_hash[7:31]}",
    )
    raw_blob = replace(
        old.raw_response,
        content_hash=pack.raw_response_sha256,
        byte_length=len(raw),
    )
    return CommittedSemanticInputs(
        source,
        base.source_grant,
        base.vlm_semantic_pack_set,
        child.request_policy,
        (CommittedVlmSemanticInput(window, identity, persisted, response, raw_blob),),
        VLM_BATCH_FINALIZER_STRATEGY_VERSION_V4,
    )


def _semantic_request(inputs: CommittedSemanticInputs) -> CommittedSemanticInputsRequest:
    source = inputs.source_manifest
    source_ref = CommittedArtifactMemberReference(
        source.receipt_id,
        source.artifact_set_id,
        0,
        source.reference.scope,
        source.reference.artifact_type,
        source.reference.logical_id,
        source.reference.revision,
        source.reference.content_hash,
    )
    assert source.source_job is not None
    return CommittedSemanticInputsRequest(
        source.source_job, source_ref, inputs.vlm_semantic_pack_set
    )


def _request(
    inputs: CommittedSemanticInputs | None = None,
    *,
    max_payload_bytes: int = 1_000_000,
    policy: V23CandidateWindowCompilePolicy | None = None,
) -> CompileV23CandidateDecisionSetRequest:
    inputs = _real_v4_inputs() if inputs is None else inputs
    item = inputs.inputs[0]
    persisted = item.semantic_pack
    source = inputs.source_manifest
    assert source.source_job is not None
    return CompileV23CandidateDecisionSetRequest(
        job=source.source_job,
        idempotency_key="v23-candidates:episode-0",
        artifact_scope=source.reference.scope,
        artifact_revision=3,
        semantic_inputs_request=_semantic_request(inputs),
        episode_index=0,
        window_manifest_sha256=item.source_window.window_manifest_sha256,
        semantic_pack_sha256=persisted.semantic_pack.canonical_hash,
        vlm_request_identity_sha256=item.request_identity.canonical_hash,
        compile_policy=(
            replace(
                _policy(max_duration=50),
                time_base=persisted.semantic_pack.events[0].support.manifest.source_time_base,
            )
            if policy is None
            else policy
        ),
        max_payload_bytes=max_payload_bytes,
    )


def _execute() -> tuple[
    _Store, CompileV23CandidateDecisionSetRequest, PersistedV23CandidateDecisionSet
]:
    inputs = _real_v4_inputs()
    store = _Store(inputs)
    request = _request(inputs)
    result = CompileV23CandidateDecisionSetCommand(store).execute(request)
    assert result.outcome.state == "succeeded"
    assert result.committed is not None
    return store, request, result.committed


def _replace_record_payload(
    store: _Store, payload_json: str, *, content_hash: str | None = None
) -> None:
    assert store.record is not None
    old = store.record.members[0]
    digest = canonical_payload_hash(payload_json) if content_hash is None else content_hash
    ref = replace(old.reference, content_hash=digest)
    member = PersistedCommittedArtifactMember(ref, payload_json, old.command_slot_id)
    artifact = ArtifactMember(
        ref.artifact_type,
        ref.logical_id,
        ref.revision,
        ref.scope,
        ref.content_hash,
        payload_json,
    )
    store.record = replace(store.record, members=(member,), set_hash=artifact_set_hash((artifact,)))


def test_fresh_success_binds_full_request_and_rereads_exact_value() -> None:
    store, request, committed = _execute()

    assert COMPILE_V23_CANDIDATE_DECISION_SET_COMMAND == ("CompileV23CandidateDecisionSet@1")
    assert len(store.claims) == len(store.successes) == 1
    claim = store.claims[0]
    assert claim.command_name == COMPILE_V23_CANDIDATE_DECISION_SET_COMMAND
    assert claim.execution_kind == "deterministic"
    assert claim.request_hash == request.request_hash
    assert request.canonical_payload()["semantic_inputs_request"] == {
        "job": {
            "job_key": request.semantic_inputs_request.job.job_key,
            "profile": request.semantic_inputs_request.job.profile,
        },
        "source_manifest": request.semantic_inputs_request.source_manifest.to_mapping(),
        "vlm_semantic_pack_set": (
            request.semantic_inputs_request.vlm_semantic_pack_set.to_mapping()
        ),
    }
    artifact = store.successes[0].artifacts[0]
    assert artifact.artifact_type == "v23_candidate_decision_set"
    semantic_identity = canonical_json_hash(
        {
            "compile_policy_sha256": request.compile_policy.canonical_hash,
            "semantic_pack_sha256": request.semantic_pack_sha256,
            "vlm_request_identity_sha256": request.vlm_request_identity_sha256,
            "window_manifest_sha256": request.window_manifest_sha256,
        }
    )
    assert artifact.logical_id == "v23_candidate_decision_set_" + semantic_identity[7:]
    assert artifact.scope == request.artifact_scope
    assert artifact.revision == request.artifact_revision
    assert artifact.payload_json == canonical_json_bytes(committed.value.to_mapping()).decode(
        "utf-8"
    )
    assert committed.record.set_hash == artifact_set_hash((artifact,))
    assert store.semantic_reads == 2
    assert store.artifact_reads == 1


def test_empty_candidate_pack_commits_one_closed_empty_set() -> None:
    inputs = _real_v4_inputs()
    item = inputs.inputs[0]
    empty_pack = replace(item.semantic_pack.semantic_pack, candidate_hypotheses=())
    payload = canonical_json_bytes(empty_pack.to_mapping()).decode("utf-8")
    persisted = replace(
        item.semantic_pack,
        semantic_pack=empty_pack,
        payload_json=payload,
        reference=replace(
            item.semantic_pack.reference, content_hash=canonical_payload_hash(payload)
        ),
    )
    inputs = replace(inputs, inputs=(replace(item, semantic_pack=persisted),))
    store = _Store(inputs)

    result = CompileV23CandidateDecisionSetCommand(store).execute(_request(inputs))

    assert result.outcome.state == "succeeded"
    assert result.committed is not None
    assert result.committed.value.candidate_ids == ()
    assert result.committed.value.decisions == ()


@pytest.mark.parametrize("state", ["pending", "running", "succeeded", "denied", "failed"])
def test_nonfresh_outcomes_return_without_compile_or_commit(
    monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    inputs = _real_v4_inputs()
    store = _Store(inputs)
    request = _request(inputs)
    store.next_claim = CommandOutcome(
        store.slot_id,
        cast(object, state),  # type: ignore[arg-type]
        is_fresh_claim=False,
        receipt_id=store.receipt_id if state in ("succeeded", "denied", "failed") else None,
        artifact_set_id=store.set_id if state == "succeeded" else None,
        failure_code="existing" if state in ("denied", "failed") else None,
        failure_detail_json="{}" if state in ("denied", "failed") else None,
        job_id=store.job_id,
    )
    import autocut_kernel.pipeline.compile_v23_candidate_decision_set_command as module

    def forbidden(*_args: object) -> object:
        raise AssertionError("nonfresh command must not compile")

    monkeypatch.setattr(module, "compile_v23_candidate_decision_set", forbidden)

    result = CompileV23CandidateDecisionSetCommand(store).execute(request)

    assert result.outcome is store.next_claim
    assert result.committed is None
    assert not store.successes and not store.rejections and store.artifact_reads == 0
    assert store.semantic_reads == 1


def test_replay_does_not_compile_a_second_time(monkeypatch: pytest.MonkeyPatch) -> None:
    store, request, _committed = _execute()
    assert store.record is not None
    store.next_claim = CommandOutcome(
        store.record.command_slot_id,
        "succeeded",
        receipt_id=store.record.receipt_id,
        artifact_set_id=store.record.artifact_set_id,
        job_id=store.record.job_id,
    )
    import autocut_kernel.pipeline.compile_v23_candidate_decision_set_command as module

    monkeypatch.setattr(
        module,
        "compile_v23_candidate_decision_set",
        lambda *_args: (_ for _ in ()).throw(AssertionError("compiled twice")),
    )

    replay = CompileV23CandidateDecisionSetCommand(store).execute(request)

    assert replay.outcome is store.next_claim
    assert replay.committed is None
    assert len(store.successes) == 1


def test_public_exact_reader_reresolves_and_verifies() -> None:
    store, request, committed = _execute()

    reread = read_committed_v23_candidate_decision_set(
        store,
        request,
        CommandOutcome(
            committed.record.command_slot_id,
            "succeeded",
            receipt_id=committed.record.receipt_id,
            artifact_set_id=committed.record.artifact_set_id,
            job_id=committed.record.job_id,
        ),
    )

    assert reread == committed


def test_exact_reader_applies_byte_cap_to_canonical_not_jsonb_text() -> None:
    inputs = _real_v4_inputs()
    probe_store = _Store(inputs)
    probe = CompileV23CandidateDecisionSetCommand(probe_store).execute(_request(inputs))
    assert probe.committed is not None
    exact_size = len(canonical_json_bytes(probe.committed.value.to_mapping()))
    store = _Store(inputs)
    request = _request(inputs, max_payload_bytes=exact_size)
    result = CompileV23CandidateDecisionSetCommand(store).execute(request)
    assert result.committed is not None and store.record is not None
    committed = result.committed
    original_set_hash = store.record.set_hash
    old_member = store.record.members[0]
    payload = json.dumps(json.loads(old_member.payload_json), ensure_ascii=False)
    assert len(payload.encode("utf-8")) > request.max_payload_bytes
    member = PersistedCommittedArtifactMember(
        old_member.reference,
        payload,
        old_member.command_slot_id,
    )
    store.record = replace(store.record, members=(member,))

    reread = read_committed_v23_candidate_decision_set(
        store,
        request,
        CommandOutcome(
            committed.record.command_slot_id,
            "succeeded",
            receipt_id=committed.record.receipt_id,
            artifact_set_id=committed.record.artifact_set_id,
            job_id=committed.record.job_id,
        ),
    )

    assert reread.value == committed.value
    assert reread.record.set_hash == original_set_hash


def test_complete_aggregate_census_is_enforced_before_claim() -> None:
    inputs = _real_v4_inputs()
    store = _Store(inputs)
    request = _request(inputs)
    object.__setattr__(inputs, "inputs", ())

    with pytest.raises(CompileV23CandidateDecisionSetError, match="every Source episode"):
        CompileV23CandidateDecisionSetCommand(store).execute(request)
    assert not store.claims


def test_payload_byte_cap_is_a_deterministic_denial() -> None:
    inputs = _real_v4_inputs()
    store = _Store(inputs)

    result = CompileV23CandidateDecisionSetCommand(store).execute(
        _request(inputs, max_payload_bytes=1)
    )

    assert result.outcome.state == "denied"
    assert result.outcome.failure_code == "V23_CANDIDATE_DECISION_SET_INVALID"
    assert len(store.rejections) == 1 and not store.successes


def test_v3_aggregate_or_pack_is_rejected_before_claim() -> None:
    inputs = _synthetic_inputs()
    store = _Store(inputs)
    item = inputs.inputs[0]
    source = inputs.source_manifest
    assert source.source_job is not None
    request = CompileV23CandidateDecisionSetRequest(
        source.source_job,
        "v23-v3-invalid",
        source.reference.scope,
        1,
        _semantic_request(inputs),
        item.source_window.episode_index,
        item.source_window.window_manifest_sha256,
        item.semantic_pack.semantic_pack.canonical_hash,
        item.request_identity.canonical_hash,
        _policy(max_duration=50),
        1_000_000,
    )

    with pytest.raises(CompileV23CandidateDecisionSetError, match="V4 aggregate"):
        CompileV23CandidateDecisionSetCommand(store).execute(request)
    assert not store.claims

    # A forged V4 aggregate label cannot retype an actual persisted V3 child.
    object.__setattr__(
        inputs,
        "vlm_batch_strategy_version",
        VLM_BATCH_FINALIZER_STRATEGY_VERSION_V4,
    )
    forged_store = _Store(inputs)
    with pytest.raises(
        CompileV23CandidateDecisionSetError,
        match="PersistedVlmSemanticPackV4",
    ):
        CompileV23CandidateDecisionSetCommand(forged_store).execute(request)
    assert not forged_store.claims


@pytest.mark.parametrize(
    "field",
    [
        "window_manifest_sha256",
        "semantic_pack_sha256",
        "vlm_request_identity_sha256",
    ],
)
def test_selector_mismatch_is_rejected_before_claim(field: str) -> None:
    inputs = _real_v4_inputs()
    store = _Store(inputs)
    request = replace(_request(inputs), **{field: "sha256:" + "f" * 64})

    with pytest.raises(CompileV23CandidateDecisionSetError, match="selector"):
        CompileV23CandidateDecisionSetCommand(store).execute(request)
    assert not store.claims


def test_exact_reader_rejects_malformed_rehashed_payload() -> None:
    store, request, committed = _execute()
    _replace_record_payload(store, '{"bad":1}')

    with pytest.raises(Exception, match="strict DecisionSet"):
        read_committed_v23_candidate_decision_set(
            store,
            request,
            CommandOutcome(
                committed.record.command_slot_id,
                "succeeded",
                receipt_id=committed.record.receipt_id,
                artifact_set_id=committed.record.artifact_set_id,
                job_id=committed.record.job_id,
            ),
        )


def test_exact_reader_rejects_tampered_payload_without_trusting_record_decoder() -> None:
    store, request, committed = _execute()
    assert store.record is not None
    object.__setattr__(store.record.members[0], "payload_json", '{"bad":1}')

    with pytest.raises(CompileV23CandidateDecisionSetError, match="hash"):
        read_committed_v23_candidate_decision_set(
            store,
            request,
            CommandOutcome(
                committed.record.command_slot_id,
                "succeeded",
                receipt_id=committed.record.receipt_id,
                artifact_set_id=committed.record.artifact_set_id,
                job_id=committed.record.job_id,
            ),
        )


def test_exact_reader_rejects_rehashed_valid_but_foreign_value() -> None:
    store, request, committed = _execute()
    mapping = committed.value.to_mapping()
    mapping["source_id"] = "foreign-source"
    decisions = cast(list[dict[str, object]], mapping["decisions"])
    for decision in decisions:
        decision["source_id"] = "foreign-source"
    _replace_record_payload(store, canonical_json_bytes(mapping).decode("utf-8"))

    with pytest.raises(Exception, match="strict DecisionSet|independent recomputation"):
        read_committed_v23_candidate_decision_set(
            store,
            request,
            CommandOutcome(
                committed.record.command_slot_id,
                "succeeded",
                receipt_id=committed.record.receipt_id,
                artifact_set_id=committed.record.artifact_set_id,
                job_id=committed.record.job_id,
            ),
        )


def test_exact_reader_rejects_exponent_number_before_jsonb_bound_assumption() -> None:
    store, request, committed = _execute()
    mapping = committed.value.to_mapping()
    mapping["stream_index"] = 1e-12
    _replace_record_payload(
        store,
        json.dumps(mapping, ensure_ascii=False, separators=(",", ":")),
    )

    with pytest.raises(CompileV23CandidateDecisionSetError, match="strict DecisionSet"):
        read_committed_v23_candidate_decision_set(
            store,
            request,
            CommandOutcome(
                committed.record.command_slot_id,
                "succeeded",
                receipt_id=committed.record.receipt_id,
                artifact_set_id=committed.record.artifact_set_id,
                job_id=committed.record.job_id,
            ),
        )


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        (lambda record: replace(record, job_id=UUID(int=9991)), "identity"),
        (lambda record: replace(record, request_hash="sha256:" + "1" * 64), "identity"),
        (lambda record: replace(record, command_name="ForeignCommand@1"), "identity"),
        (lambda record: replace(record, execution_kind="generation"), "identity"),
    ],
)
def test_exact_reader_rejects_foreign_store_record(mutation, match: str) -> None:
    store, request, committed = _execute()
    assert store.record is not None
    store.record = mutation(store.record)

    with pytest.raises(CompileV23CandidateDecisionSetError, match=match):
        read_committed_v23_candidate_decision_set(
            store,
            request,
            CommandOutcome(
                committed.record.command_slot_id,
                "succeeded",
                receipt_id=committed.record.receipt_id,
                artifact_set_id=committed.record.artifact_set_id,
                job_id=committed.record.job_id,
            ),
        )


def test_exact_reader_rejects_foreign_outcome_ids() -> None:
    store, request, committed = _execute()

    with pytest.raises(CompileV23CandidateDecisionSetError, match="identity"):
        read_committed_v23_candidate_decision_set(
            store,
            request,
            CommandOutcome(
                UUID(int=9992),
                "succeeded",
                receipt_id=committed.record.receipt_id,
                artifact_set_id=committed.record.artifact_set_id,
                job_id=committed.record.job_id,
            ),
        )


def test_ambiguous_success_commit_is_not_converted_to_rejection() -> None:
    class AmbiguousStore(_Store):
        def commit_command_success(self, success: CommandSuccess) -> CommandOutcome:
            self.successes.append(success)
            raise TimeoutError("commit result unknown")

    inputs = _real_v4_inputs()
    store = AmbiguousStore(inputs)

    with pytest.raises(TimeoutError, match="unknown"):
        CompileV23CandidateDecisionSetCommand(store).execute(_request(inputs))
    assert not store.rejections


def test_unexpected_value_error_is_infrastructure_failure_not_permanent_denial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _real_v4_inputs()
    store = _Store(inputs)
    import autocut_kernel.pipeline.compile_v23_candidate_decision_set_command as module

    def broken_compiler(*_args: object) -> object:
        raise ValueError("implementation defect")

    monkeypatch.setattr(module, "compile_v23_candidate_decision_set", broken_compiler)
    result = CompileV23CandidateDecisionSetCommand(store).execute(_request(inputs))

    assert result.outcome.state == "failed"
    assert result.outcome.failure_code == ("V23_CANDIDATE_DECISION_SET_INFRASTRUCTURE_FAILED")
    assert len(store.rejections) == 1
    assert store.rejections[0].outcome == "failed"


def test_request_requires_canonical_scope_positive_revision_and_byte_cap() -> None:
    request = _request()
    with pytest.raises(CompileV23CandidateDecisionSetError):
        replace(request, artifact_revision=0)
    with pytest.raises(CompileV23CandidateDecisionSetError):
        replace(request, max_payload_bytes=True)
    with pytest.raises(CompileV23CandidateDecisionSetError):
        replace(request, artifact_scope=replace(request.artifact_scope, key="foreign"))
