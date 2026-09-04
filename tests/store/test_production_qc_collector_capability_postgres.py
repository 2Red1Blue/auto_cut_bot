"""Disposable PostgreSQL acceptance for QC collector capability authority."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest
from autocut_kernel.pipeline.accept_production_qc_collector_capability_command import (
    AcceptProductionRenderQcCollectorCapabilityCommand,
)
from autocut_kernel.rendering.production_qc_collector_capability import (
    ProductionQcCollectorCapabilityRequest,
    ProductionQcCollectorExecutableIdentity,
    ProductionQcCollectorLiveProfile,
)
from autocut_kernel.store import (
    CommandRejection,
    CommandStateError,
    CommandSuccess,
    IdempotencyConflictError,
    PostgresRuntimeStore,
    ProductionQcCollectorCapabilityBinding,
    ProductionQcCollectorCapabilityIdentityDriftError,
    ProductionQcCollectorCapabilityUnavailableError,
    RuntimeStoreError,
)

from tests.store.test_production_qc_collector_capability_models import (
    _binding,
    _installed,
    _live_profile,
)

psycopg = pytest.importorskip("psycopg")

DSN = os.environ.get("AUTOCUT_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(
    not DSN,
    reason="set AUTOCUT_TEST_POSTGRES_DSN to disposable ac_autocut_verify",
)
MIGRATIONS = Path("packages/autocut-kernel/migrations")


@pytest.fixture(scope="module", autouse=True)
def migrated_database() -> None:
    assert DSN is not None
    with psycopg.connect(DSN, autocommit=True) as connection:
        if connection.info.dbname != "ac_autocut_verify":
            pytest.fail("capability tests may reset only ac_autocut_verify")
        with connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA IF EXISTS storage CASCADE")
            cursor.execute("DROP SCHEMA IF EXISTS runtime CASCADE")
            for migration in sorted(MIGRATIONS.glob("*.sql")):
                cursor.execute(migration.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def store() -> PostgresRuntimeStore:
    assert DSN is not None
    return PostgresRuntimeStore(lambda: psycopg.connect(DSN))


def _drifted_identity(executable_sha256: str) -> ProductionQcCollectorExecutableIdentity:
    return ProductionQcCollectorExecutableIdentity(executable_sha256, 128, "sha256:" + "d" * 64)


def _unique_profile(seed: str) -> ProductionQcCollectorLiveProfile:
    import hashlib

    resource = _installed()
    policy = resource.policy
    return ProductionQcCollectorLiveProfile(
        policy.profile_id,
        policy.policy_source_sha256,
        policy.registry_snapshot_sha256,
        policy.required_check_set_version,
        policy.collector_registry_sha256,
        policy.runner_schema_version,
        policy.fixed_environment_sha256,
        _drifted_identity(
            "sha256:" + hashlib.sha256(("ffmpeg:" + seed).encode()).hexdigest()
        ),
        _drifted_identity(
            "sha256:" + hashlib.sha256(("ffprobe:" + seed).encode()).hexdigest()
        ),
    )


def _unique_binding(seed: str) -> ProductionQcCollectorCapabilityBinding:
    """One capability identity per test so fresh claims never share a slot."""

    import hashlib

    resource = _installed()
    policy = resource.policy
    ffmpeg = _drifted_identity(
        "sha256:" + hashlib.sha256(("ffmpeg:" + seed).encode()).hexdigest()
    )
    ffprobe = _drifted_identity(
        "sha256:" + hashlib.sha256(("ffprobe:" + seed).encode()).hexdigest()
    )
    live = ProductionQcCollectorLiveProfile(
        policy.profile_id,
        policy.policy_source_sha256,
        policy.registry_snapshot_sha256,
        policy.required_check_set_version,
        policy.collector_registry_sha256,
        policy.runner_schema_version,
        policy.fixed_environment_sha256,
        ffmpeg,
        ffprobe,
    )
    request = ProductionQcCollectorCapabilityRequest(policy, live)
    return ProductionQcCollectorCapabilityBinding(request, resource.provenance)


def test_accept_persists_immutable_capability_closure(store: PostgresRuntimeStore) -> None:
    resource = _installed()
    outcome = AcceptProductionRenderQcCollectorCapabilityCommand(store).execute(
        resource, _unique_profile("accept")
    )
    assert outcome.state == "succeeded"
    assert outcome.is_fresh_claim is True
    assert outcome.receipt_id is not None and outcome.artifact_set_id is not None

    with psycopg.connect(DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT decision, profile_id, required_check_set_version, runner_schema_version,
                   receipt_id, artifact_set_id, command_slot_id, authority_revision
              FROM runtime.production_qc_collector_capabilities
             WHERE profile_id = %s
            """,
            (resource.policy.profile_id,),
        )
        row = cursor.fetchone()
        assert cursor.fetchone() is None
        assert row is not None
        assert row[0] == "accepted"
        assert row[4] == outcome.receipt_id
        assert row[5] == outcome.artifact_set_id
        assert row[6] == outcome.command_slot_id
        assert row[7] == resource.provenance.authority_revision

        cursor.execute(
            "SELECT state, command_name FROM runtime.command_slots WHERE command_slot_id = %s",
            (outcome.command_slot_id,),
        )
        assert cursor.fetchone() == ("succeeded", "AcceptProductionRenderQcCollectorCapability@1")
        cursor.execute("SELECT state FROM runtime.jobs WHERE job_id = %s", (outcome.job_id,))
        assert cursor.fetchone() == ("succeeded",)
        cursor.execute(
            "SELECT outcome, result_artifact_set_id FROM runtime.command_receipts"
            " WHERE receipt_id = %s",
            (outcome.receipt_id,),
        )
        receipt = cursor.fetchone()
        assert receipt is not None
        assert receipt[0] == "succeeded" and receipt[1] == outcome.artifact_set_id
        cursor.execute(
            "SELECT count(*) FROM runtime.artifact_set_members WHERE artifact_set_id = %s",
            (outcome.artifact_set_id,),
        )
        assert cursor.fetchone() == (2,)
        cursor.execute(
            """
            SELECT ordinal, artifact.artifact_type, artifact.logical_id, artifact.revision
              FROM runtime.artifact_set_members AS member
              JOIN runtime.artifacts AS artifact ON artifact.artifact_id = member.artifact_id
             WHERE member.artifact_set_id = %s
             ORDER BY ordinal
            """,
            (outcome.artifact_set_id,),
        )
        members = cursor.fetchall()
        assert [(m[0], m[1], m[2], m[3]) for m in members] == [
            (0, "production_qc_collector_measurement", "measurement", 1),
            (1, "production_qc_collector_capability", "decision", 1),
        ]


