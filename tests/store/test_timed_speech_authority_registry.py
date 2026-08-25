from __future__ import annotations

from uuid import uuid4

import pytest
from autocut_kernel.media import (
    TimedSpeechCapability,
    TimedSpeechGuardPolicy,
    TimedSpeechProducerRequirement,
    TimedSpeechProfileKind,
    TimedSpeechProfileRegistryEntry,
)
from autocut_kernel.media.types import TimeBase
from autocut_kernel.registry import (
    AUTHORITY_BOOTSTRAP_CAPABILITY,
    AUTHORITY_BOOTSTRAP_PRINCIPAL,
    AuthorityBootstrapIdentity,
    AuthorityRegistrySnapshot,
    BootstrappedTimedSpeechProfile,
    BootstrapTimedSpeechProfileRegistryCommand,
    BootstrapTimedSpeechProfileRegistryRequest,
    TimedSpeechProfileKey,
    VerifiedTimedSpeechAuthorityContext,
)
from autocut_kernel.registry.timed_speech import (
    AUTHORITY_BOOTSTRAP_JOB,
    BOOTSTRAP_TIMED_SPEECH_PROFILE_REGISTRY_COMMAND,
    TimedSpeechRegistryError,
)
from autocut_kernel.store import (
    CommandClaim,
    CommandOutcome,
    CommandRejection,
    CommandSuccess,
    CommittedArtifactMemberReference,
)

HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64
HASH_D = "sha256:" + "d" * 64


def _entry(*, same_model: bool = False, same_calibration: bool = False) -> TimedSpeechProfileRegistryEntry:
    clock = TimeBase(1, 48_000)
    asr = TimedSpeechProducerRequirement(
        producer_id="funasr-asr",
        producer_kind="asr",
        inference_kind="sensevoice-word-timestamp",
        generation_policy_sha256=HASH_A,
        model_sha256=HASH_B,
        adapter_sha256=HASH_C,
        calibration_record_sha256=HASH_D,
        clock_id="audio-48k",
        time_base=clock,
    )
    vad = TimedSpeechProducerRequirement(
        producer_id="funasr-vad",
        producer_kind="vad",
        inference_kind="fsmn-vad-direct",
        generation_policy_sha256=HASH_B,
        model_sha256=HASH_B if same_model else HASH_C,
        adapter_sha256=HASH_D,
        calibration_record_sha256=HASH_D if same_calibration else HASH_A,
        clock_id="audio-48k",
        time_base=clock,
    )
    return TimedSpeechProfileRegistryEntry(
        profile_id="sensevoice_word_guard_v1",
        profile_version="1",
        kind=TimedSpeechProfileKind.SENSEVOICE_WORD_GUARD_V1,
        capability=TimedSpeechCapability.KNOWN_SPEECH_ONLY,
        transcript_requirement=asr,
        vad_requirement=vad,
        guard_policy=TimedSpeechGuardPolicy(HASH_B, "audio-48k", clock, 1, 1, 1, 1),
        registry_contract_sha256=HASH_C,
    )


class _BootstrapStore:
    def __init__(self) -> None:
        self.claims: list[CommandClaim] = []
        self.commits: list[tuple[CommandSuccess, AuthorityRegistrySnapshot]] = []
        self.rejections: list[CommandRejection] = []
        self.outcome = CommandOutcome(uuid4(), "running", is_fresh_claim=True)
        self.resolved: BootstrappedTimedSpeechProfile | None = None

    def claim_command(self, claim: CommandClaim) -> CommandOutcome:
        self.claims.append(claim)
        return self.outcome

    def commit_timed_speech_profile_bootstrap(
        self, success: CommandSuccess, snapshot: AuthorityRegistrySnapshot
    ) -> CommandOutcome:
        self.commits.append((success, snapshot))
        return CommandOutcome(success.command_slot_id, "succeeded", receipt_id=uuid4(), artifact_set_id=uuid4())

    def read_bootstrapped_timed_speech_profile(
        self, snapshot: AuthorityRegistrySnapshot
    ) -> BootstrappedTimedSpeechProfile:
        assert self.resolved is not None
        assert self.resolved.snapshot == snapshot
        return self.resolved

    def commit_command_rejection(self, rejection: CommandRejection) -> CommandOutcome:
        self.rejections.append(rejection)
        return CommandOutcome(rejection.command_slot_id, "denied", receipt_id=uuid4())


