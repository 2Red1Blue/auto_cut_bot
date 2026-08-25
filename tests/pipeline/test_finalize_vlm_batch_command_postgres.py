from __future__ import annotations

import json
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from autocut_kernel.pipeline import (
    FinalizeVlmBatchCommand,
    FinalizeVlmBatchRequest,
    VlmBatchChildOutcome,
)
from autocut_kernel.store import (
    VLM_BATCH_IDEMPOTENCY_PREFIX,
    BlobRef,
    CommandClaim,
    CommandOutcome,
    CommandSuccess,
    CommittedArtifactMemberReference,
    CommittedVlmInputReference,
    Job,
    PersistedVlmGenerationChild,
    VlmRequestRecordReference,
)
from autocut_kernel.store.models import canonical_payload_hash, canonical_recipe_scope


def _hash(digit: str) -> str:
    return "sha256:" + digit * 64


class Store:
    def __init__(self) -> None:
        self.claims: dict[str, tuple[CommandClaim, CommandOutcome]] = {}
        self.successes: list[CommandSuccess] = []
        self.children: dict[str, PersistedVlmGenerationChild] = {}
        self.references: dict[str, CommittedVlmInputReference] = {}
        self.unreadable: set[str] = set()

    def claim_command(self, claim: CommandClaim) -> CommandOutcome:
        existing = self.claims.get(claim.idempotency_key)
        if existing is not None:
            if existing[0] != claim:
                raise ValueError("idempotency identity conflict")
            return existing[1]
        outcome = CommandOutcome(uuid4(), "running", is_fresh_claim=True)
        self.claims[claim.idempotency_key] = (claim, outcome)
        return outcome

    def claim_vlm_batch_command(self, claim: CommandClaim) -> CommandOutcome:
        return self.claim_command(claim)

    def read_committed_vlm_generation_child(
        self,
        job: Job,
        idempotency_key: str,
    ) -> PersistedVlmGenerationChild:
        if idempotency_key in self.unreadable or idempotency_key not in self.children:
            raise ValueError("committed VLM child is unavailable")
        result = self.children[idempotency_key]
        assert result.source_job == job
        return result

    def read_committed_vlm_input_reference(
        self,
        job: Job,
        idempotency_key: str,
    ) -> CommittedVlmInputReference:
        assert self.children[idempotency_key].source_job == job
        return self.references[idempotency_key]

    def seed_child(
        self,
        job: Job,
        child: VlmBatchChildOutcome,
        *,
        unreadable: bool = False,
    ) -> None:
        persisted = _persisted(job, child)
        self.children[child.idempotency_key] = persisted
        common = {
            "receipt_id": persisted.receipt_id,
            "artifact_set_id": persisted.artifact_set_id,
            "scope": canonical_recipe_scope(job),
            "revision": 1,
        }
        self.references[child.idempotency_key] = CommittedVlmInputReference(
            request_record=CommittedArtifactMemberReference(
                member_ordinal=0,
                artifact_type="vlm_request_record",
                logical_id=persisted.reference.logical_id,
                content_hash=persisted.reference.content_hash,
                **common,
            ),
            response_record=CommittedArtifactMemberReference(
                member_ordinal=1,
                artifact_type="vlm_response_record",
                logical_id=f"vlm_response_{child.window_manifest_sha256[7:31]}",
                content_hash=_hash("a"),
                **common,
            ),
            semantic_pack=CommittedArtifactMemberReference(
                member_ordinal=2,
                artifact_type="vlm_semantic_pack",
                logical_id=f"semantic_pack_{child.window_manifest_sha256[7:39]}",
                content_hash=_hash("b"),
                **common,
            ),
            proxy_blob=BlobRef(uuid4(), _hash("c"), 1, "video/mp4"),
            request_payload=persisted.request_payload,
            raw_response=BlobRef(uuid4(), _hash("d"), 1, "application/json"),
        )
        if unreadable:
            self.unreadable.add(child.idempotency_key)

    def commit_command_success(self, success: CommandSuccess) -> CommandOutcome:
        self.successes.append(success)
        outcome = CommandOutcome(
            success.command_slot_id,
            "succeeded",
            receipt_id=uuid4(),
            artifact_set_id=uuid4(),
        )
        self._replace(success.command_slot_id, outcome)
        return outcome

    def commit_vlm_batch_success(self, success: CommandSuccess) -> CommandOutcome:
        return self.commit_command_success(success)

    def _replace(self, slot_id: UUID, outcome: CommandOutcome) -> None:
        for key, (claim, current) in self.claims.items():
            if current.command_slot_id == slot_id:
                self.claims[key] = (claim, outcome)
                return
        raise AssertionError("unknown command slot")


