#!/usr/bin/env python3
"""tools/reuse runner: the single entry point for open-source adoption experiments.

Usage:
    uv run python tools/reuse/run.py --spec <spec.json> --mode replay|live \
        --media-root <private-dir> [--resume] [--out-root <dir>] [--reference-root <dir>]

Contract: docs/open-source-adoption-phases/00-r0-experiment-foundation.md
This runner never writes the production Kernel DB and never becomes an
admission authority; its outputs live under artifacts/shadow/ only.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from tools.reuse.adapters import load_adapter
from tools.reuse.budget import BudgetLedger
from tools.reuse.fixtures import load_fixture_manifest, verify_fixture
from tools.reuse.metrics import metrics_document
from tools.reuse.models import (
    AttemptStatus,
    ErrorCode,
    ExperimentError,
    ExperimentSpec,
    ProviderResult,
    Usage,
    file_sha256,
    load_json_strict,
)
from tools.reuse.project_registry import find_reference_root, load_registry
from tools.reuse.providers import FakeProvider, RecordingIndex
from tools.reuse.sandbox import isolation_mode
from tools.reuse.storage import ExperimentStore, RunLock

# statuses for user-visible failures that are protocol/eval conditions, not crashes
NOT_EVALUATED_CODES = {
    ErrorCode.REPLAY_MISS,
    ErrorCode.FIXTURE_UNVERIFIED,
    ErrorCode.SOURCE_MISMATCH,
    ErrorCode.METRIC_INPUT_INCOMPLETE,
}


def resolve_repo_root() -> Path:
    """Resolve the repo root via git (works from any worktree), with a fallback."""
    try:
        result = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parent), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return Path(result.stdout.strip()).resolve()
    except OSError:
        pass
    return Path(__file__).resolve().parents[2]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--spec", type=Path, required=True, help="frozen ExperimentSpec/v1 JSON file")
    parser.add_argument("--mode", choices=("replay", "live"), required=True,
                        help="must equal spec.mode; no automatic mode switching")
    parser.add_argument("--media-root", type=Path, required=True,
                        help="private directory mapping fixture input relative paths")
    parser.add_argument("--out-root", type=Path, default=None,
                        help="artifacts root (default <repo>/artifacts/shadow/reuse)")
    parser.add_argument("--reference-root", type=Path, default=None,
                        help="reference-projects/ directory (default: upward search)")
    parser.add_argument("--resume", action="store_true",
                        help="resume the latest non-terminal attempt of this experiment")
    return parser.parse_args(argv)


def build_report(
    spec: ExperimentSpec,
    spec_hash: str,
    attempt_dir_name: str,
    projection_hash: str,
    metrics_hash: str,
    metrics_doc: dict[str, object],
    budget: dict[str, object] | None,
) -> str:
    lines = [
        f"# Reuse experiment report: {spec.experiment_id}",
        "",
        f"- attempt: `{attempt_dir_name}` (spec_hash `{spec_hash[:12]}…`)",
        f"- producer: `{spec.producer_id}@{spec.producer_commit}` adapter `{spec.adapter_version}`",
        f"- mode: `{spec.mode}`, variant: `{spec.variant}`",
        f"- metric policy: `{spec.metric_policy['policy_version']}/{spec.metric_policy['revision']}`",
        f"- projection sha256: `{projection_hash[:12]}…`, metrics sha256: `{metrics_hash[:12]}…`",
        "",
        "| metric | value | num | denom | missing_reason |",
        "|---|---|---|---|---|",
    ]
    for row in metrics_doc["results"]:  # type: ignore[index]
        lines.append(
            "| {metric} | {value} | {num} | {denom} | {missing_reason} |".format(
                **{k: ("—" if row.get(k) is None else row.get(k)) for k in
                   ("metric", "value", "num", "denom", "missing_reason")}
            )
        )
    lines += ["", f"- budget usage: `{json.dumps(budget, sort_keys=True)}`" if budget else "- budget: n/a (replay)", ""]
    lines.append(
        "> Scope: this is the runner protocol smoke. It says nothing about real producer "
        "quality until the corresponding open-source method is evaluated on frozen, "
        "video-verified labels."
    )
    return "\n".join(lines) + "\n"


def run(args: argparse.Namespace) -> int:
    spec_path = args.spec.resolve()
    spec = ExperimentSpec.from_dict(
        load_json_strict(spec_path.read_text(encoding="utf-8"), context="spec file")
    )
    if args.mode != spec.mode:
        raise ExperimentError(
            ErrorCode.INVALID_SPEC,
            f"--mode {args.mode} != spec.mode {spec.mode}; automatic switching is forbidden",
        )
    spec_hash = spec.spec_hash
    repo_root = resolve_repo_root()
    out_root = (args.out_root or repo_root / "artifacts" / "shadow" / "reuse").resolve()
    store = ExperimentStore(out_root)
    experiment_dir = store.experiment_dir(spec.experiment_id)

    registry = load_registry(repo_root / "tools" / "reuse" / "projects.json")
    entry = registry.require(spec.producer_id)
    reference_root = find_reference_root(args.reference_root, repo_root) if entry.role == "reference" else None
    entry.verify(spec.producer_commit, spec.adapter_version,
                 reference_root if reference_root is not None else repo_root)

    fixture_path = (repo_root / spec.fixture_ref).resolve()
    if repo_root not in fixture_path.parents:
        raise ExperimentError(ErrorCode.PATH_ESCAPE, f"fixture_ref escapes repo: {spec.fixture_ref}")
    if file_sha256(fixture_path) != spec.fixture_sha256:
        raise ExperimentError(
            ErrorCode.FIXTURE_UNVERIFIED, "spec.fixture_sha256 does not match the manifest file"
        )
    manifest = load_fixture_manifest(fixture_path)
    verify_fixture(spec.fixture_sha256, fixture_path, manifest, args.media_root.resolve())

    adapter = load_adapter(entry.adapter_module)
    adapter.validate(spec, manifest)
    if hasattr(adapter, "bind"):
        adapter.bind(spec, manifest)

    provider = FakeProvider() if spec.mode == "live" else None
    ledger = BudgetLedger(spec.budget) if spec.budget else None

    base_metadata = {
        "experiment_id": spec.experiment_id,
        "mode": spec.mode,
        "variant": spec.variant,
        "producer_id": spec.producer_id,
        "producer_commit": spec.producer_commit,
        "adapter_version": spec.adapter_version,
        "fixture_ref": spec.fixture_ref,
        "runner_commit": _runner_commit(repo_root),
        "isolation_mode": isolation_mode(),
        "isolation_passed": False,
    }
    # One writer per experiment: the lock covers spec freeze, attempt numbering
    # and recordings from here to the end of the run.
    with RunLock(experiment_dir):
        return _run_locked(args, store, experiment_dir, spec, spec_hash, manifest,
                           adapter, provider, ledger, base_metadata)


def _run_locked(
    args: argparse.Namespace,
    store: ExperimentStore,
    experiment_dir: Path,
    spec: ExperimentSpec,
    spec_hash: str,
    manifest: object,
    adapter: object,
    provider: FakeProvider | None,
    ledger: BudgetLedger | None,
    base_metadata: dict[str, object],
) -> int:
    store.ensure_spec(experiment_dir, spec_hash, spec.to_dict())
    attempt_dir, _metadata = store.reserve(experiment_dir, spec_hash, base_metadata, resume=args.resume)
    store.mark(attempt_dir, status=AttemptStatus.RUNNING)

    recordings = RecordingIndex(store.recordings_dir(spec.producer_id))

    try:
        # Rebuild adapter state (and, for live, the attempt-wide budget ledger)
        # from persisted call evidence so a resumed attempt never re-calls.
        completed = store.load_completed_calls(attempt_dir)
        for call_id in sorted(completed):
            entry_ = completed[call_id]
            if entry_["result"].get("status") == "done":
                adapter.accept_response(call_id, entry_["raw"])
                if ledger is not None:
                    usage = entry_["result"].get("usage", {})
                    ledger.charge(Usage(
                        input_tokens=int(usage.get("input_tokens", 0)),
                        output_tokens=int(usage.get("output_tokens", 0)),
                    ))

        for call in adapter.build_calls({"accepted_call_ids": tuple(sorted(completed_done(completed)))}):
            if ledger is not None:
                ledger.validate_concurrency_plan(1)  # sequential executor
            call_dir = attempt_dir / "calls" / call.call_id
            existing_result = None
            if call_dir.is_dir() and (call_dir / "result.json").is_file():
                existing_result = json.loads((call_dir / "result.json").read_text(encoding="utf-8"))
            if spec.mode == "replay":
                recording = recordings.get(call.request_hash)
                if recording is None:
                    raise ExperimentError(
                        ErrorCode.REPLAY_MISS,
                        f"no recorded response for request hash {call.request_hash} (call {call.call_id})",
                    )
                usage = Usage(
                    input_tokens=recording.get("usage", {}).get("input_tokens", 0),
                    output_tokens=recording.get("usage", {}).get("output_tokens", 0),
                )
                result = ProviderResult(
                    raw=recording["raw"],
                    provider_response_id=recording.get("provider_response_id"),
                    usage=usage,
                    status="done",
                )
            else:
                assert provider is not None and ledger is not None
                if existing_result and existing_result.get("status") == "unknown":
                    # Recover the unknown call by polling its recorded response id — never re-call.
                    ledger.check_can_call()
                    response_id = existing_result.get("provider_response_id")
                    if not isinstance(response_id, str) or not response_id:
                        raise ExperimentError(
                            ErrorCode.PROVIDER_RESULT_UNKNOWN,
                            f"call {call.call_id} has no provider response id recorded; cannot poll",
                        )
                    result = provider.resolve_unknown(call.call_id, response_id)
                else:
                    ledger.check_can_call()
                    result = provider.call(call)
                if result.status == "unknown":
                    store.save_call_request(attempt_dir, call)
                    store.save_call_result(attempt_dir, call, result)
                    # Keep the attempt non-terminal so the same attempt can be resumed.
                    store.mark(attempt_dir, status=AttemptStatus.RUNNING, extra={
                        "pending_call": call.call_id,
                        "provider_response_id": result.provider_response_id,
                        "note": "provider result unknown; resume with --resume to poll, never re-call",
                    })
                    print(f"PROVIDER_RESULT_UNKNOWN: call {call.call_id}; rerun with --resume", file=sys.stderr)
                    return 3
                # Evidence first, accounting second: a call that trips the budget
                # cap keeps its persisted raw response — paid tokens leave evidence.
                recordings.put_if_absent(call.request_hash, result.raw, result.provider_response_id, result.usage)
            store.save_call_request(attempt_dir, call)
            store.save_call_result(attempt_dir, call, result)
            if ledger is not None and spec.mode == "live":
                ledger.charge(result.usage)
            adapter.accept_response(call.call_id, result.raw)

        adapter.finalize()
        completed_calls = store.load_completed_calls(attempt_dir)
        raw_outputs = {call_id: entry["raw"] for call_id, entry in sorted(completed_calls.items())}
        projection = adapter.project(raw_outputs)
        projection_dict = projection.to_dict()
        projection_dict["raw_hashes"] = {
            call_id: entry["result"].get("raw_sha256") for call_id, entry in sorted(completed_calls.items())
        }
        projection_hash = store.write_projection(attempt_dir, projection_dict)
        metric_results = adapter.compute_metrics(projection, manifest)  # type: ignore[attr-defined]
        metrics_doc = metrics_document(spec.metric_policy, metric_results, ledger.to_dict() if ledger else None)
        metrics_hash = store.write_metrics(attempt_dir, metrics_doc)
        report = build_report(
            spec, spec_hash, attempt_dir.name, projection_hash, metrics_hash,
            metrics_doc, ledger.to_dict() if ledger else None,
        )
        report_hash = store.write_report(attempt_dir, report)
        store.finalize(attempt_dir, {
            "projection": projection_hash, "metrics": metrics_hash, "report": report_hash,
        })
        print(f"attempt succeeded: {attempt_dir}")
        return 0
    except ExperimentError as exc:
        status = AttemptStatus.NOT_EVALUATED if exc.code in NOT_EVALUATED_CODES else AttemptStatus.FAILED
        store.mark(attempt_dir, status=status, error_code=exc.code.value)
        print(f"{exc.code.value}: {exc.message}", file=sys.stderr)
        print(f"attempt dir: {attempt_dir} (status={status.value})", file=sys.stderr)
        return 1


def completed_done(completed: dict[str, dict[str, object]]) -> set[str]:
    return {
        call_id for call_id, entry in completed.items() if entry["result"].get("status") == "done"
    }


def _runner_commit(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    )
    return result.stdout.strip() or "unknown"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run(args)
    except ExperimentError as exc:
        print(f"{exc.code.value}: {exc.message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
