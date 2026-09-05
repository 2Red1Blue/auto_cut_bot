"""Compact boundary tests use synthetic inputs, never a provider or database."""

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes, canonical_json_hash
from autocut_kernel.semantic_chain.material_support import evaluate_material_support
from autocut_kernel.semantic_chain.material_support_models import MaterialSupportEvaluation
from autocut_kernel.semantic_chain.member_refs import SemanticObjectRef
from autocut_kernel.semantic_chain.story_design_boundary import STAGE2_COMPACT_PROMPT
from autocut_kernel.semantic_chain.story_design_compact import (
    COMPACT_WIRE_SCHEMA_VERSION,
    build_story_design_compact_context,
    compact_contract_sha256,
    decode_story_design_compact,
    merge_compact_source_constraints,
    story_design_compact_response_schema,
)
from autocut_kernel.semantic_chain.story_design_compact_errors import CompactDraftError
from autocut_kernel.semantic_chain.story_design_compact_migration import (
    migrate_story_design_v1_to_compact,
)
from autocut_kernel.semantic_chain.story_design_compact_models import ProposalDraftSetV2
from autocut_kernel.semantic_chain.story_design_draft import (
    ProposalDraftSet,
    decode_story_design_draft,
)
from autocut_kernel.semantic_chain.story_design_models import SourceConstraints
from autocut_kernel.semantic_chain.story_design_validation import validate_story_proposals
from jsonschema import Draft202012Validator

from tests.semantic_chain.test_material_support import material_case
from tests.semantic_chain.test_story_design_draft import POLICY


@pytest.fixture
def compact_case():
    case = material_case()
    context = build_story_design_compact_context(
        case["inputs"], case["stage1"], case["projection"],
        **{key: case[key] for key in ("job_policy", "story_policy", "candidate_policy")},
    )
    raw = canonical_json_bytes(case["draft"].to_mapping())
    migration = migrate_story_design_v1_to_compact(raw, context=context, policy=POLICY)
    return case, context, migration


def test_rich_view_has_private_identities_and_deterministic_aliases(compact_case):
    case, context, _ = compact_case
    again = build_story_design_compact_context(
        case["inputs"], case["stage1"], case["projection"],
        **{key: case[key] for key in ("job_policy", "story_policy", "candidate_policy")},
    )
    assert again == context
    assert again.private_mapping() == context.private_mapping()
    assert "sha256:" not in context.model_view_json
    assert "member_ref" not in context.model_view_json
    assert "sha256:" in json.dumps(context.private_mapping())
    view = context.model_view()
    assert view["episodes"][0]["summary"] == case["stage1"].coverage.episode_digests.digests[0].summary
    assert view["candidates"][0]["reason"] == case["projection"].catalog.candidates[0].reason
    assert view["candidates"][0]["dialogue_excerpt"] == case["projection"].catalog.candidates[0].dialogue_excerpt
    assert len(view["connections"]) == len(context.graph.edges)
    assert len(set(alias for alias, _ in context.aliases)) == len(context.aliases)
    for prefix in "p f e t o c s n".split():
        refs = [ref for alias, ref in context.aliases if alias.startswith(prefix)]
        assert refs == sorted(refs, key=lambda ref: canonical_json_bytes(ref.to_mapping()))
    view["subjects"].clear()
    assert context.model_view() == again.model_view()
    assert compact_contract_sha256() == compact_contract_sha256()