def test_replay_returns_same_outcome_without_duplicate_work(
    store: PostgresRuntimeStore,
) -> None:
    resource = _installed()
    command = AcceptProductionRenderQcCollectorCapabilityCommand(store)
    profile = _live_profile(resource.policy)
    first = command.execute(resource, profile)
    second = command.execute(resource, profile)
    assert second.state == "succeeded"
    assert second.is_fresh_claim is False
    assert second.command_slot_id == first.command_slot_id
    assert second.receipt_id == first.receipt_id
    assert second.artifact_set_id == first.artifact_set_id


def test_commit_replay_after_response_lost_returns_same_closure(
    store: PostgresRuntimeStore,
) -> None:
    binding = _unique_binding("commit-replay")
    claim = store.claim_qc_collector_capability_command(binding.claim)
    assert claim.is_fresh_claim is True
    outcome = store.commit_qc_collector_capability_success(
        CommandSuccess(
            claim.command_slot_id,
            binding.expected_set_hash,
            binding.members,
        ),
        binding,
    )
    replay = store.commit_qc_collector_capability_success(
        CommandSuccess(
            claim.command_slot_id,
            binding.expected_set_hash,
            binding.members,
        ),
        binding,
    )
    assert replay.state == "succeeded"
    assert replay.receipt_id == outcome.receipt_id
    assert replay.artifact_set_id == outcome.artifact_set_id