def _request() -> BootstrapTimedSpeechProfileRegistryRequest:
    entry = _entry()
    snapshot = AuthorityRegistrySnapshot(
        HASH_D, TimedSpeechProfileKey(entry.profile_id, entry.profile_version)
    )
    return BootstrapTimedSpeechProfileRegistryRequest(
        AuthorityBootstrapIdentity(AUTHORITY_BOOTSTRAP_PRINCIPAL, AUTHORITY_BOOTSTRAP_CAPABILITY),
        snapshot,
        entry,
    )


def test_bootstrap_uses_fixed_identity_and_profile_specific_member() -> None:
    store = _BootstrapStore()
    request = _request()

    result = BootstrapTimedSpeechProfileRegistryCommand(store).execute(request)

    assert result.state == "succeeded"
    assert store.claims[0].job == AUTHORITY_BOOTSTRAP_JOB
    assert store.claims[0].command_name == BOOTSTRAP_TIMED_SPEECH_PROFILE_REGISTRY_COMMAND
    artifact = store.commits[0][0].artifacts[0]
    assert artifact.logical_id == "timed-speech/sensevoice_word_guard_v1/1"
    assert artifact.content_hash == request.entry.canonical_hash


def test_post_claim_deterministic_validation_error_is_terminal_denial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _BootstrapStore()
    request = _request()

    def invalid_artifact(_request: BootstrapTimedSpeechProfileRegistryRequest):
        raise TimedSpeechRegistryError("invalid")

    monkeypatch.setattr(BootstrapTimedSpeechProfileRegistryRequest, "artifact", invalid_artifact)

    result = BootstrapTimedSpeechProfileRegistryCommand(store).execute(request)

    assert result.state == "denied"
    assert store.rejections[0].failure_code == "AUTHORITY_BOOTSTRAP_VALIDATION_FAILED"
    assert store.commits == []


@pytest.mark.parametrize("state", ("denied", "running"))
def test_non_successful_bootstrap_replay_does_not_require_success_anchor(state: str) -> None:
    store = _BootstrapStore()
    store.outcome = CommandOutcome(uuid4(), state, is_fresh_claim=False)

    result = BootstrapTimedSpeechProfileRegistryCommand(store).execute(_request())

    assert result is store.outcome


def test_verified_authority_context_only_bootstraps_its_exact_profile() -> None:
    context = VerifiedTimedSpeechAuthorityContext(_request().snapshot, _request().entry)

    request = context.bootstrap_request()

    assert request.snapshot == context.snapshot
    assert request.entry == context.entry
    with pytest.raises(ValueError, match="placeholder hash"):
        AuthorityRegistrySnapshot(
            "sha256:" + "0" * 64,
            TimedSpeechProfileKey("sensevoice_word_guard_v1", "1"),
        )


@pytest.mark.parametrize("kwargs", ({"same_model": True}, {"same_calibration": True}))
def test_registry_entry_rejects_shared_asr_vad_model_or_calibration(
    kwargs: dict[str, bool],
) -> None:
    with pytest.raises(ValueError, match="distinct ASR and VAD"):
        _entry(**kwargs)


def test_bootstrapped_projection_requires_the_anchor_identity() -> None:
    request = _request()
    reference = CommittedArtifactMemberReference(
        receipt_id=uuid4(),
        artifact_set_id=uuid4(),
        member_ordinal=0,
        scope=request.artifact().scope,
        artifact_type=request.artifact().artifact_type,
        logical_id=request.artifact().logical_id,
        revision=1,
        content_hash=request.entry.canonical_hash,
    )
    resolved = BootstrappedTimedSpeechProfile(request.snapshot, reference, request.entry)

    assert resolved.reference == reference
    with pytest.raises(ValueError, match="authority anchor"):
        BootstrappedTimedSpeechProfile(
            request.snapshot,
            CommittedArtifactMemberReference(
                receipt_id=reference.receipt_id,
                artifact_set_id=reference.artifact_set_id,
                member_ordinal=1,
                scope=reference.scope,
                artifact_type=reference.artifact_type,
                logical_id=reference.logical_id,
                revision=reference.revision,
                content_hash=reference.content_hash,
            ),
            request.entry,
        )