def test_wire_schema_and_domain_codec_are_distinct_and_closed(compact_case):
    case, context, migration = compact_case
    schema = story_design_compact_response_schema(POLICY)
    Draft202012Validator.check_schema(schema)
    wire = json.loads(migration.wire_bytes)
    Draft202012Validator(schema).validate(wire)
    assert set(wire) == {"schema_version", "proposals"}
    assert wire["schema_version"] == COMPACT_WIRE_SCHEMA_VERSION
    assert "sha256:" not in migration.wire_bytes.decode()
    assert "proposal_id" not in migration.wire_bytes.decode()
    draft = decode_story_design_compact(migration.wire_bytes, context=context, policy=POLICY)
    assert ProposalDraftSetV2.from_mapping(draft.to_mapping()) == draft
    assert draft.proposals[0].required_fact_refs == case["draft"].proposals[0].required_fact_refs
    assert [row.title for row in draft.proposals] == [row.title for row in case["draft"].proposals]
    assert len({row.proposal_id for row in draft.proposals}) == len(draft.proposals)
    with pytest.raises(ValueError):
        ProposalDraftSet.from_mapping(draft.to_mapping())
    with pytest.raises(ValueError):
        ProposalDraftSetV2.from_mapping(case["draft"].to_mapping())


@pytest.mark.parametrize("field,value", [
    ("thread_refs", ["o1"]), ("thread_refs", ["t999999"]),
    ("key_subject_refs", ["f1"]), ("key_subject_refs", ["p999999"]),
    ("obligation_refs", ["o1", "o1"]), ("obligation_refs", []),
    ("material_requirements", []), ("required_fact_refs", []),
    ("proposal_id", "model-owned-id"), ("editing_profile_ref", "style999999"),
    ("genre_tags", ["not-in-policy"]), ("target_duration_seconds", {"min": 11, "max": 12}),
])
def test_invalid_model_choices_are_never_filled_or_retyped(compact_case, field, value):
    _, context, migration = compact_case
    wire = json.loads(migration.wire_bytes)
    wire["proposals"][0][field] = value
    with pytest.raises(ValueError):
        decode_story_design_compact(canonical_json_bytes(wire), context=context, policy=POLICY)


def test_subject_is_actual_observed_person_without_phantom_character(compact_case):
    _, context, migration = compact_case
    person_alias, person_ref = next((alias, ref) for alias, ref in context.aliases
                                  if alias.startswith("p") and ref.object_type == "entity")
    wire = json.loads(migration.wire_bytes)
    wire["proposals"][0]["key_subject_refs"] = [person_alias]
    draft = decode_story_design_compact(canonical_json_bytes(wire), context=context, policy=POLICY)
    assert draft.proposals[0].key_subject_refs == (person_ref,)
    assert person_ref.object_type == "entity"
    assert "key_character_refs" not in draft.proposals[0].to_mapping()
    with pytest.raises(ValueError):
        decode_story_design_draft(canonical_json_bytes(wire), expected_input_binding_sha256=context.input_binding_sha256, policy=POLICY)


def test_nonperson_entity_is_rejected_even_in_direct_domain_value(compact_case):
    _, context, migration = compact_case
    person = next(node for node in context.graph.nodes if node.node_type == "entity" and node.attributes.entity_kind == "person")
    changed_node = replace(person, attributes=replace(person.attributes, entity_kind="object"))
    graph = replace(context.graph, nodes=tuple(changed_node if node == person else node for node in context.graph.nodes))
    owner = replace(context.graph_owner, content_hash=graph.canonical_hash)
    proposal = migration.draft.proposals[0]

    def ref(value):
        return replace(value, member_ref=owner)

    proposal = replace(proposal, thread_refs=tuple(map(ref, proposal.thread_refs)),
                       required_obligation_refs=tuple(map(ref, proposal.required_obligation_refs)),
                       required_fact_refs=tuple(map(ref, proposal.required_fact_refs)),
                       key_subject_refs=(SemanticObjectRef(owner, "entity", person.node_id),),
                       material_requirements=tuple(replace(row, obligation_ref=ref(row.obligation_ref)) for row in proposal.material_requirements))
    draft = replace(migration.draft, proposals=(proposal, replace(proposal, proposal_id="second")))
    with pytest.raises(ValueError, match="observed subject must be a person"):
        validate_story_proposals(draft, graph=graph,
                                 graph_object_refs=tuple(SemanticObjectRef(owner, node.node_type, node.node_id) for node in graph.nodes),
                                 source_refs=context.granted_sources, job_policy=context.job_policy, story_policy=context.story_policy)