def test_succeeded_command_replay_rejects_changed_provenance(
    store: PostgresRuntimeStore,
) -> None:
    resource = _installed()
    profile = _unique_profile("provenance-replay")
    command = AcceptProductionRenderQcCollectorCapabilityCommand(store)
    first = command.execute(resource, profile)
    assert first.state == "succeeded"
    changed = replace(
        resource,
        provenance=replace(resource.provenance, source_commit="a" * 40),
    )
    with pytest.raises(CommandStateError, match="different artifact set"):
        command.execute(changed, profile)
    assert command.execute(resource, profile).receipt_id == first.receipt_id


@pytest.mark.parametrize("ack_lost", [False, True], ids=["before-commit", "after-commit"])
def test_ambiguous_commit_is_reconciled_without_rejection(
    monkeypatch: pytest.MonkeyPatch, ack_lost: bool,
) -> None:
    """Inject a driver failure at the real connection's second commit (success)."""
    commits = 0

    class FaultingConnection:
        def __init__(self) -> None:
            self.connection = psycopg.connect(DSN)

        def cursor(self):
            return self.connection.cursor()

        def commit(self) -> None:
            nonlocal commits
            commits += 1
            if commits == 2:
                if ack_lost:
                    self.connection.commit()
                raise psycopg.OperationalError("injected commit acknowledgement failure")
            self.connection.commit()

        def rollback(self) -> None:
            self.connection.rollback()

        def close(self) -> None:
            self.connection.close()

    faulting_store = PostgresRuntimeStore(FaultingConnection)

    def forbidden_rejection(*args, **kwargs):
        pytest.fail("an ambiguous commit must not write a rejection")

    monkeypatch.setattr(faulting_store, "commit_command_rejection", forbidden_rejection)
    resource = _installed()
    profile = _unique_profile(f"commit-fault-{ack_lost}")
    command = AcceptProductionRenderQcCollectorCapabilityCommand(faulting_store)
    with pytest.raises(RuntimeStoreError, match="database operation failed"):
        command.execute(resource, profile)
    # The same identity resumes a rolled-back transaction or replays its commit;
    # neither case generates another command slot or contradictory Receipt.
    recovered = command.execute(resource, profile)
    replay = command.execute(resource, profile)
    assert recovered.state == replay.state == "succeeded"
    assert recovered.command_slot_id == replay.command_slot_id
    assert recovered.receipt_id == replay.receipt_id
    assert recovered.artifact_set_id == replay.artifact_set_id


def test_generic_command_boundary_rejects_protected_capability(store: PostgresRuntimeStore) -> None:
    from autocut_kernel.store import CommandClaim, Job

    binding = _binding()
    with pytest.raises(CommandStateError, match="owner API"):
        store.claim_command(
            CommandClaim(
                binding.job,
                binding.attempt_idempotency_key,
                "AcceptProductionRenderQcCollectorCapability@1",
                binding.request_hash,
                execution_kind="deterministic",
            )
        )
    with pytest.raises(CommandStateError, match="owner API"):
        store.claim_command(
            CommandClaim(
                Job("unrelated-job", "test"),
                "production-qc-collector-capability:prefix-only",
                "OtherCommand",
                "sha256:" + "0" * 63 + "1",
                execution_kind="deterministic",
            )
        )


def test_generic_success_writer_rejects_capability_command(
    store: PostgresRuntimeStore,
) -> None:
    binding = _unique_binding("generic-success-writer")
    claim = store.claim_qc_collector_capability_command(binding.claim)
    assert claim.is_fresh_claim is True
    with pytest.raises(CommandStateError, match="owner API"):
        store.commit_command_success(
            CommandSuccess(claim.command_slot_id, binding.expected_set_hash, binding.members)
        )
    # Close the slot so later tests are unaffected.
    outcome = store.commit_qc_collector_capability_success(
        CommandSuccess(claim.command_slot_id, binding.expected_set_hash, binding.members),
        binding,
    )
    assert outcome.state == "succeeded"


