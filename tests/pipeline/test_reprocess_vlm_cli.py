"""Operational CLI boundaries: explicit writes, strict input, and no secret output."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID

import pytest
from autocut_kernel.pipeline import reprocess_vlm_batch_command as batch_core
from autocut_kernel.pipeline import reprocess_vlm_evidence_command as reprocess_core
from autocut_kernel.store.models import (
    ArtifactMember,
    CommandOutcome,
    CommittedArtifactMemberReference,
    Job,
    canonical_payload_hash,
    canonical_recipe_scope,
)
from autocut_kernel.vlm.normalized_contracts import (
    VLM_PARSER_NORMALIZED_V4,
    parser_contract_sha256_for,
)

from scripts import reprocess_vlm_evidence as cli


class FakeStore:
    def __init__(self):
        self.claims = 0
        self.commits = 0
        self.reads = 0
        self.outcome = CommandOutcome(UUID(int=20), "succeeded", receipt_id=UUID(int=21), artifact_set_id=UUID(int=22))

    def claim_command(self, claim):
        assert claim.execution_kind == "deterministic"
        self.claims += 1
        return CommandOutcome(self.outcome.command_slot_id, "running")

    def commit_reprocessed_vlm_success(self, _request, _success):
        self.commits += 1
        return self.outcome

    def commit_derived_vlm_batch_success(self, _request, _success):
        self.commits += 1
        return self.outcome

    def dispatch(self, *_args, **_kwargs):
        raise AssertionError("CLI must never invoke a provider")

    def reconcile(self, *_args, **_kwargs):
        raise AssertionError("CLI must never reconcile a provider")


def _reference(job, kind, logical_id, ordinal=0):
    return CommittedArtifactMemberReference(UUID(int=1), UUID(int=2), ordinal,
        canonical_recipe_scope(job), kind, logical_id, 1, "sha256:" + "1" * 64)


def _request(mode, *, projection_version=2):
    job = Job("cli-recovery-fixture", "test")
    if mode == "reprocess":
        return reprocess_core.ReprocessVlmEvidenceRequest(
            job, UUID(int=3), UUID(int=4), UUID(int=5), "sha256:" + "2" * 64,
            "sha256:" + "3" * 64, "sha256:" + "4" * 64, UUID(int=2), 0, 1,
            parser_contract_sha256_for(VLM_PARSER_NORMALIZED_V4),
            projection_version=projection_version,
        )
    return batch_core.FinalizeDerivedVlmBatchRequest(job,
        _reference(job, "whole_series_source_manifest", "whole_series_source_manifest"),
        (batch_core.VlmBatchEvidenceSelection(0, "vlm-reprocess:fixture",
            _reference(job, "reprocessed_vlm_evidence", "reprocessed_vlm_fixture")),))


def _artifact(job, kind):
    raw = '{"fixture":true}'
    return ArtifactMember(kind, kind, 1, canonical_recipe_scope(job), canonical_payload_hash(raw), raw)


@pytest.fixture
def harness(tmp_path, monkeypatch):
    store = FakeStore()

    def forbid_configured_store():
        raise AssertionError("injected Store must not load a PostgreSQL connection")

    monkeypatch.setattr(cli, "_configured_store", forbid_configured_store)

    def make(mode, *, projection_version=2):
        request = _request(mode, projection_version=projection_version)
        path = tmp_path / f"{mode}.json"
        path.write_text(json.dumps(request.to_mapping()), encoding="utf-8")
        artifacts = ((_artifact(request.job, "reprocessed_vlm_evidence"), _artifact(request.job, "vlm_semantic_pack"))
                     if mode == "reprocess" else (_artifact(request.job, "vlm_semantic_pack_set"),))
        evidence = SimpleNamespace(artifacts=artifacts,
            normalization=SimpleNamespace(transformations=(SimpleNamespace(path="$.candidate_hypotheses[0].tags"),)))

        def rebuild(current_store, actual_request):
            assert current_store is store and actual_request == request
            current_store.reads += 1
            return evidence

        def rebuild_batch(current_store, actual_request):
            assert current_store is store and actual_request == request
            current_store.reads += 1
            return artifacts[0], None, None, None, ()

        monkeypatch.setattr(cli, "rebuild_reprocessed_vlm_evidence", rebuild)
        monkeypatch.setattr(reprocess_core, "rebuild_reprocessed_vlm_evidence", rebuild)
        monkeypatch.setattr(reprocess_core, "read_reprocessed_vlm_evidence", lambda *_args: evidence)
        monkeypatch.setattr(cli, "rebuild_derived_vlm_batch", rebuild_batch)
        monkeypatch.setattr(batch_core, "rebuild_derived_vlm_batch", rebuild_batch)
        monkeypatch.setattr(batch_core, "read_derived_vlm_semantic_inputs", lambda *_args: None)
        return store, path, request, artifacts

    return make


def test_v1_request_roundtrip_preserves_original_hash_and_v2_is_explicit():
    from autocut_kernel.media.types import canonical_sha256

    v1 = _request("reprocess", projection_version=1)
    existing_caller = reprocess_core.ReprocessVlmEvidenceRequest(**{
        name: getattr(v1, name) for name in v1.__dataclass_fields__ if name != "projection_version"
    })
    assert existing_caller == v1 and existing_caller.request_hash == v1.request_hash
    legacy = {
        "strategy_version": "reprocess-vlm-evidence-v1",
        "target_parser_strategy": VLM_PARSER_NORMALIZED_V4,
        "target_parser_contract_sha256": v1.target_parser_contract_sha256,
        "job": {"job_key": "cli-recovery-fixture", "profile": "test"},
        "parent_command_slot_id": str(UUID(int=3)), "parent_receipt_id": str(UUID(int=4)),
        "parent_attempt_id": str(UUID(int=5)), "parent_request_hash": "sha256:" + "2" * 64,
        "parent_request_payload_sha256": "sha256:" + "3" * 64,
        "parent_raw_response_sha256": "sha256:" + "4" * 64,
        "source_artifact_set_id": str(UUID(int=2)), "episode_index": 0,
        "parent_artifact_revision": 1, "provider_call_budget": 0,
    }
    assert v1.to_mapping() == legacy
    restored = reprocess_core.ReprocessVlmEvidenceRequest.from_mapping(legacy)
    assert restored == v1 and restored.request_hash == canonical_sha256(legacy)
    assert restored.idempotency_key == "vlm-reprocess:" + canonical_sha256(legacy)[7:]
    v2 = replace(v1, projection_version=2)
    assert v2.to_mapping() == {**legacy, "strategy_version": "reprocess-vlm-evidence-v2", "projection_version": 2}
    assert v2.request_hash != v1.request_hash and v2.idempotency_key != v1.idempotency_key
    assert reprocess_core.ReprocessVlmEvidenceRequest.from_mapping(v2.to_mapping()) == v2
    with pytest.raises(ValueError):
        reprocess_core.ReprocessVlmEvidenceRequest.from_mapping({**legacy, "projection_version": 2})
    with pytest.raises(ValueError):
        reprocess_core.ReprocessVlmEvidenceRequest.from_mapping({**legacy, "strategy_version": "reprocess-vlm-evidence-v2"})
    with pytest.raises(ValueError):
        reprocess_core.ReprocessVlmEvidenceRequest.from_mapping({**v2.to_mapping(), "projection_version": 2.0})


def test_cli_dry_run_never_silently_upgrades_a_v1_request(harness, capsys):
    store, path, request, _ = harness("reprocess", projection_version=1)
    assert cli.main(["--mode", "reprocess", "--request", str(path)], store=store) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["request_hash"] == request.request_hash
    assert report["idempotency_key"] == request.idempotency_key
    assert store.claims == store.commits == 0


@pytest.mark.parametrize("mode", ["reprocess", "finalize-batch"])
@pytest.mark.parametrize("flag", [[], ["--dry-run"]])
def test_default_and_explicit_dry_run_never_claim_or_commit(harness, capsys, mode, flag):
    store, path, request, artifacts = harness(mode)
    assert cli.main(["--mode", mode, "--request", str(path), *flag], store=store) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "validated" and report["execution_requested"] is False
    assert report["request_hash"] == request.request_hash
    assert report["provider_calls"] == 0 and store.reads == 1
    assert store.claims == store.commits == 0
    assert "members" not in report and "receipt_id" not in report
    assert len(report["artifacts"]) == len(artifacts)


@pytest.mark.parametrize("mode", ["reprocess", "finalize-batch"])
def test_execute_uses_command_and_reports_exact_reusable_member_references(harness, capsys, mode):
    store, path, request, artifacts = harness(mode)
    assert cli.main(["--mode", mode, "--request", str(path), "--execute"], store=store) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "succeeded" and report["execution_requested"] is True
    assert report["idempotency_key"] == request.idempotency_key
    assert store.claims == store.commits == 1
    for ordinal, (mapping, artifact) in enumerate(zip(report["members"], artifacts, strict=True)):
        reference = CommittedArtifactMemberReference.from_mapping(mapping)
        assert reference.member_ordinal == ordinal
        assert reference.receipt_id == store.outcome.receipt_id
        assert reference.artifact_set_id == store.outcome.artifact_set_id
        assert reference.content_hash == artifact.content_hash
    assert all("payload_json" not in artifact for artifact in report["artifacts"])
    assert '{"fixture":true}' not in json.dumps(report)


@pytest.mark.parametrize("raw", [b'{"job":{},"job":{}}', b'{', b'{"x":NaN}', b'{"x":1.2}', b'\xff'])
def test_malformed_or_duplicate_json_is_rejected_before_store_use(tmp_path, capsys, raw):
    path = tmp_path / "request.json"
    path.write_bytes(raw)
    store = FakeStore()
    assert cli.main(["--mode", "reprocess", "--request", str(path)], store=store) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["phase"] == "request" and store.claims == store.commits == store.reads == 0


def test_oversized_request_is_rejected_before_store_use(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "MAX_REQUEST_BYTES", 8)
    path = tmp_path / "request.json"
    path.write_bytes(b" " * 9)
    store = FakeStore()
    assert cli.main(["--mode", "reprocess", "--request", str(path)], store=store) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "invalid_input"
    assert store.claims == 0


def test_deep_request_is_rejected_before_store_use(tmp_path, capsys):
    path = tmp_path / "request.json"
    path.write_text('{"x":' + '[' * 34 + '0' + ']' * 34 + '}', encoding="utf-8")
    store = FakeStore()
    assert cli.main(["--mode", "reprocess", "--request", str(path)], store=store) == 2
    assert json.loads(capsys.readouterr().out)["phase"] == "request"
    assert store.claims == store.commits == store.reads == 0


def test_dry_run_parser_rejection_is_safe_and_does_not_claim(harness, monkeypatch, capsys):
    store, path, _, _ = harness("reprocess")
    secret = "signed-response-secret"

    def reject(*_args):
        raise cli.VlmResponseRejected("UNKNOWN_REFERENCE", secret)

    monkeypatch.setattr(cli, "rebuild_reprocessed_vlm_evidence", reject)
    assert cli.main(["--mode", "reprocess", "--request", str(path)], store=store) == 1
    output = capsys.readouterr()
    assert secret not in output.out + output.err
    assert json.loads(output.out)["error_code"] == "UNKNOWN_REFERENCE"
    assert store.claims == store.commits == 0


@pytest.mark.parametrize("error_type, exit_code", [(ValueError, 2), (RuntimeError, 3)])
def test_execute_audit_failure_does_not_leave_a_slot_or_semantic_denial(harness, monkeypatch, capsys, error_type, exit_code):
    store, path, _, _ = harness("reprocess")

    def fail_audit(*_args):
        raise error_type("connection-or-parent-secret")

    monkeypatch.setattr(reprocess_core, "rebuild_reprocessed_vlm_evidence", fail_audit)
    assert cli.main(["--mode", "reprocess", "--request", str(path), "--execute"], store=store) == exit_code
    output = capsys.readouterr()
    assert "connection-or-parent-secret" not in output.out + output.err
    assert json.loads(output.out)["status"] != "denied"
    assert store.claims == store.commits == 0


def test_execute_audited_response_rejection_gets_a_new_durable_denial(harness, monkeypatch, capsys):
    store, path, _, _ = harness("reprocess")
    rejections = []

    def reject_response(*_args):
        raise cli.VlmResponseRejected("UNKNOWN_REFERENCE", "local response issue")

    def commit_denial(rejection):
        rejections.append(rejection)
        return CommandOutcome(store.outcome.command_slot_id, "denied", receipt_id=UUID(int=24),
                              failure_code=rejection.failure_code)

    monkeypatch.setattr(reprocess_core, "rebuild_reprocessed_vlm_evidence", reject_response)
    store.commit_command_rejection = commit_denial
    assert cli.main(["--mode", "reprocess", "--request", str(path), "--execute"], store=store) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "denied"
    assert store.claims == 1 and len(rejections) == 1 and store.commits == 0


@pytest.mark.parametrize("mode", ["reprocess", "finalize-batch"])
def test_terminal_denial_is_reported_without_payload_or_new_commit(harness, capsys, mode):
    store, path, _, _ = harness(mode)
    denied = CommandOutcome(store.outcome.command_slot_id, "denied", receipt_id=UUID(int=23),
                            failure_code="UNKNOWN_REFERENCE", failure_detail_json='{"secret":"hidden"}')
    store.claim_command = lambda _claim: denied
    assert cli.main(["--mode", mode, "--request", str(path), "--execute"], store=store) == 1
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert report["status"] == "denied" and report["error_code"] == "UNKNOWN_REFERENCE"
    assert "hidden" not in output.out + output.err
    assert "members" not in report and store.commits == 0


@pytest.mark.parametrize("error", [ValueError("private-report-secret"), RuntimeError("private-report-secret"),
                                  cli.VlmResponseRejected("UNKNOWN_REFERENCE", "private-report-secret")])
def test_postcommit_reporting_failure_preserves_success_and_exact_receipt(harness, monkeypatch, capsys, error):
    store, path, request, _ = harness("finalize-batch")

    def report_unavailable(*_args):
        raise error

    monkeypatch.setattr(cli, "rebuild_derived_vlm_batch", report_unavailable)
    assert cli.main(["--mode", "finalize-batch", "--request", str(path), "--execute"], store=store) == 3
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert report["status"] == "succeeded_reporting_incomplete" and report["command_state"] == "succeeded"
    assert report["error_code"] == "POST_COMMIT_REPORT_UNAVAILABLE"
    assert report["receipt_id"] == str(store.outcome.receipt_id)
    assert report["artifact_set_id"] == str(store.outcome.artifact_set_id)
    assert report["command_slot_id"] == str(store.outcome.command_slot_id)
    assert report["idempotency_key"] == request.idempotency_key
    assert report["members"] == report["artifacts"] == []
    assert report["next_action"] == "inspect_existing_receipt_without_changing_request"
    assert store.claims == store.commits == 1
    assert "private-report-secret" not in output.out + output.err


@pytest.mark.parametrize("mode", ["reprocess", "finalize-batch"])
@pytest.mark.parametrize("state", ["pending", "running"])
def test_nonterminal_outcomes_are_not_reported_as_permanent_denials(harness, monkeypatch, capsys, mode, state):
    store, path, _, _ = harness(mode)
    outcome = CommandOutcome(store.outcome.command_slot_id, state)
    if mode == "reprocess":
        monkeypatch.setattr(cli.ReprocessVlmEvidenceCommand, "execute",
                            lambda *_args: reprocess_core.ReprocessVlmEvidenceResult(outcome))
    else:
        monkeypatch.setattr(cli.FinalizeDerivedVlmBatchCommand, "execute", lambda *_args: outcome)
    assert cli.main(["--mode", mode, "--request", str(path), "--execute"], store=store) == 3
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == state
    assert report["receipt_id"] is None and report["artifact_set_id"] is None
    assert "members" not in report and store.commits == 0


def test_no_environment_configuration_is_safe_and_does_not_execute(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("AUTO_CUT_BOT_PIPELINE_KERNEL_POSTGRES_DSN", raising=False)
    monkeypatch.delenv("AUTO_CUT_BOT_PIPELINE_POSTGRES_DSN", raising=False)
    path = tmp_path / "request.json"
    path.write_text(json.dumps(_request("reprocess").to_mapping()), encoding="utf-8")
    assert cli.main(["--mode", "reprocess", "--request", str(path)]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["phase"] == "configuration" and report["status"] == "unavailable"


@pytest.mark.parametrize("kernel_override", [False, True])
def test_configured_store_uses_project_environment_names_without_connecting(monkeypatch, kernel_override):
    import psycopg
    from autocut_kernel.store import postgres

    fallback = "postgresql://operator:never-print@fallback/db"
    kernel = "postgresql://operator:never-print@kernel/db"
    monkeypatch.setenv("AUTO_CUT_BOT_PIPELINE_POSTGRES_DSN", fallback)
    monkeypatch.delenv("AUTO_CUT_BOT_PIPELINE_KERNEL_POSTGRES_DSN", raising=False)
    if kernel_override:
        monkeypatch.setenv("AUTO_CUT_BOT_PIPELINE_KERNEL_POSTGRES_DSN", kernel)
    connections = []
    monkeypatch.setattr(psycopg, "connect", lambda dsn: connections.append(dsn))
    monkeypatch.setattr(postgres, "PostgresRuntimeStore", lambda connection_factory: connection_factory)
    factory = cli._configured_store()
    assert connections == []
    factory()
    assert connections == [kernel if kernel_override else fallback]


def test_store_exception_never_prints_connection_credentials(harness, capsys):
    store, path, _, _ = harness("reprocess")
    secret = "postgresql://operator:do-not-print@private-host/production"

    def fail(_claim):
        raise RuntimeError(secret)

    store.claim_command = fail
    assert cli.main(["--mode", "reprocess", "--request", str(path), "--execute"], store=store) == 3
    output = capsys.readouterr()
    assert secret not in output.out + output.err
    assert json.loads(output.out)["error_code"] == "RECOVERY_EXECUTION_FAILED"


def test_conflicting_modes_and_unrecognized_secret_arguments_do_not_echo(capsys):
    assert cli.main(["--mode", "reprocess", "--request", "x", "--execute", "--dry-run"]) == 2
    capsys.readouterr()
    secret = "postgresql://operator:do-not-print@host/db"
    assert cli.main(["--mode", "reprocess", "--request", "x", "--dsn", secret]) == 2
    output = capsys.readouterr()
    assert secret not in output.out + output.err