def test_policy_merge_never_treats_empty_intersection_as_unrestricted(compact_case):
    _, context, migration = compact_case
    unconstrained_job = replace(context.job_policy, source_constraints=SourceConstraints((), (), "render_source"))
    unrestricted = replace(context, job_policy=unconstrained_job)
    all_granted = {"source_selection": "all_granted", "allowed_source_refs": [], "forbidden_source_refs": []}
    result = merge_compact_source_constraints(all_granted, unrestricted)
    assert set(result.allowed_source_refs) == set(context.granted_sources)
    denied = {**all_granted, "forbidden_source_refs": [context.alias_for(ref) for ref in context.granted_sources]}
    with pytest.raises(ValueError, match="COMPACT_MATERIAL_INFEASIBLE"):
        merge_compact_source_constraints(denied, unrestricted)
    with pytest.raises(ValueError, match="COMPACT_SOURCE_SELECTION_INVALID"):
        merge_compact_source_constraints({**all_granted, "source_selection": "subset"}, context)
    with pytest.raises(ValueError):
        merge_compact_source_constraints({**all_granted, "forbidden_source_refs": ["s999999"]}, context)
    wire = json.loads(migration.wire_bytes)
    for proposal in wire["proposals"]:
        for requirement in proposal["material_requirements"]:
            requirement["additional_checks"] = []
    draft = decode_story_design_compact(canonical_json_bytes(wire), context=context, policy=POLICY)
    assert draft.proposals[0].material_requirements[0].physical_requirements == context.story_policy.required_physical_requirements


@pytest.mark.parametrize("mutation", ["duplicate_key", "bytes", "depth", "references", "missing", "owner"])
def test_strict_json_budgets_and_unknown_fields(compact_case, mutation):
    _, context, migration = compact_case
    raw = migration.wire_bytes
    policy = POLICY
    if mutation == "duplicate_key":
        raw = raw.replace(b'"schema_version":', b'"schema_version":"duplicate","schema_version":', 1)
    elif mutation == "bytes":
        policy = replace(policy, max_response_bytes=len(raw) - 1)
    elif mutation == "depth":
        policy = replace(policy, max_json_depth=2)
    elif mutation == "references":
        policy = replace(policy, max_total_references=1)
    else:
        wire = json.loads(raw)
        if mutation == "missing":
            del wire["proposals"][0]["thread_refs"]
        else:
            wire["input_binding_sha256"] = context.input_binding_sha256
        raw = canonical_json_bytes(wire)
    with pytest.raises(ValueError):
        decode_story_design_compact(raw, context=context, policy=policy)


def test_material_v2_roundtrip_does_not_relax_v1(compact_case):
    case, _, migration = compact_case
    support = evaluate_material_support(case["inputs"], case["stage1"], case["projection"], migration.draft,
                                        **{key: case[key] for key in ("job_policy", "story_policy", "candidate_policy")})
    wire = support.to_mapping()
    assert wire["schema_version"] == "stage2-material-support-v2"
    assert MaterialSupportEvaluation.from_mapping(wire) == support
    wrong = deepcopy(wire)
    wrong["schema_version"] = "stage2-material-support-v1"
    with pytest.raises(ValueError):
        MaterialSupportEvaluation.from_mapping(wrong)


