"""Stage 2 CLI over synthetic persistence/provider doubles, without live I/O."""

import json
from dataclasses import fields

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes
from autocut_kernel.vlm.provider_port import ProviderIndeterminate

from auto_cut_bot.pipeline.vlm.doubao_ark_provider import DoubaoArkVlmProviderConfig
from scripts import run_stage2_request as cli
from tests.semantic_chain.test_compile_story_portfolio_command import command_case


@pytest.fixture
def case(tmp_path, monkeypatch):
    store, provider, request, _ = command_case(max_attempts=1, backoff=())
    path = tmp_path / "request.json"
    path.write_bytes(canonical_json_bytes(request.to_mapping()))
    monkeypatch.setattr(cli, "_make_store", lambda _: store)
    monkeypatch.setattr(cli, "_make_provider", lambda *_: provider)
    return store, provider, request, ["--request", str(path), "--debug-root", str(tmp_path / "stage2")]


def _forbid(*args, **kwargs):
    raise AssertionError("forbidden provider/command call")


@pytest.mark.parametrize("args", [
    [],
    ["--request", "unused.json", "--debug-root", "/unused",
     "--unknown", "postgresql://private-user:private-password@host/private-db"],
    ["--request", "unused.json", "--debug-root", "/unused", "--execute", "--dry-run"],
])
def test_argument_errors_are_safe_json_without_stderr_echo(args, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_load_request", _forbid)
    assert cli.main(args) == 2
    output = capsys.readouterr()
    assert output.err == ""
    assert "private" not in output.out
    report = json.loads(output.out)
    assert report["phase"] == "arguments"
    assert report["exception_kind"] == "Stage2RequestCliError"


def test_default_dry_run_reads_exact_predecessor_and_never_dispatches(case, monkeypatch, capsys):
    store, provider, _, args = case
    before = len(store.attempts), len(store.successes), len(provider.dispatches)
    monkeypatch.setattr(cli, "_make_provider", _forbid)
    monkeypatch.setattr(cli.CompileStoryPortfolioCommand, "execute", _forbid)
    assert cli.main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "dry_run_prepared"
    assert report["provider_calls"] == 0
    assert report["provider_payload_byte_count"] > 0
    assert report["request_hash"].startswith("sha256:")
    assert (len(store.attempts), len(store.successes), len(provider.dispatches)) == before


def test_execute_uses_native_command_once_and_reports_receipt(case, capsys):
    store, provider, _, args = case
    assert cli.main([*args, "--execute"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "succeeded"
    assert report["receipt_id"] is not None
    assert report["artifact_set_id"] is not None
    assert len(provider.dispatches) == 1
    assert len(store.successes) == 1
    assert "failure_detail_json" not in report


def test_unknown_result_returns_without_second_dispatch_or_automatic_loop(case, capsys):
    store, provider, _, args = case
    provider.dispatch_results = [ProviderIndeterminate("OUTCOME_UNKNOWN")]
    assert cli.main([*args, "--execute"]) == 3
    report = json.loads(capsys.readouterr().out)
    assert report["status"] in ("pending", "running")
    assert report["attempt_state"] == "indeterminate"
    assert len(provider.dispatches) == 1
    assert not store.successes


def test_execute_rejects_larger_budget_before_store_or_provider_creation(tmp_path, monkeypatch, capsys):
    _, _, request, _ = command_case(max_attempts=2, backoff=(0,))
    path = tmp_path / "request.json"
    path.write_bytes(canonical_json_bytes(request.to_mapping()))
    monkeypatch.setattr(cli, "_make_store", _forbid)
    monkeypatch.setattr(cli, "_make_provider", _forbid)
    assert cli.main(["--request", str(path), "--debug-root", str(tmp_path / "stage2"), "--execute"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["phase"] == "execution_budget"
    assert report["exception_kind"] == "Stage2RequestCliError"


@pytest.mark.parametrize("raw", [b'{"secret":"one","secret":"two"}', b'{"secret":',
                                 b'[' * 65 + b']' * 65])
def test_request_decoder_rejects_duplicate_malformed_and_deep_json(tmp_path, raw, capsys):
    path = tmp_path / "private-request.json"
    path.write_bytes(raw)
    assert cli.main(["--request", str(path), "--debug-root", str(tmp_path / "stage2")]) == 2
    output = capsys.readouterr().out
    assert "secret" not in output
    assert json.loads(output)["phase"] == "request_decode"


def test_request_file_is_bounded_before_parsing(tmp_path, monkeypatch, capsys):
    path = tmp_path / "request.json"
    path.write_bytes(b" " * 9)
    monkeypatch.setattr(cli, "MAX_REQUEST_FILE_BYTES", 8)
    assert cli.main(["--request", str(path), "--debug-root", str(tmp_path / "stage2")]) == 2
    assert json.loads(capsys.readouterr().out)["exception_kind"] == "Stage2RequestCliError"


def test_debug_invocations_preserve_previous_files(tmp_path):
    root = tmp_path / "existing-stage-directory"
    first, first_id = cli._new_debug_sink(root)
    sentinel = first.root / "previous-result.json"
    sentinel.write_text("original", encoding="utf-8")
    second, second_id = cli._new_debug_sink(root)
    assert first_id != second_id
    assert first.root != second.root
    assert sentinel.read_text(encoding="utf-8") == "original"


def test_kernel_dsn_takes_precedence_without_connecting_until_store_use(monkeypatch):
    factories = []
    calls = []
    monkeypatch.setattr(cli, "PostgresRuntimeStore", lambda factory: factories.append(factory))
    monkeypatch.setattr(cli.psycopg, "connect", lambda dsn: calls.append(dsn))
    cli._make_store({cli.PIPELINE_KERNEL_POSTGRES_DSN_ENV: "kernel-private",
                     cli.PIPELINE_POSTGRES_DSN_ENV: "control-private"})
    assert calls == []
    factories[0]()
    assert calls == ["kernel-private"]


def test_provider_configuration_reuses_composition_defaults_and_frozen_request(monkeypatch):
    _, _, request, _ = command_case(max_attempts=1, backoff=())
    observed = []
    monkeypatch.setattr(cli, "DoubaoDraftProvider", lambda config, **kwargs: observed.append((config, kwargs)))
    defaults = {item.name: item.default for item in fields(DoubaoArkVlmProviderConfig)}
    sink = object()
    cli._make_provider({cli.PIPELINE_ARK_API_KEY_ENV: "private-api-key"}, request, sink)
    config, kwargs = observed[0]
    assert config.base_url == defaults["base_url"]
    assert config.timeout_seconds == defaults["timeout_seconds"]
    assert config.max_stream_bytes == request.draft_policy.max_response_bytes
    assert kwargs["max_request_bytes"] == request.max_prompt_bytes
    assert kwargs["adapter_strategy_version"] == request.generation.adapter_strategy_version
    assert kwargs["debug_sink"] is sink


def test_exception_messages_are_not_printed(case, monkeypatch, capsys):
    _, _, _, args = case

    def fail(_):
        raise RuntimeError("postgresql://private-user:private-password@host/private-db")

    monkeypatch.setattr(cli, "_make_store", fail)
    assert cli.main(args) == 2
    output = capsys.readouterr().out
    assert "private" not in output
    assert json.loads(output)["exception_kind"] == "RuntimeError"


def test_debug_sink_receives_safe_exception_without_original_text_or_chain(case, monkeypatch, capsys):
    _, _, _, args = case
    captured = []

    def fail(*args, **kwargs):
        raise RuntimeError("postgresql://private-user:private-password@host/private-db")

    monkeypatch.setattr(cli, "read_committed_story_design_inputs", fail)
    monkeypatch.setattr(cli.FileModelIoDebugSink, "capture_stage_error",
                        lambda self, context, error: captured.append(error))
    assert cli.main(args) == 2
    output = capsys.readouterr()
    assert "private" not in output.out + output.err
    assert json.loads(output.out)["exception_kind"] == "RuntimeError"
    assert len(captured) == 1
    assert isinstance(captured[0], cli.Stage2RequestCliError)
    assert str(captured[0]) == ""
    assert captured[0].__cause__ is None
    assert captured[0].__context__ is None