def _child(index: int, state: str = "succeeded") -> VlmBatchChildOutcome:
    digit = f"{index + 1:x}"
    return VlmBatchChildOutcome(
        episode_index=index,
        idempotency_key=f"vlm-child-{index}",
        window_manifest_sha256=_hash(digit),
        source_manifest_sha256=_hash("2"),
        source_provenance_sha256=_hash("3"),
        request_hash=_hash(f"{index + 4:x}"),
        state=state,  # type: ignore[arg-type]
        receipt_id=uuid4(),
        artifact_set_id=uuid4() if state == "succeeded" else None,
    )


def _persisted(job: Job, child: VlmBatchChildOutcome) -> PersistedVlmGenerationChild:
    assert child.artifact_set_id is not None
    request_payload = BlobRef(uuid4(), _hash("8"), 10, "application/json")
    attempt_id = uuid4()
    identity = {
        "frame_pts_index_set_sha256": _hash("a"),
        "frame_samples_sha256": _hash("b"),
        "model_id": "doubao-model",
        "parse_policy_sha256": _hash("c"),
        "preprocess_policy_sha256": _hash("d"),
        "prompt_template_sha256": _hash("e"),
        "prompt_version": "prompt-v1",
        "provider_id": "doubao-provider",
        "proxy_blob_ref_sha256": _hash("f"),
        "request_parameters_sha256": _hash("1"),
        "request_payload_sha256": request_payload.content_hash,
        "response_schema_sha256": _hash("2"),
        "source_clock_id": "clock-0",
        "source_id": f"source-{child.episode_index}",
        "source_sha256": _hash("3"),
        "window_manifest_set_sha256": _hash("6"),
        "window_manifest_sha256": child.window_manifest_sha256,
        "window_sampling_policy_sha256": _hash("7"),
    }
    identity_hash = canonical_payload_hash(json.dumps(identity))
    payload = {
        "attempt_id": str(attempt_id),
        "episode_index": child.episode_index,
        "idempotency_key": child.idempotency_key,
        "provider_idempotency_key": f"provider-{child.episode_index}",
        "proxy_blob": {
            "byte_length": 20,
            "content_hash": _hash("9"),
            "media_type": "video/mp4",
            "object_id": str(uuid4()),
        },
        "request_hash": child.request_hash,
        "request_identity": identity,
        "request_identity_sha256": identity_hash,
        "request_payload_blob": {
            "byte_length": request_payload.byte_length,
            "content_hash": request_payload.content_hash,
            "media_type": request_payload.media_type,
            "object_id": str(request_payload.object_id),
        },
        "source_manifest_sha256": child.source_manifest_sha256,
        "source_provenance_sha256": child.source_provenance_sha256,
        "window_manifest_set_sha256": _hash("6"),
        "window_manifest_sha256": child.window_manifest_sha256,
    }
    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return PersistedVlmGenerationChild(
        VlmRequestRecordReference(
            canonical_recipe_scope(job),
            f"vlm_request_{child.window_manifest_sha256[7:31]}",
            1,
            canonical_payload_hash(payload_json),
        ),
        payload_json,
        job,
        uuid4(),
        uuid4(),
        child.idempotency_key,
        child.request_hash,
        attempt_id,
        f"provider-{child.episode_index}",
        request_payload,
        child.receipt_id,
        child.artifact_set_id,
        child.episode_index,
        child.window_manifest_sha256,
        _hash("6"),
        child.source_manifest_sha256,
        child.source_provenance_sha256,
        identity_hash,
    )


def _request(*children: VlmBatchChildOutcome) -> FinalizeVlmBatchRequest:
    job = Job("pipeline_run_" + "a" * 32, "test")
    return FinalizeVlmBatchRequest(
        job,
        VLM_BATCH_IDEMPOTENCY_PREFIX + "finalize",
        canonical_recipe_scope(job),
        1,
        max(len(children), 1),
        _hash("2"),
        _hash("3"),
        tuple(children),
    )


