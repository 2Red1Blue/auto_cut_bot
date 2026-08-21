# pyright: reportMissingModuleSource=false
"""Command-line entry point for the Phase -1 authority gates."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from .aggregate_gate import verify_change, verify_push
from .common import canonical_hash, load_mapping, write_json_atomic
from .errors import GateViolation
from .lock import build_authority_lock, verify_authority_lock
from .receipts import make_typed_receipt
from .remote_gate import verify_remote_protection
from .source_candidate_gate import verify_pre_a_source_candidate
from .task_gate import admit_task, check_change_scopes
from .trellis_sync import check_trellis_drift, sync_trellis_authority


def _bindings(values: list[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        if "=" not in value:
            raise GateViolation("AUTH-CLI-BINDING", "repository roots use NAME=PATH")
        name, raw_path = value.split("=", 1)
        if not name or not raw_path or name in result:
            raise GateViolation("AUTH-CLI-BINDING", f"invalid repository binding: {value}")
        result[name] = Path(raw_path)
    return result


def _string_bindings(values: list[str]) -> dict[str, str]:
    return {name: str(path) for name, path in _bindings(values).items()}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m tools.authority.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    lock = subparsers.add_parser("verify-lock")
    lock.add_argument("--lock", type=Path, required=True)
    lock.add_argument("--repository-root", action="append", default=[])

    build = subparsers.add_parser("build-lock")
    build.add_argument("--source-manifest-repository", required=True)
    build.add_argument("--source-manifest-commit", required=True)
    build.add_argument("--source-manifest-path", required=True)
    build.add_argument("--repository-root", action="append", default=[])
    build.add_argument("--output", type=Path, required=True)

    admit = subparsers.add_parser("admit-task")
    admit.add_argument("--manifest", type=Path, required=True)
    admit.add_argument("--lock", type=Path, required=True)
    admit.add_argument("--model-policy", type=Path, required=True)
    admit.add_argument("--protected-paths", type=Path, required=True)
    admit.add_argument("--repository-root", action="append", default=[])
    admit.add_argument("--receipt", type=Path)

    scope = subparsers.add_parser("check-scope")
    scope.add_argument("--manifest", type=Path, required=True)
    scope.add_argument("--lock", type=Path, required=True)
    scope.add_argument("--protected-paths", type=Path, required=True)
    scope.add_argument("--repository-root", action="append", default=[])

    for name in ("sync-trellis", "check-trellis-drift"):
        command = subparsers.add_parser(name)
        command.add_argument("--source-root", type=Path, required=True)
        command.add_argument("--destination-root", type=Path, required=True)
        command.add_argument("--manifest", type=Path, required=True)

    remote = subparsers.add_parser("verify-remote")
    remote.add_argument("--attestation", type=Path, required=True)
    remote.add_argument("--policy", type=Path, required=True)
    remote.add_argument("--repository-root", type=Path, required=True)
    remote.add_argument("--candidate-commit", required=True)

    pre_a = subparsers.add_parser("verify-source-candidate")
    pre_a.add_argument("--repository-root", type=Path, required=True)
    pre_a.add_argument("--predecessor-commit", required=True)
    pre_a.add_argument("--synthetic-fixture-manifest", required=True)
    pre_a.add_argument("--output", type=Path)

    change = subparsers.add_parser("verify-change")
    change.add_argument("--manifest", type=Path, required=True)
    change.add_argument("--lock", type=Path, required=True)
    change.add_argument("--model-policy", type=Path, required=True)
    change.add_argument("--protected-paths", type=Path, required=True)
    change.add_argument("--repository-root", action="append", default=[])
    change.add_argument("--registry-path", action="append", default=[])
    change.add_argument("--reuse-ledger", required=True)
    change.add_argument("--checker-collector", action="append", default=[])
    change.add_argument(
        "--scan-profile", choices=("production", "test_fixture"), default="production"
    )
    change.add_argument("--synthetic-fixture-manifest", type=Path)
    change.add_argument("--output", type=Path)

    push = subparsers.add_parser("verify-push")
    push.add_argument("--repository", required=True)
    push.add_argument("--repository-root", action="append", default=[])
    push.add_argument("--task-id", required=True)
    push.add_argument("--lock", type=Path, required=True)
    push.add_argument("--change-bundle", type=Path, required=True)
    push.add_argument("--candidate-commit", required=True)
    push.add_argument("--remote-attestation", type=Path, required=True)
    push.add_argument("--remote-policy", type=Path, required=True)
    push.add_argument(
        "--scan-profile", choices=("production", "test_fixture"), default="production"
    )
    push.add_argument("--synthetic-fixture-manifest", type=Path)
    push.add_argument("--output", type=Path)
    return parser


def _authority_hash(lock_path: Path | None) -> str:
    if lock_path is None:
        return "sha256:" + "0" * 64
    try:
        return str(load_mapping(lock_path).get("bundle_hash", "sha256:" + "0" * 64))
    except GateViolation:
        return "sha256:" + "0" * 64


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    receipt_path = getattr(args, "receipt", None)
    lock_path = getattr(args, "lock", None)
    task_id: str | None = None
    try:
        if args.command == "verify-lock":
            verified = verify_authority_lock(args.lock, _bindings(args.repository_root))
            print(json.dumps(verified, sort_keys=True))
        elif args.command == "build-lock":
            lock = build_authority_lock(
                source_manifest_repository=args.source_manifest_repository,
                source_manifest_commit=args.source_manifest_commit,
                source_manifest_path=args.source_manifest_path,
                repository_roots=_bindings(args.repository_root),
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                yaml.safe_dump(lock, sort_keys=False, allow_unicode=True), encoding="utf-8"
            )
            print(lock["bundle_hash"])
        elif args.command == "admit-task":
            task_id = str(load_mapping(args.manifest).get("task_id"))
            admit_task(
                manifest_path=args.manifest,
                authority_lock_path=args.lock,
                model_policy_path=args.model_policy,
                protected_paths_path=args.protected_paths,
                repository_roots=_bindings(args.repository_root),
            )
            if receipt_path:
                write_json_atomic(
                    receipt_path,
                    make_typed_receipt(
                        "task_admission",
                        authority_lock_hash=_authority_hash(lock_path),
                        decision="allow",
                        reason_codes=[],
                        task_id=task_id,
                        context_hash=canonical_hash(load_mapping(args.manifest)),
                        repository_heads_hash=canonical_hash(
                            {
                                name: str(path.resolve())
                                for name, path in _bindings(args.repository_root).items()
                            }
                        ),
                        authorization_id=(
                            "locked-task-authorization"
                            if load_mapping(args.manifest).get("task_type") == "authority_change"
                            else None
                        ),
                    ),
                )
        elif args.command == "check-scope":
            verify_authority_lock(args.lock, _bindings(args.repository_root))
            receipt = check_change_scopes(
                manifest_path=args.manifest,
                protected_paths_path=args.protected_paths,
                repository_roots=_bindings(args.repository_root),
                authority_lock_hash=_authority_hash(args.lock),
            )
            print(json.dumps(receipt, sort_keys=True))
        elif args.command == "sync-trellis":
            sync_trellis_authority(
                source_root=args.source_root,
                destination_root=args.destination_root,
                manifest_path=args.manifest,
            )
        elif args.command == "check-trellis-drift":
            check_trellis_drift(
                source_root=args.source_root,
                destination_root=args.destination_root,
                manifest_path=args.manifest,
            )
        elif args.command == "verify-remote":
            verify_remote_protection(
                attestation_path=args.attestation,
                policy_path=args.policy,
                repository_root=args.repository_root,
                candidate_commit=args.candidate_commit,
            )
        elif args.command == "verify-source-candidate":
            result = verify_pre_a_source_candidate(
                root=args.repository_root,
                predecessor_commit=args.predecessor_commit,
                synthetic_fixture_manifest_path=args.synthetic_fixture_manifest,
            )
            if args.output:
                write_json_atomic(args.output, result)
            else:
                print(json.dumps(result, sort_keys=True))
        elif args.command == "verify-change":
            result = verify_change(
                task_manifest_path=args.manifest,
                authority_lock_path=args.lock,
                model_policy_path=args.model_policy,
                protected_paths_path=args.protected_paths,
                repository_roots=_bindings(args.repository_root),
                registry_paths=args.registry_path,
                reuse_ledger_path=args.reuse_ledger,
                checker_collector_ids=_string_bindings(args.checker_collector),
                scan_profile=args.scan_profile,
                synthetic_fixture_manifest_path=args.synthetic_fixture_manifest,
            )
            if args.output:
                write_json_atomic(args.output, result)
            else:
                print(json.dumps(result, sort_keys=True))
        elif args.command == "verify-push":
            roots = _bindings(args.repository_root)
            if args.repository not in roots:
                raise GateViolation("AUTH-CLI-BINDING", "push repository root is missing")
            result = verify_push(
                root=roots[args.repository],
                repository=args.repository,
                task_id=args.task_id,
                authority_lock_path=args.lock,
                repository_roots=roots,
                change_bundle=load_mapping(args.change_bundle),
                candidate_commit=args.candidate_commit,
                remote_attestation_path=args.remote_attestation,
                remote_policy_path=args.remote_policy,
                scan_profile=args.scan_profile,
                synthetic_fixture_manifest_path=args.synthetic_fixture_manifest,
            )
            if args.output:
                write_json_atomic(args.output, result)
            else:
                print(json.dumps(result, sort_keys=True))
        else:  # pragma: no cover - argparse owns command exhaustiveness
            raise GateViolation("AUTH-CLI-COMMAND", "unknown command")
    except GateViolation as exc:
        if receipt_path:
            write_json_atomic(
                receipt_path,
                make_typed_receipt(
                    "task_admission",
                    authority_lock_hash=_authority_hash(lock_path),
                    decision="deny",
                    reason_codes=[exc.code],
                    task_id=task_id or "unknown-task",
                    context_hash="sha256:" + "0" * 64,
                    repository_heads_hash="sha256:" + "0" * 64,
                    authorization_id=None,
                ),
            )
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
