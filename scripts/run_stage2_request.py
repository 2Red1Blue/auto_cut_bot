"""Prepare or execute one exact Stage 2 request against committed Stage 1 inputs.

Default --dry-run reads predecessors and prepares bytes without provider I/O.
--execute invokes the native Command once and requires max_attempts=1. It does
not create a Pipeline run or rerun upstream stages. Pending/unknown outcomes
remain persisted for explicit reconciliation; this CLI has no retry loop.

--debug-root is an absolute stage directory. Each invocation gets a new private
subdirectory so prior diagnostic files are preserved. Debug files are mirrors,
not authoritative receipts. Exit: 0 prepared/succeeded, 1 denied/failed,
2 input/configuration/execution exception, 3 pending/running/unknown.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping
from dataclasses import fields
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
from autocut_kernel.contracts.compiler.canonical import sha256_bytes
from autocut_kernel.pipeline.compile_story_portfolio_command import (
    CompileStoryPortfolioCommand,
    CompileStoryPortfolioResult,
)
from autocut_kernel.pipeline.compile_story_portfolio_request import (
    CompileStoryPortfolioRequest,
    prepare_stage2_request,
)
from autocut_kernel.pipeline.story_design_inputs import read_committed_story_design_inputs
from autocut_kernel.semantic_chain.story_design_draft import (
    _LIMIT_CEILINGS,
    StoryDesignDraftPolicy,
    _bounded_value,
)
from autocut_kernel.store import PostgresRuntimeStore

from auto_cut_bot.pipeline.debug import FileModelIoDebugSink, PipelineStageDebugContext
from auto_cut_bot.pipeline.runtime.composition import (
    PIPELINE_ARK_API_KEY_ENV,
    PIPELINE_ARK_BASE_URL_ENV,
    PIPELINE_KERNEL_POSTGRES_DSN_ENV,
    PIPELINE_POSTGRES_DSN_ENV,
)
from auto_cut_bot.pipeline.vlm.ark_responses_transport import ArkResponsesTransportConfig
from auto_cut_bot.pipeline.vlm.doubao_ark_provider import DoubaoArkVlmProviderConfig
from auto_cut_bot.pipeline.vlm.doubao_draft_provider import DoubaoDraftProvider

_INPUT_LIMITS = StoryDesignDraftPolicy(**_LIMIT_CEILINGS)
MAX_REQUEST_FILE_BYTES = _INPUT_LIMITS.max_response_bytes
_ARK_DEFAULTS = {item.name: item.default for item in fields(DoubaoArkVlmProviderConfig)}


class Stage2RequestCliError(ValueError):
    """An explicit CLI precondition failed; details must not expose input text."""


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # argparse otherwise echoes invalid arguments, including pasted secrets.
        raise Stage2RequestCliError


def _load_request(path: Path) -> tuple[CompileStoryPortfolioRequest, str]:
    with path.open("rb") as stream:
        raw = stream.read(MAX_REQUEST_FILE_BYTES + 1)
    if not raw or len(raw) > MAX_REQUEST_FILE_BYTES:
        raise Stage2RequestCliError
    value = _bounded_value(raw, _INPUT_LIMITS)
    return CompileStoryPortfolioRequest.from_mapping(value), sha256_bytes(raw)


def _make_store(values: Mapping[str, str]) -> PostgresRuntimeStore:
    dsn = (values.get(PIPELINE_KERNEL_POSTGRES_DSN_ENV, "").strip()
           or values.get(PIPELINE_POSTGRES_DSN_ENV, "").strip())
    if not dsn:
        raise Stage2RequestCliError
    return PostgresRuntimeStore(lambda: psycopg.connect(dsn))


def _make_provider(
    values: Mapping[str, str], request: CompileStoryPortfolioRequest,
    sink: FileModelIoDebugSink,
) -> DoubaoDraftProvider:
    api_key = values.get(PIPELINE_ARK_API_KEY_ENV, "").strip()
    if not api_key:
        raise Stage2RequestCliError
    base_url = (values.get(PIPELINE_ARK_BASE_URL_ENV, "").strip()
                or _ARK_DEFAULTS["base_url"])
    return DoubaoDraftProvider(
        ArkResponsesTransportConfig(api_key, base_url, _ARK_DEFAULTS["timeout_seconds"],
                                    request.draft_policy.max_response_bytes),
        max_request_bytes=request.max_prompt_bytes,
        adapter_strategy_version=request.generation.adapter_strategy_version,
        debug_sink=sink,
    )


def _new_debug_sink(root: Path) -> tuple[FileModelIoDebugSink, str]:
    if not root.is_absolute():
        raise Stage2RequestCliError
    root.mkdir(parents=True, exist_ok=True)
    invocation_id = str(uuid4())
    invocation = root / f"invocation-{invocation_id}"
    invocation.mkdir(mode=0o700, exist_ok=False)
    return FileModelIoDebugSink(invocation), invocation_id


def _uuid(value: UUID | None) -> str | None:
    return str(value) if type(value) is UUID else None  # noqa: E721


def _outcome_report(result: CompileStoryPortfolioResult) -> tuple[dict[str, object], int]:
    outcome, attempt = result.outcome, result.attempt
    state = outcome.state
    if state not in ("pending", "running", "succeeded", "denied", "failed"):
        raise Stage2RequestCliError
    report: dict[str, object] = {
        "status": state, "command_slot_id": _uuid(outcome.command_slot_id),
        "receipt_id": _uuid(outcome.receipt_id), "artifact_set_id": _uuid(outcome.artifact_set_id),
        "job_id": _uuid(outcome.job_id), "attempt_id": None, "attempt_state": None,
    }
    if attempt is not None:
        report["attempt_id"] = _uuid(attempt.attempt_id)
        report["attempt_state"] = attempt.state if attempt.state in (
            "reserved", "dispatched", "responded", "indeterminate", "reconciled", "committed", "failed",
        ) else "unknown"
    return report, 0 if state == "succeeded" else 1 if state in ("denied", "failed") else 3


def main(argv: list[str] | None = None) -> int:
    parser = _ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--debug-root", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--execute", action="store_true")
    report: dict[str, object] = {"scope": "stage2_only"}
    phase = "arguments"
    sink = context = None
    try:
        args = parser.parse_args(argv)
        report["mode"] = "execute" if args.execute else "dry_run"
        phase = "request_decode"
        request, file_hash = _load_request(args.request)
        report["request_file_sha256"] = file_hash
        phase = "execution_budget"
        if args.execute and request.retry_policy.max_attempts != 1:
            raise Stage2RequestCliError
        phase = "configuration"
        store = _make_store(os.environ)
        sink, invocation_id = _new_debug_sink(args.debug_root)
        report["debug_invocation_id"] = invocation_id
        context = PipelineStageDebugContext(
            request.job.job_key, "stage2_portfolio", str(request.stage1_outcome.command_slot_id),
            request.artifact_revision, "execute" if args.execute else "dry_run",
        )
        with sink.stage_scope(context):
            sink.capture_stage_input(context, value=request.to_mapping())
            if args.execute:
                provider = _make_provider(os.environ, request, sink)
                phase = "command_execute"
                result = CompileStoryPortfolioCommand(store, provider).execute(request)
                outcome, exit_code = _outcome_report(result)
                report.update(outcome)
            else:
                phase = "predecessor_read"
                inputs = read_committed_story_design_inputs(
                    store, stage1_request=request.stage1_request, stage1_outcome=request.stage1_outcome,
                )
                phase = "request_prepare"
                prepared = prepare_stage2_request(request, inputs)
                report.update({
                    "status": "dry_run_prepared", "provider_calls": 0,
                    "request_hash": prepared.request_hash,
                    "provider_payload_sha256": sha256_bytes(prepared.provider_payload),
                    "provider_payload_byte_count": len(prepared.provider_payload),
                    "request_payload_byte_count": len(prepared.request_payload),
                    "max_attempts": request.retry_policy.max_attempts,
                })
                exit_code = 0
            sink.capture_stage_output(context, value=report)
    except Exception as error:
        # Database/provider errors may embed DSNs, credentials or raw model text.
        report.update({"status": "error", "phase": phase,
                       "error_code": "STAGE2_REQUEST_CLI_REJECTED",
                       "exception_kind": type(error).__name__})
        if sink is not None and context is not None:
            # The current file sink keeps only the type, but do not give any
            # sink the original exception or a chain that can contain secrets.
            sink.capture_stage_error(context, Stage2RequestCliError())
            sink.capture_stage_output(context, value=report)
        exit_code = 2
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
