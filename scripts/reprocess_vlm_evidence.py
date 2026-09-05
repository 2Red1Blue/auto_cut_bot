"""Audit or execute deterministic VLM recovery from a frozen JSON request.

Requires the installed Kernel. Provider invocation is never part of this tool.
Database configuration is read only from AUTO_CUT_BOT_PIPELINE_KERNEL_POSTGRES_DSN
or AUTO_CUT_BOT_PIPELINE_POSTGRES_DSN. Neither connection strings nor raw responses are printed.

Examples:
    python scripts/reprocess_vlm_evidence.py --mode reprocess --request request.json
    python scripts/reprocess_vlm_evidence.py --mode reprocess --request request.json --execute
    python scripts/reprocess_vlm_evidence.py --mode finalize-batch --request batch.json --execute

The default is read-only validation; --execute explicitly permits a new derived
Command/Receipt. Exit codes: 0 validated/succeeded; 1 durable rejection or parser
rejection; 2 invalid input or unavailable configuration/implementation;
3 nonterminal, persistence, unexpected execution, or post-commit reporting failure.
A succeeded_reporting_incomplete result preserves the succeeded Receipt: inspect
that exact Receipt without changing the request or creating another request key.
This is a local operational CLI,
not a declaration that a public Pipeline recovery endpoint has been deployed.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from autocut_kernel.pipeline.reprocess_vlm_batch_command import (
    DerivedVlmBatchStore,
    FinalizeDerivedVlmBatchCommand,
    FinalizeDerivedVlmBatchRequest,
    rebuild_derived_vlm_batch,
)
from autocut_kernel.pipeline.reprocess_vlm_evidence_command import (
    ReprocessVlmEvidenceCommand,
    ReprocessVlmEvidenceRequest,
    rebuild_reprocessed_vlm_evidence,
)
from autocut_kernel.store.models import (
    ArtifactMember,
    CommandOutcome,
    CommittedArtifactMemberReference,
)
from autocut_kernel.vlm.normalized_contracts import ParserImplementationUnavailableError
from autocut_kernel.vlm.parser import VlmResponseIndeterminate, VlmResponseRejected

MAX_REQUEST_BYTES = 1024 * 1024
MAX_REQUEST_DEPTH = 32


class RecoveryInputError(ValueError):
    """The recovery selector cannot be interpreted exactly."""


class RecoveryConfigurationError(ValueError):
    """The explicitly configured Store is unavailable."""


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # argparse normally echoes invalid arguments, which might contain a DSN.
        raise RecoveryInputError("invalid recovery arguments")


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RecoveryInputError("duplicate request JSON key")
        result[key] = value
    return result


def _noninteger(_value: str) -> object:
    raise RecoveryInputError("recovery selectors require integer JSON numbers")


def _load_request(path: Path) -> object:
    with path.open("rb") as stream:
        raw = stream.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        raise RecoveryInputError("request exceeds the read bound")
    value = json.loads(raw.decode("utf-8", "strict"), object_pairs_hook=_pairs,
                       parse_float=_noninteger, parse_constant=_noninteger)
    if type(value) is not dict:
        raise RecoveryInputError("request must be a JSON object")
    pending = [(value, 0)]
    while pending:
        current, depth = pending.pop()
        if depth > MAX_REQUEST_DEPTH:
            raise RecoveryInputError("request exceeds the depth bound")
        if type(current) is dict:
            pending.extend((member, depth + 1) for member in current.values())
        elif type(current) is list:
            pending.extend((member, depth + 1) for member in current)
        elif type(current) is str:
            current.encode("utf-8", "strict")
    return value


def _configured_store() -> DerivedVlmBatchStore:
    dsn = os.environ.get("AUTO_CUT_BOT_PIPELINE_KERNEL_POSTGRES_DSN") or os.environ.get("AUTO_CUT_BOT_PIPELINE_POSTGRES_DSN")
    if not dsn or not dsn.strip():
        raise RecoveryConfigurationError("a Kernel PostgreSQL DSN must be configured in the environment")
    import psycopg
    from autocut_kernel.store.postgres import PostgresRuntimeStore

    return PostgresRuntimeStore(lambda: psycopg.connect(dsn))


def _artifact_identity(artifact: ArtifactMember) -> dict[str, object]:
    return {"artifact_type": artifact.artifact_type, "logical_id": artifact.logical_id,
            "revision": artifact.revision, "content_hash": artifact.content_hash,
            "scope": {"namespace": artifact.scope.namespace, "kind": artifact.scope.kind, "key": artifact.scope.key}}


def _member_refs(outcome: CommandOutcome, artifacts: tuple[ArtifactMember, ...]) -> list[dict[str, object]]:
    if outcome.state != "succeeded" or outcome.receipt_id is None or outcome.artifact_set_id is None:
        raise RecoveryInputError("exact member references require a succeeded Receipt")
    return [CommittedArtifactMemberReference(
        outcome.receipt_id, outcome.artifact_set_id, ordinal, artifact.scope, artifact.artifact_type,
        artifact.logical_id, artifact.revision, artifact.content_hash,
    ).to_mapping() for ordinal, artifact in enumerate(artifacts)]


def _outcome_report(outcome: CommandOutcome) -> dict[str, object]:
    return {"status": outcome.state, "command_slot_id": str(outcome.command_slot_id),
            "receipt_id": str(outcome.receipt_id) if outcome.receipt_id is not None else None,
            "artifact_set_id": str(outcome.artifact_set_id) if outcome.artifact_set_id is not None else None,
            "error_code": outcome.failure_code}


def main(argv: list[str] | None = None, *, store: DerivedVlmBatchStore | None = None) -> int:
    parser = _ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, type=Path, help="frozen request JSON file")
    parser.add_argument("--mode", required=True, choices=("reprocess", "finalize-batch"))
    activity = parser.add_mutually_exclusive_group()
    activity.add_argument("--execute", action="store_true", help="commit the explicitly selected derived result")
    activity.add_argument("--dry-run", action="store_true", help="validate only (the default)")
    report: dict[str, object] = {"provider_calls": 0, "execution_requested": False}
    phase = "arguments"
    committed_outcome: CommandOutcome | None = None
    try:
        args = parser.parse_args(argv)
        report.update({"mode": args.mode, "execution_requested": args.execute})
        phase = "request"
        value = _load_request(args.request)
        request = (ReprocessVlmEvidenceRequest.from_mapping(value) if args.mode == "reprocess"
                   else FinalizeDerivedVlmBatchRequest.from_mapping(value))
        report.update({"request_hash": request.request_hash, "idempotency_key": request.idempotency_key})
        phase = "configuration"
        bound_store = _configured_store() if store is None else store
        phase = "execute" if args.execute else "validate"
        if isinstance(request, ReprocessVlmEvidenceRequest):
            if args.execute:
                result = ReprocessVlmEvidenceCommand(bound_store).execute(request)
                report.update(_outcome_report(result.outcome))
                if result.outcome.state != "succeeded":
                    print(json.dumps(report, sort_keys=True))
                    return 1 if result.outcome.state in ("denied", "failed") else 3
                committed_outcome = result.outcome
                phase = "report_committed_result"
                if result.evidence is None:
                    raise RecoveryInputError("succeeded reprocess did not expose its audited evidence")
                artifacts = result.evidence.artifacts
                report["members"] = _member_refs(result.outcome, artifacts)
            else:
                evidence = rebuild_reprocessed_vlm_evidence(bound_store, request)
                artifacts = evidence.artifacts
                report.update({"status": "validated", "transformed_paths": [item.path for item in evidence.normalization.transformations]})
        else:
            if args.execute:
                outcome = FinalizeDerivedVlmBatchCommand(bound_store).execute(request)
                report.update(_outcome_report(outcome))
                if outcome.state != "succeeded":
                    print(json.dumps(report, sort_keys=True))
                    return 1 if outcome.state in ("denied", "failed") else 3
                committed_outcome = outcome
                phase = "report_committed_result"
                artifact, *_ = rebuild_derived_vlm_batch(bound_store, request)
                artifacts = (artifact,)
                report["members"] = _member_refs(outcome, artifacts)
            else:
                artifact, *_ = rebuild_derived_vlm_batch(bound_store, request)
                artifacts = (artifact,)
                report["status"] = "validated"
        report["artifacts"] = [_artifact_identity(artifact) for artifact in artifacts]
        exit_code = 0
    except (VlmResponseRejected, VlmResponseIndeterminate) as error:
        report.update({"status": "rejected", "phase": phase, "error_code": error.code})
        exit_code = 1
    except ParserImplementationUnavailableError:
        report.update({"status": "unavailable", "phase": phase, "error_code": "PARSER_IMPLEMENTATION_UNAVAILABLE"})
        exit_code = 2
    except (RecoveryInputError, RecoveryConfigurationError, OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError) as error:
        report.update({"status": "invalid_input" if phase in ("arguments", "request") else "unavailable",
                       "phase": phase, "error_code": "RECOVERY_INPUT_UNAVAILABLE", "exception_kind": type(error).__name__})
        exit_code = 2
    except Exception as error:
        # Database exceptions can contain credentials or signed URLs. Never print them.
        report.update({"status": "failed", "phase": phase, "error_code": "RECOVERY_EXECUTION_FAILED",
                       "exception_kind": type(error).__name__})
        exit_code = 3
    if committed_outcome is not None and exit_code != 0:
        # Reporting must not overwrite a durable success with a failed command state.
        report.update(_outcome_report(committed_outcome))
        report.update({"status": "succeeded_reporting_incomplete", "command_state": "succeeded",
                       "error_code": "POST_COMMIT_REPORT_UNAVAILABLE", "members": [], "artifacts": [],
                       "next_action": "inspect_existing_receipt_without_changing_request"})
        exit_code = 3
    print(json.dumps(report, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
