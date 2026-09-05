"""Inspect saved Stage 2 context/raw bytes without provider or database I/O.

Run with the Kernel installed (or PYTHONPATH=packages/autocut-kernel/src):
    python scripts/diagnose_stage2_debug.py --context context.json --response raw.json

Exit codes: 0 = proposal rules passed; 1 = proposal rules rejected;
2 = malformed, unsupported, oversized or unreadable diagnostic input.
This is not Store replay, Admission, a Receipt, or persisted stage success.
The saved context does not prove its authority or the full frozen draft policy.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import cast

from autocut_kernel.contracts.compiler.canonical import sha256_bytes
from autocut_kernel.semantic_chain.member_refs import SemanticMemberIdentity, SemanticObjectRef
from autocut_kernel.semantic_chain.narrative_models import NarrativeGraph
from autocut_kernel.semantic_chain.story_design_draft import (
    _LIMIT_CEILINGS,
    ProposalDraftSet,
    StoryDesignDraftPolicy,
    _bounded_value,
    _check_limits,
)
from autocut_kernel.semantic_chain.story_design_models import JobPolicy, StoryDesignPolicy
from autocut_kernel.semantic_chain.story_design_validation import (
    StoryProposalValidationError,
    validate_story_proposals,
)

# Reuse the decoder's implementation ceilings, not guessed deployment limits.
OFFLINE_LIMITS = StoryDesignDraftPolicy(**_LIMIT_CEILINGS)
MAX_FILE_BYTES = OFFLINE_LIMITS.max_response_bytes
_CONTEXT_KEYS = {
    "schema_version", "input_binding_sha256", "stage1_members", "source_grant",
    "candidate_catalog", "policies", "episode_digest_set", "event_card_set", "narrative_graph",
}


class UnsupportedDiagnosticInput(ValueError):
    """The saved context cannot supply unambiguous proposal-rule inputs."""


def _mapping(value: object) -> dict[str, object]:
    if type(value) is not dict:  # noqa: E721
        raise UnsupportedDiagnosticInput
    return cast(dict[str, object], value)


def _array(value: object) -> list[object]:
    if type(value) is not list:  # noqa: E721
        raise UnsupportedDiagnosticInput
    return cast(list[object], value)


def _context_inputs(value: object) -> tuple[
    str, NarrativeGraph, tuple[SemanticObjectRef, ...], tuple[SemanticObjectRef, ...],
    JobPolicy, StoryDesignPolicy,
]:
    context = _mapping(value)
    if (set(context) != _CONTEXT_KEYS
            or context["schema_version"] != "stage2-proposal-context-v1"):
        raise UnsupportedDiagnosticInput
    graph_data = _mapping(context["narrative_graph"])
    graph = NarrativeGraph.from_mapping(graph_data["payload"])
    graph_owner = SemanticMemberIdentity.from_mapping(graph_data["member_ref"])
    graph_refs = tuple(SemanticObjectRef(graph_owner, node.node_type, node.node_id)
                       for node in graph.nodes)
    candidates = _array(_mapping(_mapping(context["candidate_catalog"])["payload"])["candidates"])
    candidate_sources = tuple(SemanticObjectRef.from_mapping(_mapping(item)["source_ref"])
                              for item in candidates)
    owners = {ref.member_ref for ref in candidate_sources}
    if (len(owners) != 1 or any(ref.object_type != "source" for ref in candidate_sources)
            or any(owner.artifact_type != "whole_series_source_manifest"
                   or owner.scope != graph_owner.scope for owner in owners)):
        raise UnsupportedDiagnosticInput
    source_owner = next(iter(owners))
    grant = _mapping(context["source_grant"])
    source_ids = tuple(_mapping(item)["source_id"] for item in _array(grant["sources"]))
    if (not source_ids or any(type(item) is not str or not item.strip() for item in source_ids)
            or len(set(source_ids)) != len(source_ids)
            or not {ref.object_id for ref in candidate_sources} <= set(source_ids)):
        raise UnsupportedDiagnosticInput
    source_refs = tuple(SemanticObjectRef(source_owner, "source", cast(str, item))
                        for item in source_ids)
    policies = _mapping(context["policies"])
    job = JobPolicy.from_mapping(policies["job_policy"])
    story = StoryDesignPolicy.from_mapping(policies["story_policy"])
    binding = context["input_binding_sha256"]
    if type(binding) is not str:
        raise UnsupportedDiagnosticInput
    return binding, graph, graph_refs, source_refs, job, story


def _report() -> dict[str, object]:
    return {
        "scope": "offline_structure_and_proposal_rules_only", "authority_verified": False,
        "full_policy_and_resource_verification": False, "provider_calls": 0,
        "context_sha256": None, "raw_sha256": None,
        "context_byte_count": None, "raw_byte_count": None,
    }


def diagnose(context_raw: bytes, response_raw: bytes) -> tuple[dict[str, object], int]:
    """Read-only byte inspection; hashes bind files, not committed provenance."""
    report = _report()
    report.update({
        "context_sha256": sha256_bytes(context_raw), "raw_sha256": sha256_bytes(response_raw),
        "context_byte_count": len(context_raw), "raw_byte_count": len(response_raw),
    })
    phase = "context_json"
    try:
        context = _bounded_value(context_raw, OFFLINE_LIMITS)
        phase = "context_inputs"
        binding, graph, graph_refs, source_refs, job, story = _context_inputs(context)
        phase = "response_json"
        response = _bounded_value(response_raw, OFFLINE_LIMITS)
        _check_limits(response, OFFLINE_LIMITS)
        phase = "response_structure"
        draft = ProposalDraftSet.from_mapping(response)
        if draft.input_binding_sha256 != binding:
            raise UnsupportedDiagnosticInput
        report.update({"proposal_count": len(draft.proposals), "graph_node_count": len(graph.nodes),
                       "source_count": len(source_refs)})
        phase = "proposal_rules"
        validate_story_proposals(
            draft, graph=graph, graph_object_refs=graph_refs, source_refs=source_refs,
            job_policy=job, story_policy=story,
        )
    except StoryProposalValidationError as error:
        report.update({"status": "proposal_rules_rejected", "phase": phase,
                       "diagnostic": error.to_diagnostic()})
        return report, 1
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError) as error:
        report.update({
            "status": "unsupported_diagnostic_input" if phase == "context_inputs"
            or type(error) is UnsupportedDiagnosticInput else "invalid_diagnostic_input",
            "phase": phase, "error_code": "OFFLINE_DIAGNOSTIC_INPUT_REJECTED",
            "exception_kind": type(error).__name__,
        })
        return report, 2
    report.update({"status": "proposal_rules_passed", "phase": phase})
    return report, 0


def _read_bounded(path: Path) -> bytes:
    with path.open("rb") as stream:
        raw = stream.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise UnsupportedDiagnosticInput
    return raw


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", type=Path, required=True)
    parser.add_argument("--response", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        context_raw = _read_bounded(args.context)
        response_raw = _read_bounded(args.response)
    except (OSError, ValueError) as error:
        report = _report()
        report.update({"status": "unsupported_diagnostic_input", "phase": "file_read",
                       "error_code": "OFFLINE_DIAGNOSTIC_INPUT_UNAVAILABLE",
                       "exception_kind": type(error).__name__})
        exit_code = 2
    else:
        report, exit_code = diagnose(context_raw, response_raw)
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