def _seed_request(store: Store, request: FinalizeVlmBatchRequest) -> None:
    for child in request.children:
        store.seed_child(request.job, child)


def test_success_commits_nonempty_aggregate_artifact_and_replays_receipt() -> None:
    store = Store()
    command = FinalizeVlmBatchCommand(store)
    request = _request(_child(0), _child(1))
    _seed_request(store, request)

    first = command.execute(request)
    replay = command.execute(request)

    assert first.outcome.state == "succeeded"
    assert first.outcome.receipt_id == replay.outcome.receipt_id
    assert first.artifact is not None
    assert first.artifact.artifact_type == "vlm_semantic_pack_set"
    payload = json.loads(first.artifact.payload_json)
    assert payload["children"][0]["request_record"]["member_ordinal"] == 0
    assert payload["children"][0]["semantic_pack"]["artifact_type"] == "vlm_semantic_pack"
    assert payload["request_policy"]["model_id"] == "doubao-model"
    assert len(store.successes) == 1


def test_finalizer_refuses_rejected_child_without_claiming_aggregate() -> None:
    store = Store()
    with pytest.raises(ValueError, match="only independently provable succeeded"):
        _request(_child(0, "denied"))
    assert store.claims == {}


def test_nonterminal_or_noncontiguous_children_cannot_claim_aggregate_command() -> None:
    store = Store()
    with pytest.raises(ValueError, match="terminal"):
        _child(0, "running")
    with pytest.raises(ValueError, match="ordered episode indexes"):
        FinalizeVlmBatchCommand(store).execute(_request(_child(1)))
    assert store.claims == {}


@pytest.mark.parametrize(
    "tamper",
    ["receipt", "request_hash", "missing", "command_name"],
)
def test_fabricated_or_mismatched_child_cannot_create_aggregate(tamper: str) -> None:
    store = Store()
    persisted_child = _child(0)
    request_child = persisted_child
    if tamper == "receipt":
        request_child = replace(persisted_child, receipt_id=uuid4())
    elif tamper == "request_hash":
        request_child = replace(persisted_child, request_hash=_hash("9"))
    request = _request(request_child)
    if tamper != "missing":
        store.seed_child(
            request.job,
            persisted_child,
            unreadable=tamper == "command_name",
        )

    with pytest.raises(ValueError, match="persisted Kernel outcome|unavailable"):
        FinalizeVlmBatchCommand(store).execute(request)

    assert all(
        claim.command_name != "FinalizeVlmBatchCommand"
        for claim, _outcome in store.claims.values()
    )


def test_duplicate_persisted_child_cannot_be_relabelled_as_another_episode() -> None:
    store = Store()
    first = _child(0)
    relabelled = replace(
        first,
        episode_index=1,
        idempotency_key="vlm-child-relabelled",
    )
    request = _request(first, relabelled)
    store.seed_child(request.job, first)
    store.children[relabelled.idempotency_key] = store.children[first.idempotency_key]

    with pytest.raises(ValueError, match="exact persisted Kernel outcome|duplicate"):
        FinalizeVlmBatchCommand(store).execute(request)
    assert store.claims == {}


def test_mixed_frozen_request_policies_cannot_be_batched() -> None:
    store = Store()
    request = _request(_child(0), _child(1))
    _seed_request(store, request)
    second = store.children["vlm-child-1"]
    payload = json.loads(second.payload_json)
    payload["request_identity"]["model_id"] = "another-model"
    identity_json = json.dumps(
        payload["request_identity"], separators=(",", ":"), sort_keys=True
    )
    payload["request_identity_sha256"] = canonical_payload_hash(identity_json)
    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    store.children["vlm-child-1"] = replace(
        second,
        payload_json=payload_json,
        request_identity_sha256=payload["request_identity_sha256"],
        reference=replace(
            second.reference,
            content_hash=canonical_payload_hash(payload_json),
        ),
    )
    store.references["vlm-child-1"] = replace(
        store.references["vlm-child-1"],
        request_record=replace(
            store.references["vlm-child-1"].request_record,
            content_hash=canonical_payload_hash(payload_json),
        ),
    )

    with pytest.raises(ValueError, match="frozen request policy"):
        FinalizeVlmBatchCommand(store).execute(request)