def test_explicit_migration_repairs_only_bound_person_and_derives_fact_closure(compact_case):
    case, context, _ = compact_case
    person = next(ref for alias, ref in context.aliases if alias.startswith("p") and ref.object_type == "entity")
    original = case["draft"]
    wrong_character = replace(person, object_type="character")
    proposal = replace(original.proposals[0], key_character_refs=(wrong_character,), required_fact_refs=())
    raw = canonical_json_bytes(replace(original, proposals=(proposal, original.proposals[1])).to_mapping())
    migrated = migrate_story_design_v1_to_compact(raw, context=context, policy=POLICY)
    assert migrated.draft.proposals[0].key_subject_refs == (person,)
    assert migrated.draft.proposals[0].required_fact_refs == original.proposals[0].required_fact_refs
    assert {row["kind"] for row in migrated.changes} >= {"declared_character_to_observed_person", "derived_obligation_fact_closure"}
    foreign = replace(wrong_character, member_ref=replace(wrong_character.member_ref, revision=2))
    proposal_wire = json.loads(raw)
    proposal_wire["proposals"][0]["key_character_refs"] = [foreign.to_mapping()]
    with pytest.raises(ValueError):
        migrate_story_design_v1_to_compact(canonical_json_bytes(proposal_wire), context=context, policy=POLICY)


@pytest.mark.parametrize("field,value,code", [
    ("key_subject_refs", ["private-token-do-not-export"], "COMPACT_REFERENCE_NOT_FOUND"),
    ("thread_refs", ["t999999"], "COMPACT_REFERENCE_NOT_FOUND"),
    ("obligation_refs", ["o1", "o1"], "COMPACT_DUPLICATE_REFERENCE"),
])
def test_typed_reference_diagnostics_export_only_local_path(compact_case, field, value, code):
    _, context, migration = compact_case
    wire = json.loads(migration.wire_bytes)
    wire["proposals"][0][field] = value
    with pytest.raises(CompactDraftError) as rejected:
        decode_story_design_compact(canonical_json_bytes(wire), context=context, policy=POLICY)
    diagnostic = rejected.value.to_diagnostic()
    assert diagnostic["error_code"] == code
    assert diagnostic["proposal_index"] == 0
    assert diagnostic["json_path"] == f"$.proposals[0].{field}[{len(value) - 1}]"
    assert "private-token" not in json.dumps(diagnostic)
    assert "sha256:" not in json.dumps(diagnostic)


def test_diagnostic_revalidates_mutable_exception_metadata():
    error = CompactDraftError("COMPACT_REFERENCE_NOT_FOUND", json_path="$.proposals[0].thread_refs[2]", proposal_index=0)
    error.error_code = "SECRET-provider-url"
    error.json_path = "$.proposals[0].SECRET-provider-url"
    error.proposal_index = True
    diagnostic = error.to_diagnostic()
    assert diagnostic["error_code"] == "COMPACT_FIELD_INVALID"
    assert diagnostic["json_path"] == "$" and diagnostic["proposal_index"] is None
    assert "SECRET" not in json.dumps(diagnostic)


def test_source_constraints_can_only_shrink_job_and_grant_universe(compact_case):
    # Pure merger algebra over synthetic values is not a committed SourceGrant.
    _, original, _ = compact_case
    first = original.granted_sources[0]
    sources = (first, replace(first, object_id="synthetic-second"), replace(first, object_id="synthetic-third"))
    aliases = tuple((alias, ref) for alias, ref in original.aliases if not alias.startswith("s"))
    aliases += tuple((f"s{index}", ref) for index, ref in enumerate(sources, 1))
    job = replace(original.job_policy, source_constraints=SourceConstraints(sources[:2], (sources[2],), "render_source"))
    context = replace(original, aliases=aliases, granted_sources=sources, job_policy=job)
    base = {"source_selection": "all_granted", "allowed_source_refs": [], "forbidden_source_refs": []}
    baseline = merge_compact_source_constraints(base, context)
    assert set(baseline.allowed_source_refs) == set(sources[:2])
    narrowed = merge_compact_source_constraints({**base, "source_selection": "subset", "allowed_source_refs": ["s1", "s3"]}, context)
    assert narrowed.allowed_source_refs == (first,)
    assert set(narrowed.allowed_source_refs) <= set(baseline.allowed_source_refs)
    with pytest.raises(CompactDraftError, match="COMPACT_MATERIAL_INFEASIBLE"):
        merge_compact_source_constraints({**base, "source_selection": "subset", "allowed_source_refs": ["s3"]}, context)


