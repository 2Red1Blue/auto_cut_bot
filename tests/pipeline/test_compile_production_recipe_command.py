from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from autocut_kernel.media.types import TimeBase
from autocut_kernel.physical_edit.candidate_exact_span import CandidateExactSpanPolicy
from autocut_kernel.physical_edit.dialogue_guard import DialogueRequirement
from autocut_kernel.physical_edit.editorial_exact_span import (
    EDITORIAL_EXACT_SPAN_STRATEGY,
    EditorialExactSpanPolicy,
)
from autocut_kernel.pipeline import compile_production_recipe_command as command_module
from autocut_kernel.pipeline.compile_production_recipe_command import (
    COMPILE_PRODUCTION_RECIPE_COMMAND,
    STAGE4_COMPILATION_BLOCKED,
    STAGE4_OUTPUT_TIMING_INDETERMINATE,
    CompileProductionRecipeCommand,
    CompileProductionRecipeError,
    CompileProductionRecipeRequest,
    ProductionRecipeCompilationLimits,
    read_committed_production_recipe_set,
    resolve_compile_production_recipe_request,
)
from autocut_kernel.store.models import (
    ArtifactMember,
    CommandOutcome,
    CommandSuccess,
    CommittedArtifactMemberReference,
    PersistedCommittedArtifactMember,
    PersistedCommittedArtifactSet,
    artifact_set_hash,
    canonical_payload_hash,
    canonical_recipe_scope,
)

from tests.authority.editorial_media_fixture import editorial_timed_media_case