def test_conflicting_claim_under_same_identity_is_rejected(store: PostgresRuntimeStore) -> None:
    from autocut_kernel.store import CommandClaim

    binding = _unique_binding("conflicting-claim")
    store.claim_qc_collector_capability_command(binding.claim)
    with pytest.raises(IdempotencyConflictError):
        store.claim_qc_collector_capability_command(
            CommandClaim(
                binding.job,
                binding.attempt_idempotency_key,
                "AcceptProductionRenderQcCollectorCapability@1",
                "sha256:" + "1" * 64,
                execution_kind="deterministic",
            )
        )


def test_denial_owns_no_set_member_or_capability_row(store: PostgresRuntimeStore) -> None:
    binding = _unique_binding("denial")
    claim = store.claim_qc_collector_capability_command(binding.claim)
    assert claim.is_fresh_claim is True
    outcome = store.commit_command_rejection(
        CommandRejection(
            claim.command_slot_id,
            "PRODUCTION_QC_COLLECTOR_CAPABILITY_DENIED",
            '{"reason":"validator refused the measurement","stage":"test"}',
            "denied",
        )
    )
    assert outcome.state == "denied"
    with psycopg.connect(DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM runtime.artifact_sets WHERE job_id = %s",
            (outcome.job_id,),
        )
        assert cursor.fetchone() == (0,)
        cursor.execute(
            "SELECT count(*) FROM runtime.production_qc_collector_capabilities"
            " WHERE command_slot_id = %s",
            (claim.command_slot_id,),
        )
        assert cursor.fetchone() == (0,)


def test_capability_row_is_insert_only_and_durable(store: PostgresRuntimeStore) -> None:
    with psycopg.connect(DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT scope_key, capability_request_json, receipt_id, artifact_set_id
              FROM runtime.production_qc_collector_capabilities
             LIMIT 1
            """
        )
        row = cursor.fetchone()
        assert row is not None
        with pytest.raises(psycopg.errors.RaiseException, match="insert-only"):
            cursor.execute(
                "UPDATE runtime.production_qc_collector_capabilities SET decision = 'accepted'"
                " WHERE scope_key = %s",
                (row[0],),
            )
        connection.rollback()
        with pytest.raises(psycopg.errors.RaiseException, match="cannot be deleted"):
            cursor.execute(
                "DELETE FROM runtime.production_qc_collector_capabilities WHERE scope_key = %s",
                (row[0],),
            )
        connection.rollback()


def test_accepted_members_are_immutable(store: PostgresRuntimeStore) -> None:
    resource = _installed()
    outcome = AcceptProductionRenderQcCollectorCapabilityCommand(store).execute(
        resource, _unique_profile("immutable-members")
    )
    assert outcome.state == "succeeded" and outcome.artifact_set_id is not None
    with psycopg.connect(DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT artifact.artifact_id
              FROM runtime.artifact_set_members AS member
              JOIN runtime.artifacts AS artifact ON artifact.artifact_id = member.artifact_id
             WHERE member.artifact_set_id = %s AND member.ordinal = 0
            """,
            (outcome.artifact_set_id,),
        )
        row = cursor.fetchone()
        assert row is not None
        with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
            cursor.execute(
                "UPDATE runtime.artifacts SET payload_json = payload_json"
                " WHERE artifact_id = %s",
                (row[0],),
            )
        connection.rollback()