def test_source_error_path_has_requirement_and_original_ref_index(compact_case):
    _, context, migration = compact_case
    wire = json.loads(migration.wire_bytes)
    source = wire["proposals"][1]["material_requirements"][0]["source_constraints"]
    source["forbidden_source_refs"] = ["s999999"]
    with pytest.raises(CompactDraftError) as rejected:
        decode_story_design_compact(canonical_json_bytes(wire), context=context, policy=POLICY)
    assert rejected.value.to_diagnostic()["json_path"] == "$.proposals[1].material_requirements[0].source_constraints.forbidden_source_refs[0]"
    assert rejected.value.to_diagnostic()["proposal_index"] == 1


@pytest.mark.parametrize("filename", ["story_design_boundary.py", "story_design_compact_errors.py"])
def test_compact_identity_binds_dispatcher_and_diagnostic_implementation(monkeypatch, filename):
    original_hash = compact_contract_sha256()
    original_read = Path.read_bytes

    def changed_read(path):
        content = original_read(path)
        return content + b"\n# simulated implementation revision\n" if path.name == filename else content

    monkeypatch.setattr(Path, "read_bytes", changed_read)
    assert compact_contract_sha256() != original_hash


def test_compact_identity_binds_effective_prompt_text():
    original_hash = compact_contract_sha256()
    assert compact_contract_sha256(prompt_template=STAGE2_COMPACT_PROMPT) == original_hash
    assert compact_contract_sha256(prompt_template=STAGE2_COMPACT_PROMPT + "\nAdditional frozen guidance.") != original_hash
    with pytest.raises(ValueError):
        compact_contract_sha256(prompt_template="")


def test_compact_empty_draft_is_rejected_without_changing_legacy_empty_wire(compact_case):
    _, context, migration = compact_case
    with pytest.raises(ValueError):
        ProposalDraftSetV2(context.input_binding_sha256, ())
    domain = {**migration.draft.to_mapping(), "proposals": []}
    with pytest.raises(ValueError):
        ProposalDraftSetV2.from_mapping(domain)
    raw = canonical_json_bytes({"schema_version": COMPACT_WIRE_SCHEMA_VERSION, "proposals": []})
    with pytest.raises(CompactDraftError) as rejected:
        decode_story_design_compact(raw, context=context, policy=POLICY)
    assert rejected.value.to_diagnostic()["json_path"] == "$.proposals"
    assert not Draft202012Validator(story_design_compact_response_schema(POLICY)).is_valid(json.loads(raw))
    legacy = ProposalDraftSet(context.input_binding_sha256, ())
    assert decode_story_design_draft(canonical_json_bytes(legacy.to_mapping()),
                                     expected_input_binding_sha256=context.input_binding_sha256, policy=POLICY) == legacy


def test_empty_material_v2_cannot_be_decoded_or_serialized_as_v1(compact_case):
    case, context, migration = compact_case
    support = evaluate_material_support(case["inputs"], case["stage1"], case["projection"], migration.draft,
                                        **{key: case[key] for key in ("job_policy", "story_policy", "candidate_policy")})
    legacy_draft = ProposalDraftSet(context.input_binding_sha256, ())
    empty = replace(support, proposals=(), draft_sha256=legacy_draft.canonical_hash)
    assert empty.to_mapping()["schema_version"] == "stage2-material-support-v1"
    assert MaterialSupportEvaluation.from_mapping(empty.to_mapping()) == empty
    # An explicit v2 marker cannot be discarded even with the matching v1 hash.
    with pytest.raises(ValueError):
        MaterialSupportEvaluation.from_mapping({**empty.to_mapping(), "schema_version": "stage2-material-support-v2"})
    empty_v2_hash = canonical_json_hash({**migration.draft.to_mapping(), "proposals": []})
    with pytest.raises(ValueError):
        replace(support, proposals=(), draft_sha256=empty_v2_hash)