class _Stage4Store:
    def __init__(self, base: object) -> None:
        self.base = base
        self.claims: list[object] = []
        self.outcomes: dict[str, CommandOutcome] = {}
        self.stage4_record: PersistedCommittedArtifactSet | None = None

    def __getattr__(self, name: str) -> object:
        return getattr(self.base, name)

    def claim_command(self, claim):  # type: ignore[no-untyped-def]
        self.claims.append(claim)
        prior = self.outcomes.get(claim.idempotency_key)
        if prior is not None:
            return replace(prior, is_fresh_claim=False)
        outcome = CommandOutcome(uuid4(), "claimed", is_fresh_claim=True)
        self.outcomes[claim.idempotency_key] = outcome
        return outcome

    def commit_production_recipe_success(self, verified):  # type: ignore[no-untyped-def]
        success = command_module._open_verified_production_recipe_commit(verified)
        claim = self.claims[-1]
        source = self.base.editorial.inputs.source_manifest
        receipt_id, artifact_set_id = uuid4(), uuid4()
        outcome = CommandOutcome(
            success.command_slot_id,
            "succeeded",
            receipt_id=receipt_id,
            artifact_set_id=artifact_set_id,
            job_id=source.job_id,
        )
        members = tuple(
            PersistedCommittedArtifactMember(
                CommittedArtifactMemberReference(
                    receipt_id,
                    artifact_set_id,
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
        self.stage4_record = PersistedCommittedArtifactSet(
            claim.job,
            source.job_id,
            success.command_slot_id,
            receipt_id,
            artifact_set_id,
            claim.request_hash,
            claim.command_name,
            claim.execution_kind,
            success.set_hash,
            members,
        )
        self.outcomes[claim.idempotency_key] = outcome
        return outcome

    def commit_command_rejection(self, rejection):  # type: ignore[no-untyped-def]
        claim = self.claims[-1]
        outcome = CommandOutcome(
            rejection.command_slot_id,
            rejection.outcome,
            failure_code=rejection.failure_code,
            failure_detail_json=rejection.failure_detail_json,
        )
        self.outcomes[claim.idempotency_key] = outcome
        return outcome

    def read_committed_artifact_set(self, job, **expected):  # type: ignore[no-untyped-def]
        if expected["expected_command_name"] == COMPILE_PRODUCTION_RECIPE_COMMAND:
            assert self.stage4_record is not None
            return self.stage4_record
        return self.base.read_committed_artifact_set(job, **expected)


def _reverse_json_object_order(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _reverse_json_object_order(child) for key, child in reversed(tuple(value.items()))
        }
    if isinstance(value, list):
        return [_reverse_json_object_order(child) for child in value]
    return value


def _replace_record_payloads_with_jsonb_display(
    record: PersistedCommittedArtifactSet,
) -> PersistedCommittedArtifactSet:
    members = tuple(
        replace(
            member,
            payload_json=json.dumps(
                _reverse_json_object_order(json.loads(member.payload_json)),
                ensure_ascii=False,
                separators=(", ", ": "),
            ),
        )
        for member in record.members
    )
    return replace(record, members=members)


def _request(
    case,
    *,
    entries: int = 32,
    member_payload: int = 8_000_000,
    total_payload: int = 8_000_000,
):  # type: ignore[no-untyped-def]
    _store, stage3_request, stage3_outcome, batch_request, batch_outcome, _resolver, _limits = case
    return CompileProductionRecipeRequest(
        stage3_request.job,
        "stage4:compile:one",
        canonical_recipe_scope(stage3_request.job),
        1,
        stage3_request,
        stage3_outcome,
        batch_request,
        batch_outcome,
        EditorialExactSpanPolicy(EDITORIAL_EXACT_SPAN_STRATEGY, 10, TimeBase(1, 10)),
        CandidateExactSpanPolicy(100_000, 100_000, 1, 1, 1),
        ProductionRecipeCompilationLimits(entries, member_payload, total_payload),
    )


def _install_non_dialogue_blueprint_projection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise persistence with the fixture's known-speech authority.

    The shared committed fixture deliberately requests complete dialogue and
    therefore also supplies the denial vector below.  This narrow projection
    changes only that semantic requirement for the positive transaction test.
    """
    original = command_module.derive_editorial_exact_span_query

    def derive(**values):  # type: ignore[no-untyped-def]
        query = original(**values)
        return replace(
            query,
            dialogue_protection_kind="known_speech",
            request=replace(
                query.request,
                dialogue_requirement=DialogueRequirement.NOT_REQUIRED,
            ),
        )

    monkeypatch.setattr(command_module, "derive_editorial_exact_span_query", derive)


def test_cpu_success_is_atomic_exact_and_replayable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_non_dialogue_blueprint_projection(monkeypatch)
    case = editorial_timed_media_case(tmp_path, monkeypatch)
    base, *_rest, resolver, limits = case
    store = _Stage4Store(base)
    request = _request(case)

    first = CompileProductionRecipeCommand(store, resolver, limits).execute(request)

    assert first.outcome.state == "succeeded", (
        first.outcome.failure_code,
        first.outcome.failure_detail_json,
    )
    assert first.committed is not None
    assert first.committed.report.backend_discriminator == "installed_cpu_profile"
    assert first.committed.admission.render_authorized is True
    assert first.committed.admission.admission.next_action == "render"
    assert first.committed.admission.admission.authority_status == "unverified"
    assert (
        first.committed.admission.expected_input_binding_sha256
        == first.committed.report.input_binding_sha256
    )
    assert (
        first.committed.admission.expected_authority_sha256
        == first.committed.report.authority_sha256
    )
    assert (
        first.committed.admission.expected_editorial_exact_policy_sha256
        == request.editorial_exact_span_policy.canonical_hash
    )
    assert (
        first.committed.admission.expected_candidate_exact_policy_sha256
        == request.candidate_exact_span_policy.canonical_hash
    )
    assert tuple(member.reference.artifact_type for member in first.committed.record.members) == (
        "physical_edit_compilation_report",
        *("recipe" for _ in first.committed.recipes),
        "physical_edit_admission",
    )
    replay = CompileProductionRecipeCommand(store, resolver, limits).execute(request)
    assert replay.outcome.is_fresh_claim is False
    assert replay.committed == first.committed
    assert (
        read_committed_production_recipe_set(
            store, request, first.outcome, authority_profile_resolver=resolver, limits=limits
        )
        == first.committed
    )


def test_stage4_commit_capability_is_exact_and_tamper_evident(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_non_dialogue_blueprint_projection(monkeypatch)
    case = editorial_timed_media_case(tmp_path, monkeypatch)
    request = _request(case)
    base, *_rest, resolver, limits = case
    resolved = resolve_compile_production_recipe_request(
        base,
        request,
        authority_profile_resolver=resolver,
        limits=limits,
    )
    report, recipes, admission, verified = command_module._compile_and_admit(resolved)
    artifacts = command_module._artifacts(resolved, report, recipes, admission)
    success = CommandSuccess(uuid4(), artifact_set_hash(artifacts), artifacts)
    capability = command_module._issue_verified_production_recipe_commit(
        success,
        verified,
    )

    assert command_module._open_verified_production_recipe_commit(capability) == success
    with pytest.raises(CompileProductionRecipeError, match="verified commit capability"):
        command_module._open_verified_production_recipe_commit(success)
    with pytest.raises(CompileProductionRecipeError, match="modified"):
        command_module._open_verified_production_recipe_commit(
            replace(capability, _verification_mac=b"\x00" * 32)
        )


def test_entry_limit_denies_without_an_artifact_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = editorial_timed_media_case(tmp_path, monkeypatch)
    base, *_rest, resolver, limits = case
    store = _Stage4Store(base)

    result = CompileProductionRecipeCommand(store, resolver, limits).execute(
        _request(case, entries=1)
    )

    assert result.outcome.state == "denied"
    assert result.outcome.failure_code == STAGE4_COMPILATION_BLOCKED
    assert result.committed is None
    assert store.stage4_record is None


def test_member_and_total_payload_limits_are_independent_exact_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_non_dialogue_blueprint_projection(monkeypatch)
    case = editorial_timed_media_case(tmp_path, monkeypatch)
    base, *_rest, resolver, limits = case
    sizing_store = _Stage4Store(base)
    sizing = CompileProductionRecipeCommand(sizing_store, resolver, limits).execute(_request(case))
    assert sizing.outcome.state == "succeeded" and sizing_store.stage4_record is not None
    payload_sizes = tuple(
        len(member.payload_json.encode("utf-8")) for member in sizing_store.stage4_record.members
    )
    max_member = max(payload_sizes)
    total = sum(payload_sizes)

    exact_store = _Stage4Store(base)
    exact = CompileProductionRecipeCommand(exact_store, resolver, limits).execute(
        _request(case, member_payload=max_member, total_payload=total)
    )
    assert exact.outcome.state == "succeeded"

    member_store = _Stage4Store(base)
    member_denial = CompileProductionRecipeCommand(member_store, resolver, limits).execute(
        _request(case, member_payload=max_member - 1, total_payload=total)
    )
    assert member_denial.outcome.state == "denied"
    assert member_denial.outcome.failure_code == STAGE4_COMPILATION_BLOCKED
    assert member_store.stage4_record is None

    total_store = _Stage4Store(base)
    total_denial = CompileProductionRecipeCommand(total_store, resolver, limits).execute(
        _request(case, member_payload=max_member, total_payload=total - 1)
    )
    assert total_denial.outcome.state == "denied"
    assert total_denial.outcome.failure_code == STAGE4_COMPILATION_BLOCKED
    assert total_store.stage4_record is None


def test_exact_reader_accepts_postgres_jsonb_display_at_canonical_byte_caps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_non_dialogue_blueprint_projection(monkeypatch)
    case = editorial_timed_media_case(tmp_path, monkeypatch)
    base, *_rest, resolver, limits = case
    sizing_store = _Stage4Store(base)
    sizing = CompileProductionRecipeCommand(sizing_store, resolver, limits).execute(_request(case))
    assert sizing.outcome.state == "succeeded" and sizing_store.stage4_record is not None
    payload_sizes = tuple(
        len(member.payload_json.encode("utf-8")) for member in sizing_store.stage4_record.members
    )
    request = _request(
        case,
        member_payload=max(payload_sizes),
        total_payload=sum(payload_sizes),
    )
    store = _Stage4Store(base)
    result = CompileProductionRecipeCommand(store, resolver, limits).execute(request)
    assert result.outcome.state == "succeeded" and store.stage4_record is not None
    canonical_record = store.stage4_record
    store.stage4_record = _replace_record_payloads_with_jsonb_display(canonical_record)
    assert (
        sum(len(member.payload_json.encode("utf-8")) for member in store.stage4_record.members)
        > request.compilation_limits.max_total_payload_bytes
    )
    assert any(
        persisted.payload_json != canonical.payload_json
        for persisted, canonical in zip(
            store.stage4_record.members,
            canonical_record.members,
            strict=True,
        )
    )

    reread = read_committed_production_recipe_set(
        store,
        request,
        result.outcome,
        authority_profile_resolver=resolver,
        limits=limits,
    )

    assert reread.report == result.committed.report
    assert reread.recipes == result.committed.recipes
    assert reread.admission == result.committed.admission


def test_exact_reader_rejects_unbounded_jsonb_transport_expansion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_non_dialogue_blueprint_projection(monkeypatch)
    case = editorial_timed_media_case(tmp_path, monkeypatch)
    base, *_rest, resolver, limits = case
    sizing_store = _Stage4Store(base)
    sizing = CompileProductionRecipeCommand(sizing_store, resolver, limits).execute(_request(case))
    assert sizing.outcome.state == "succeeded" and sizing_store.stage4_record is not None
    payload_sizes = tuple(
        len(member.payload_json.encode("utf-8")) for member in sizing_store.stage4_record.members
    )
    request = _request(
        case,
        member_payload=max(payload_sizes),
        total_payload=sum(payload_sizes),
    )
    store = _Stage4Store(base)
    result = CompileProductionRecipeCommand(store, resolver, limits).execute(request)
    assert result.outcome.state == "succeeded" and store.stage4_record is not None
    target = store.stage4_record.members[0]
    expanded = target.payload_json.replace(
        ",",
        "," + " " * (request.compilation_limits.max_member_payload_bytes * 3 + 8_192),
        1,
    )
    changed = replace(target, payload_json=expanded)
    store.stage4_record = replace(
        store.stage4_record,
        members=(changed, *store.stage4_record.members[1:]),
    )

    with pytest.raises(
        CompileProductionRecipeError,
        match="JSONB transport exceeds its bounded read allowance",
    ):
        read_committed_production_recipe_set(
            store,
            request,
            result.outcome,
            authority_profile_resolver=resolver,
            limits=limits,
        )


def test_exact_reader_rejects_member_count_amplification_before_decoding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_non_dialogue_blueprint_projection(monkeypatch)
    case = editorial_timed_media_case(tmp_path, monkeypatch)
    base, *_rest, resolver, limits = case
    store = _Stage4Store(base)
    request = _request(case, entries=4)
    result = CompileProductionRecipeCommand(store, resolver, limits).execute(request)
    assert result.outcome.state == "succeeded" and store.stage4_record is not None
    record = store.stage4_record
    extra_count = request.compilation_limits.max_compilation_entries + 3 - len(record.members)
    extra_payload = "{}"
    extra_hash = canonical_payload_hash(extra_payload)
    extras = tuple(
        PersistedCommittedArtifactMember(
            CommittedArtifactMemberReference(
                record.receipt_id,
                record.artifact_set_id,
                len(record.members) + index,
                request.artifact_scope,
                "unexpected_stage4_member",
                f"unexpected-{index}",
                request.artifact_revision,
                extra_hash,
            ),
            extra_payload,
            record.command_slot_id,
        )
        for index in range(extra_count)
    )
    members = (*record.members, *extras)
    artifacts = tuple(
        ArtifactMember(
            member.reference.artifact_type,
            member.reference.logical_id,
            member.reference.revision,
            member.reference.scope,
            member.reference.content_hash,
            member.payload_json,
        )
        for member in members
    )
    store.stage4_record = replace(
        record,
        members=members,
        set_hash=artifact_set_hash(artifacts),
    )

    with pytest.raises(
        CompileProductionRecipeError,
        match="member count exceeds its frozen compilation bound",
    ):
        read_committed_production_recipe_set(
            store,
            request,
            result.outcome,
            authority_profile_resolver=resolver,
            limits=limits,
        )


def test_request_hash_binds_cpu_cuda_backend_discriminator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = editorial_timed_media_case(tmp_path, monkeypatch)
    base, *_rest, resolver, limits = case
    store = _Stage4Store(base)
    request = _request(case)
    installed = resolve_compile_production_recipe_request(
        store, request, authority_profile_resolver=resolver, limits=limits
    )

    monkeypatch.setattr(
        command_module,
        "_read_all_media",
        lambda *_args, **_kwargs: (
            installed.media,
            installed.authority,
            "runtime_cuda_capability",
        ),
    )
    runtime_discriminator = resolve_compile_production_recipe_request(
        store, request, authority_profile_resolver=resolver, limits=limits
    )

    assert installed.backend_discriminator == "installed_cpu_profile"
    assert runtime_discriminator.backend_discriminator == "runtime_cuda_capability"
    assert runtime_discriminator.request_hash != installed.request_hash


def test_complete_dialogue_without_sentence_authority_is_a_stable_denial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = editorial_timed_media_case(tmp_path, monkeypatch)
    base, *_rest, resolver, limits = case
    store = _Stage4Store(base)

    result = CompileProductionRecipeCommand(store, resolver, limits).execute(_request(case))

    assert result.outcome.state == "denied"
    assert result.outcome.failure_code == "STAGE4_DIALOGUE_EVIDENCE_INDETERMINATE"
    assert result.committed is None
    assert store.stage4_record is None


def test_output_timing_indeterminate_denies_without_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_non_dialogue_blueprint_projection(monkeypatch)
    case = editorial_timed_media_case(tmp_path, monkeypatch)
    base, *_rest, resolver, limits = case
    store = _Stage4Store(base)

    def reject_timing(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise ValueError("timing mismatch")

    monkeypatch.setattr(command_module, "verify_editorial_timing", reject_timing)
    result = CompileProductionRecipeCommand(store, resolver, limits).execute(_request(case))

    assert result.outcome.state == "denied"
    assert result.outcome.failure_code == STAGE4_OUTPUT_TIMING_INDETERMINATE
    assert result.committed is None
    assert store.stage4_record is None


def test_exact_reader_rejects_a_coherently_rehashed_recipe_tamper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_non_dialogue_blueprint_projection(monkeypatch)
    case = editorial_timed_media_case(tmp_path, monkeypatch)
    base, *_rest, resolver, limits = case
    store = _Stage4Store(base)
    request = _request(case)
    result = CompileProductionRecipeCommand(store, resolver, limits).execute(request)
    assert result.outcome.state == "succeeded" and store.stage4_record is not None
    record = store.stage4_record
    target = record.members[1]
    value = json.loads(target.payload_json)
    value["profile_id"] = "foreign-production-profile"
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    changed_ref = replace(target.reference, content_hash=canonical_payload_hash(payload))
    changed_member = PersistedCommittedArtifactMember(changed_ref, payload, target.command_slot_id)
    changed_members = (record.members[0], changed_member, *record.members[2:])
    changed_artifacts = tuple(
        replace(
            artifact, content_hash=member.reference.content_hash, payload_json=member.payload_json
        )
        for artifact, member in zip(record.artifacts, changed_members, strict=True)
    )
    store.stage4_record = replace(
        record,
        members=changed_members,
        set_hash=artifact_set_hash(changed_artifacts),
    )

    with pytest.raises(CompileProductionRecipeError):
        read_committed_production_recipe_set(
            store,
            request,
            result.outcome,
            authority_profile_resolver=resolver,
            limits=limits,
        )


def test_exact_reader_rejects_a_coherently_rehashed_admission_binding_tamper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_non_dialogue_blueprint_projection(monkeypatch)
    case = editorial_timed_media_case(tmp_path, monkeypatch)
    base, *_rest, resolver, limits = case
    store = _Stage4Store(base)
    request = _request(case)
    result = CompileProductionRecipeCommand(store, resolver, limits).execute(request)
    assert result.outcome.state == "succeeded" and store.stage4_record is not None
    record = store.stage4_record
    target = record.members[-1]
    value = json.loads(target.payload_json)
    value["input_binding_sha256"] = "sha256:" + "f" * 64
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    changed_ref = replace(target.reference, content_hash=canonical_payload_hash(payload))
    changed_member = PersistedCommittedArtifactMember(changed_ref, payload, target.command_slot_id)
    changed_members = (*record.members[:-1], changed_member)
    changed_artifacts = tuple(
        replace(
            artifact,
            content_hash=member.reference.content_hash,
            payload_json=member.payload_json,
        )
        for artifact, member in zip(record.artifacts, changed_members, strict=True)
    )
    store.stage4_record = replace(
        record,
        members=changed_members,
        set_hash=artifact_set_hash(changed_artifacts),
    )

    with pytest.raises(CompileProductionRecipeError):
        read_committed_production_recipe_set(
            store,
            request,
            result.outcome,
            authority_profile_resolver=resolver,
            limits=limits,
        )