def test_resolve_returns_exact_verified_projection(store: PostgresRuntimeStore) -> None:
    resource = _installed()
    request = ProductionQcCollectorCapabilityRequest(
        resource.policy, _unique_profile("resolve")
    )
    outcome = AcceptProductionRenderQcCollectorCapabilityCommand(store).execute(
        resource, request.live_profile
    )
    assert outcome.state == "succeeded"
    capability = store.resolve_accepted_production_qc_collector_capability(request)
    assert capability.request == request
    assert capability.provenance == resource.provenance
    assert capability.measurement_member_sha256.startswith("sha256:")
    assert capability.capability_member_sha256.startswith("sha256:")
    assert capability.measurement_member_sha256 != capability.capability_member_sha256
    with psycopg.connect(DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT receipt_id, artifact_set_id, command_slot_id, scope_key"
            "  FROM runtime.production_qc_collector_capabilities"
            " WHERE capability_request_sha256 = %s",
            (request.canonical_sha256,),
        )
        row = cursor.fetchone()
        assert row is not None
        assert row[0] == capability.receipt_id
        assert row[1] == capability.artifact_set_id
        assert row[2] == capability.command_slot_id
        assert row[3] == capability.scope_key


def test_resolve_is_unavailable_for_unknown_lineage(store: PostgresRuntimeStore) -> None:
    from autocut_kernel.rendering.production_qc_collector_capability import (
        ProductionQcCollectorPolicySource,
    )

    resource = _installed()
    policy = resource.policy
    synthetic = ProductionQcCollectorPolicySource(
        policy.profile_id,
        "sha256:" + "9" * 64,
        policy.registry_snapshot_sha256,
        policy.required_check_set_version,
        policy.collector_registry_sha256,
        policy.runner_schema_version,
        policy.fixed_environment_sha256,
    )
    request = ProductionQcCollectorCapabilityRequest(
        synthetic,
        ProductionQcCollectorLiveProfile(
            synthetic.profile_id,
            synthetic.policy_source_sha256,
            synthetic.registry_snapshot_sha256,
            synthetic.required_check_set_version,
            synthetic.collector_registry_sha256,
            synthetic.runner_schema_version,
            synthetic.fixed_environment_sha256,
            _drifted_identity("sha256:" + "a" * 64),
            _drifted_identity("sha256:" + "c" * 64),
        ),
    )
    with pytest.raises(ProductionQcCollectorCapabilityUnavailableError):
        store.resolve_accepted_production_qc_collector_capability(request)


def test_resolve_detects_identity_drift_within_lineage(store: PostgresRuntimeStore) -> None:
    resource = _installed()
    drifted = ProductionQcCollectorLiveProfile(
        resource.policy.profile_id,
        resource.policy.policy_source_sha256,
        resource.policy.registry_snapshot_sha256,
        resource.policy.required_check_set_version,
        resource.policy.collector_registry_sha256,
        resource.policy.runner_schema_version,
        resource.policy.fixed_environment_sha256,
        _drifted_identity("sha256:" + "1" * 64),
        _drifted_identity("sha256:" + "c" * 64),
    )
    request = ProductionQcCollectorCapabilityRequest(resource.policy, drifted)
    with pytest.raises(ProductionQcCollectorCapabilityIdentityDriftError):
        store.resolve_accepted_production_qc_collector_capability(request)


def test_new_tool_measurement_receives_fresh_authority_acceptance(
    store: PostgresRuntimeStore,
) -> None:
    """An FFmpeg upgrade is accepted under the same policy, never silently reused."""

    resource = _installed()
    upgraded = ProductionQcCollectorLiveProfile(
        resource.policy.profile_id,
        resource.policy.policy_source_sha256,
        resource.policy.registry_snapshot_sha256,
        resource.policy.required_check_set_version,
        resource.policy.collector_registry_sha256,
        resource.policy.runner_schema_version,
        resource.policy.fixed_environment_sha256,
        _drifted_identity("sha256:" + "2" * 64),
        _drifted_identity("sha256:" + "3" * 64),
    )
    outcome = AcceptProductionRenderQcCollectorCapabilityCommand(store).execute(
        resource, upgraded
    )
    assert outcome.state == "succeeded"
    with psycopg.connect(DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM runtime.production_qc_collector_capabilities"
            " WHERE profile_id = %s",
            (resource.policy.profile_id,),
        )
        assert cursor.fetchone()[0] >= 2
