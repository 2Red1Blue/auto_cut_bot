"""Pure CandidateCatalog V2 enrichment over exact committed V4 observations."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import replace

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes
from autocut_kernel.semantic_chain.candidate_catalog_v2 import (
    CandidateCapabilityPolicy,
    CandidateCapabilityRule,
    CandidateCatalogV2,
    CandidateCatalogV2Policy,
)
from autocut_kernel.semantic_chain.candidate_enrichment_compiler import (
    CandidateEnrichmentCompilerError,
    candidate_enrichment_prompt_inputs,
    compile_candidate_enrichment,
)
from autocut_kernel.semantic_chain.candidate_enrichment_draft import (
    CANDIDATE_ENRICHMENT_DRAFT_SCHEMA_VERSION,
    CandidateEnrichmentAlias,
    CandidateEnrichmentDraftError,
    CandidateEnrichmentDraftPolicy,
    CandidateEnrichmentReferenceCatalog,
    decode_candidate_enrichment_draft,
)
from autocut_kernel.semantic_chain.coverage_admission import CoverageAdmission
from autocut_kernel.semantic_chain.coverage_analysis import Stage1CoveragePolicy
from autocut_kernel.semantic_chain.coverage_compiler import compile_stage1_coverage
from autocut_kernel.semantic_chain.dependency_projection import DependencyProjectionPolicy
from autocut_kernel.semantic_chain.dependency_proof import build_dependency_proof
from autocut_kernel.semantic_chain.member_refs import SemanticMemberIdentity
from autocut_kernel.semantic_chain.stage1_checks import KC_RULE_IDS, Stage1Check
from autocut_kernel.semantic_chain.stage1_members import decode_coverage_members
from autocut_kernel.semantic_chain.stage1_result import decode_stage1_members
from autocut_kernel.source_manifest import SourceOperationPolicy

from tests.semantic_chain.test_stage1_result import _member
from tests.semantic_chain.test_stage1_v4_flow import POLICY as STAGE1_DRAFT_POLICY
from tests.semantic_chain.test_stage1_v4_flow import _draft as _stage1_draft
from tests.semantic_chain.test_stage1_v4_flow import _v4_inputs

DRAFT_POLICY = CandidateEnrichmentDraftPolicy(64_000, 8, 4, 6, 8, 256, 2_048)
CATALOG_POLICY = CandidateCatalogV2Policy(
    "candidate-catalog-v2",
    "candidate-content-id-v1",
    "anchor-source-envelope-v1",
)
CAPABILITY_POLICY = CandidateCapabilityPolicy(
    "registered-semantic-capabilities-v1",
    (
        CandidateCapabilityRule("dialogue_salience_v1", "0.7", "0.6"),
        CandidateCapabilityRule("action_salience_v1", "0.5", "0.5"),
    ),
)
COVERAGE_POLICY = Stage1CoveragePolicy("0", "strict_global")


def _admitted_case():
    inputs = _v4_inputs()
    grant = replace(
        inputs.source_grant,
        policy=SourceOperationPolicy(
            inputs.source_grant.policy.authorization_id,
            inputs.source_grant.policy.series_id,
            inputs.source_grant.policy.expected_source_count,
            ("semantic_analysis", "render_source"),
        ),
    )
    inputs = replace(inputs, source_grant=grant)
    raw = canonical_json_bytes(_stage1_draft(inputs))
    compilation = compile_stage1_coverage(
        inputs,
        raw,
        draft_policy=STAGE1_DRAFT_POLICY,
        coverage_policy=COVERAGE_POLICY,
        scope=inputs.source_manifest.reference.scope,
        revision=1,
    )
    dependency_policy = DependencyProjectionPolicy("semantic-dependencies-v1")
    proof = build_dependency_proof(
        inputs,
        graph_member=compilation.narrative.narrative_graph,
        event_card_member=compilation.narrative.event_cards,
        ledger_member=compilation.coverage_ledger,
        policy=dependency_policy,
        revision=1,
    )
    business = (*compilation.members, proof)
    coverage = decode_coverage_members(
        compilation.members, scope=inputs.source_manifest.reference.scope
    )
    admission = CoverageAdmission(
        "admitted-stage1-v1",
        coverage.coverage_ledger.input_binding_sha256,
        "sha256:" + hashlib.sha256(raw).hexdigest(),
        coverage.coverage_ledger.draft_sha256,
        STAGE1_DRAFT_POLICY.canonical_hash,
        COVERAGE_POLICY.canonical_hash,
        dependency_policy.canonical_hash,
        "strict_global",
        "stage1-kc-v1",
        tuple(SemanticMemberIdentity.from_artifact_member(item) for item in business),
        tuple(Stage1Check(rule_id, "pass", ()) for rule_id in KC_RULE_IDS),
    )
    members = (
        *business,
        _member(
            "coverage_admission",
            inputs.source_manifest.reference.scope,
            admission.to_mapping(),
        ),
    )
    stage1 = decode_stage1_members(members, scope=inputs.source_manifest.reference.scope)
    assert stage1.admission.validation_status == "valid"
    assert stage1.admission.next_action == "continue"
    return inputs, stage1


def _provider_payload(prompt, *, reverse: bool = False):
    event = next(
        item for item in prompt.reference_catalog.aliases if item.object_type == "event"
    )
    fact = event.direct_fact_aliases[0]
    measurements = [
        {
            "measurement_kind": "dialogue_salience",
            "value": "0.8",
            "confidence": "0.7",
            "evidence_refs": [event.alias, fact] if not reverse else [fact, event.alias],
        },
        {
            "measurement_kind": "action_salience",
            "value": "0.4",
            "confidence": "0.9",
            "evidence_refs": [event.alias],
        },
    ]
    if reverse:
        measurements.reverse()
    return {
        "schema_version": CANDIDATE_ENRICHMENT_DRAFT_SCHEMA_VERSION,
        "candidates": [
            {
                "local_candidate_id": "candidate_1",
                "summary": "The visible discovery has strong semantic story value.",
                "anchor_refs": [event.alias],
                "semantic_measurements": measurements,
            }
        ],
    }


def _raw(payload: object) -> bytes:
    return canonical_json_bytes(payload)


def _compile(payload: object):
    inputs, stage1 = _admitted_case()
    return compile_candidate_enrichment(
        inputs,
        stage1,
        _raw(payload),
        scope=inputs.source_manifest.reference.scope,
        revision=1,
        draft_policy=DRAFT_POLICY,
        catalog_policy=CATALOG_POLICY,
        capability_policy=CAPABILITY_POLICY,
    )


def test_exact_v4_and_admitted_stage1_compile_deterministic_closed_catalog() -> None:
    inputs, stage1 = _admitted_case()
    prompt = candidate_enrichment_prompt_inputs(
        inputs,
        stage1,
        draft_policy=DRAFT_POLICY,
        catalog_policy=CATALOG_POLICY,
        capability_policy=CAPABILITY_POLICY,
    )
    first = compile_candidate_enrichment(
        inputs,
        stage1,
        _raw(_provider_payload(prompt)),
        scope=inputs.source_manifest.reference.scope,
        revision=1,
        draft_policy=DRAFT_POLICY,
        catalog_policy=CATALOG_POLICY,
        capability_policy=CAPABILITY_POLICY,
    )
    reordered = compile_candidate_enrichment(
        inputs,
        stage1,
        _raw(_provider_payload(prompt, reverse=True)),
        scope=inputs.source_manifest.reference.scope,
        revision=1,
        draft_policy=DRAFT_POLICY,
        catalog_policy=CATALOG_POLICY,
        capability_policy=CAPABILITY_POLICY,
    )
    assert reordered.catalog == first.catalog
    assert reordered.member.content_hash == first.member.content_hash
    assert CandidateCatalogV2.from_mapping(first.catalog.to_mapping()) == first.catalog

    candidate = first.catalog.candidates[0]
    assert candidate.anchor_refs[0].vlm_event_ref.object_id == candidate.anchor_refs[0].event_card_ref.object_id
    assert {item.object_type for item in candidate.semantic_measurements[0].evidence_refs} == {
        "vlm_event",
        "vlm_fact",
    }
    assert candidate.semantic_measurements[0].value == "0.8"
    assert candidate.semantic_measurements[0].confidence == "0.7"
    outcomes = {item.capability: item.outcome for item in candidate.capability_assessment}
    assert outcomes == {"dialogue": "available", "action": "value_below_threshold"}
    assert candidate.coarse_support.source_interval == inputs.inputs[0].semantic_pack.semantic_pack.events[0].support.source_interval
    assert candidate.source_window_ref.object_id == candidate.coarse_support.core_owner_window_manifest_sha256


def test_output_contains_no_later_or_physical_authority_fields() -> None:
    inputs, stage1 = _admitted_case()
    prompt = candidate_enrichment_prompt_inputs(
        inputs,
        stage1,
        draft_policy=DRAFT_POLICY,
        catalog_policy=CATALOG_POLICY,
        capability_policy=CAPABILITY_POLICY,
    )
    wire = compile_candidate_enrichment(
        inputs,
        stage1,
        _raw(_provider_payload(prompt)),
        scope=inputs.source_manifest.reference.scope,
        revision=1,
        draft_policy=DRAFT_POLICY,
        catalog_policy=CATALOG_POLICY,
        capability_policy=CAPABILITY_POLICY,
    ).catalog.to_mapping()

    forbidden = ("frame", "asr", "vad", "physical", "endpoint", "admission", "publication", "publish", "pass")

    def visit(value: object) -> None:
        if type(value) is dict:
            for key, child in value.items():
                assert not any(item in key.casefold() for item in forbidden)
                visit(child)
        elif type(value) is list:
            for child in value:
                visit(child)

    visit(wire)


@pytest.mark.parametrize(
    "mutation",
    (
        "unknown-field",
        "unknown-ref",
        "duplicate-anchor",
        "duplicate-evidence",
        "outside-closure",
        "noncanonical-decimal",
        "provider-capability",
        "physical-field",
    ),
)
def test_decoder_rejects_untrusted_reference_and_authority_drift(mutation: str) -> None:
    inputs, stage1 = _admitted_case()
    prompt = candidate_enrichment_prompt_inputs(
        inputs,
        stage1,
        draft_policy=DRAFT_POLICY,
        catalog_policy=CATALOG_POLICY,
        capability_policy=CAPABILITY_POLICY,
    )
    payload = _provider_payload(prompt)
    candidate = payload["candidates"][0]
    measurement = candidate["semantic_measurements"][0]
    if mutation == "unknown-field":
        measurement["explanation"] = "not in the contract"
    elif mutation == "unknown-ref":
        candidate["anchor_refs"] = ["w9999/event/missing"]
    elif mutation == "duplicate-anchor":
        candidate["anchor_refs"] *= 2
    elif mutation == "duplicate-evidence":
        measurement["evidence_refs"] *= 2
    elif mutation == "outside-closure":
        other = CandidateEnrichmentAlias(
            "w0001/fact/other",
            "fact",
            prompt.reference_catalog.aliases[0].owner_window_manifest_sha256,
            "sha256:" + "9" * 64,
        )
        references = CandidateEnrichmentReferenceCatalog(
            tuple(sorted((*prompt.reference_catalog.aliases, other), key=lambda item: item.alias))
        )
        measurement["evidence_refs"] = [other.alias]
        with pytest.raises(CandidateEnrichmentDraftError, match="escapes anchor closure"):
            decode_candidate_enrichment_draft(
                _raw(payload), policy=DRAFT_POLICY, references=references
            )
        return
    elif mutation == "noncanonical-decimal":
        measurement["value"] = "0.80"
    elif mutation == "provider-capability":
        candidate["capability_assessment"] = [{"capability": "dialogue"}]
    else:
        candidate["physical_endpoint"] = 10
    with pytest.raises(CandidateEnrichmentDraftError):
        decode_candidate_enrichment_draft(
            _raw(payload), policy=DRAFT_POLICY, references=prompt.reference_catalog
        )


def test_decoder_rejects_cross_owner_candidate_without_compiler_defaults() -> None:
    owner_a = "sha256:" + "a" * 64
    owner_b = "sha256:" + "b" * 64
    refs = CandidateEnrichmentReferenceCatalog(
        (
            CandidateEnrichmentAlias(
                "w0001/event/event_1", "event", owner_a, "sha256:" + "1" * 64
            ),
            CandidateEnrichmentAlias(
                "w0002/event/event_1", "event", owner_b, "sha256:" + "2" * 64
            ),
        )
    )
    payload = {
        "schema_version": CANDIDATE_ENRICHMENT_DRAFT_SCHEMA_VERSION,
        "candidates": [
            {
                "local_candidate_id": "candidate_1",
                "summary": "Cross-owner candidate.",
                "anchor_refs": ["w0002/event/event_1", "w0001/event/event_1"],
                "semantic_measurements": [
                    {
                        "measurement_kind": "visual_salience",
                        "value": "0.8",
                        "confidence": "0.8",
                        "evidence_refs": ["w0001/event/event_1"],
                    }
                ],
            }
        ],
    }
    with pytest.raises(CandidateEnrichmentDraftError, match="cross observation owners"):
        decode_candidate_enrichment_draft(_raw(payload), policy=DRAFT_POLICY, references=refs)


def test_compiler_rejects_nonadmitted_stage1_and_missing_render_grant() -> None:
    inputs, stage1 = _admitted_case()
    prompt = candidate_enrichment_prompt_inputs(
        inputs,
        stage1,
        draft_policy=DRAFT_POLICY,
        catalog_policy=CATALOG_POLICY,
        capability_policy=CAPABILITY_POLICY,
    )
    denied = replace(
        inputs,
        source_grant=replace(
            inputs.source_grant,
            policy=SourceOperationPolicy(
                inputs.source_grant.policy.authorization_id,
                inputs.source_grant.policy.series_id,
                inputs.source_grant.policy.expected_source_count,
                ("semantic_analysis",),
            ),
        ),
    )
    with pytest.raises(CandidateEnrichmentCompilerError, match="render_source"):
        compile_candidate_enrichment(
            denied,
            stage1,
            _raw(_provider_payload(prompt)),
            scope=denied.source_manifest.reference.scope,
            revision=1,
            draft_policy=DRAFT_POLICY,
            catalog_policy=CATALOG_POLICY,
            capability_policy=CAPABILITY_POLICY,
        )
    checks = list(stage1.admission.rule_results)
    checks[0] = Stage1Check(checks[0].rule_id, "fail", ("not_admitted",))
    nonadmitted = replace(stage1, admission=replace(stage1.admission, rule_results=tuple(checks)))
    with pytest.raises(CandidateEnrichmentCompilerError, match="admitted Stage1"):
        candidate_enrichment_prompt_inputs(
            inputs,
            nonadmitted,
            draft_policy=DRAFT_POLICY,
            catalog_policy=CATALOG_POLICY,
            capability_policy=CAPABILITY_POLICY,
        )


def test_catalog_codec_rejects_self_approval_and_tampered_capability() -> None:
    inputs, stage1 = _admitted_case()
    prompt = candidate_enrichment_prompt_inputs(
        inputs,
        stage1,
        draft_policy=DRAFT_POLICY,
        catalog_policy=CATALOG_POLICY,
        capability_policy=CAPABILITY_POLICY,
    )
    catalog = compile_candidate_enrichment(
        inputs,
        stage1,
        _raw(_provider_payload(prompt)),
        scope=inputs.source_manifest.reference.scope,
        revision=1,
        draft_policy=DRAFT_POLICY,
        catalog_policy=CATALOG_POLICY,
        capability_policy=CAPABILITY_POLICY,
    ).catalog
    wire = catalog.to_mapping()
    with pytest.raises(ValueError):
        CandidateCatalogV2.from_mapping({**wire, "pass": True})
    tampered = deepcopy(wire)
    tampered["candidates"][0]["capability_assessment"][0]["basis_measurement_ids"] = []
    with pytest.raises(ValueError):
        CandidateCatalogV2.from_mapping(tampered)
