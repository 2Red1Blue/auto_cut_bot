"""Pure compiled Blueprint values; no admission, Store or provider fixture."""

import json
from dataclasses import replace

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes, canonical_json_hash
from autocut_kernel.semantic_chain.editorial_blueprint import (
    EDITORIAL_BLUEPRINT_STRATEGY_VERSION,
    BlueprintEvidenceRequirement,
    EditorialBlueprint,
    EditorialBlueprintBeat,
    EditorialBlueprintError,
    EditorialBlueprintProjection,
    _evidence_requirement_id,
    project_editorial_blueprints,
)
from autocut_kernel.semantic_chain.editorial_members import (
    compose_editorial_business_members,
    decode_editorial_business_members,
)
from autocut_kernel.semantic_chain.story_design_models import (
    PhysicalRequirement,
    SourceConstraints,
)
from autocut_kernel.store.models import canonical_payload_hash

from tests.semantic_chain.test_editorial_members import editorial_case
from tests.semantic_chain.test_editorial_models import _beat, _ref, _story


def _requirement(story_id: str) -> BlueprintEvidenceRequirement:
    beat = _beat()
    physical = (PhysicalRequirement("dialogue_integrity", "complete"),)
    return BlueprintEvidenceRequirement(
        _evidence_requirement_id(story_id, 0, 0, "material-0"), "material-0",
        beat.required_obligation_refs[0], beat.required_fact_refs, 1, physical,
        canonical_json_hash([item.to_mapping() for item in physical]),
        SourceConstraints((_ref("whole_series_source_manifest", "source", "source-1"),), (), "render_source"),
        "one_of", beat.evidence_requirements[0].alternative_sets,
    )


def test_compiled_blueprint_derives_stable_ordered_beat_identity_and_hashes() -> None:
    story = _story()
    requirement = _requirement(story.story_id)
    source = story.beats[0]
    beats = (
        EditorialBlueprintBeat(
            canonical_json_hash({"schema_version": "stage3-editorial-beat-id-v1", "story_id": story.story_id,
                                 "strategy_version": EDITORIAL_BLUEPRINT_STRATEGY_VERSION, "ordinal": 0}),
            0, source.narrative_role, source.narrative_function, source.summary,
            source.required_obligation_refs, source.required_fact_refs, (requirement,),
            source.candidate_preferences, source.span_policy, source.duration_seconds,
        ),
    )
    blueprint = EditorialBlueprint(story.story_id, story.proposal_ref, EDITORIAL_BLUEPRINT_STRATEGY_VERSION,
                                   beats, (), story.story_duration_seconds,
                                   story.editing_intent, story.teaser_intent)
    projection = EditorialBlueprintProjection("sha256:" + "b" * 64, EDITORIAL_BLUEPRINT_STRATEGY_VERSION, (blueprint,))
    assert projection.canonical_hash == canonical_json_hash(projection.to_mapping())
    assert blueprint.beats[0].beat_id == canonical_json_hash({"schema_version": "stage3-editorial-beat-id-v1", "story_id": story.story_id, "strategy_version": EDITORIAL_BLUEPRINT_STRATEGY_VERSION, "ordinal": 0})
    assert requirement.physical_requirements_hash == canonical_json_hash([
        item.to_mapping() for item in requirement.physical_requirements
    ])


def test_compiled_values_reject_wrong_strategy_order_or_modified_physical_copy() -> None:
    story = _story()
    requirement = _requirement(story.story_id)
    beat = _beat()
    stable = canonical_json_hash({"schema_version": "stage3-editorial-beat-id-v1", "story_id": story.story_id,
                                  "strategy_version": EDITORIAL_BLUEPRINT_STRATEGY_VERSION, "ordinal": 0})
    compiled = EditorialBlueprintBeat(stable, 0, beat.narrative_role, beat.narrative_function, beat.summary,
                                      beat.required_obligation_refs, beat.required_fact_refs, (requirement,),
                                      beat.candidate_preferences, beat.span_policy, beat.duration_seconds)
    with pytest.raises(EditorialBlueprintError):
        EditorialBlueprint(story.story_id, story.proposal_ref, "partitioned-v1", (compiled,), (),
                           story.story_duration_seconds, story.editing_intent, story.teaser_intent)
    with pytest.raises(EditorialBlueprintError):
        replace(requirement, physical_requirements_hash="sha256:" + "f" * 64)
    with pytest.raises(EditorialBlueprintError):
        EditorialBlueprintProjection("sha256:" + "b" * 64, "partitioned-v1", ())


def test_real_two_story_projection_preserves_frozen_order_and_every_requirement() -> None:
    inputs, contexts, draft, projection = editorial_case()
    assert tuple(item.story_id for item in projection.blueprints) == inputs.portfolio.values.business.portfolio.target_story_ids
    assert projection.input_binding_sha256 == contexts.input_binding_sha256
    assert EditorialBlueprintProjection.from_mapping(projection.to_mapping()) == projection
    for compiled, source in zip(projection.blueprints, draft.stories, strict=True):
        assert tuple(item.ordinal for item in compiled.beats) == tuple(range(len(source.beats)))
        assert {item.material_requirement_id for beat in compiled.beats for item in beat.evidence_requirements} == {
            item.source_material_requirement_id for beat in source.beats for item in beat.evidence_requirements
        }


@pytest.mark.parametrize("mutation", ("dropped_obligation", "foreign_proposal", "reordered_targets"))
def test_projection_rejects_real_draft_target_proposal_and_mandatory_coverage_drift(mutation: str) -> None:
    inputs, contexts, draft, _ = editorial_case()
    stories = list(draft.stories)
    if mutation == "dropped_obligation":
        stories[0] = replace(stories[0], beats=(replace(stories[0].beats[0], required_obligation_refs=()),))
    elif mutation == "foreign_proposal":
        stories[0] = replace(stories[0], proposal_ref=replace(stories[0].proposal_ref, object_id="foreign"))
    else:
        stories.reverse()
    changed = replace(draft, stories=tuple(stories))
    with pytest.raises(EditorialBlueprintError):
        project_editorial_blueprints(inputs.narrative.values, inputs.portfolio.values, changed,
                                    expected_input_binding_sha256=contexts.input_binding_sha256,
                                    strategy_version=contexts.policy.strategy)


def test_closed_decoders_reject_unknown_missing_duplicate_ids_and_invalid_enum() -> None:
    _inputs, _contexts, _draft, projection = editorial_case()
    mapping = projection.to_mapping()
    for changed in (
        {**mapping, "unknown": True},
        {key: value for key, value in mapping.items() if key != "blueprints"},
        {**mapping, "strategy_version": "partitioned-v1"},
    ):
        with pytest.raises(ValueError):
            EditorialBlueprintProjection.from_mapping(changed)
    duplicate = projection.to_mapping()
    duplicate["blueprints"].append(duplicate["blueprints"][0])
    with pytest.raises(ValueError):
        EditorialBlueprintProjection.from_mapping(duplicate)
    bad_enum = projection.to_mapping()
    bad_enum["blueprints"][0]["beats"][0]["narrative_function"] = "unknown"
    with pytest.raises(ValueError):
        EditorialBlueprintProjection.from_mapping(bad_enum)


@pytest.mark.parametrize(
    "mutation",
    (
        "foreign_fact_owner", "foreign_obligation_owner", "dropped_nested_fact",
        "derived_id", "role", "duplicate_alternative", "ordering_out_of_range",
    ),
)
def test_projection_decoder_rejects_rehashed_nested_closure_tampering(mutation: str) -> None:
    _inputs, _contexts, _draft, projection = editorial_case()
    mapping = projection.to_mapping()
    beat = mapping["blueprints"][0]["beats"][0]
    requirement = beat["evidence_requirements"][0]
    if mutation == "foreign_fact_owner":
        beat["required_fact_refs"][0]["member_ref"]["content_hash"] = "sha256:" + "f" * 64
    elif mutation == "foreign_obligation_owner":
        requirement["obligation_ref"]["member_ref"]["content_hash"] = "sha256:" + "f" * 64
    elif mutation == "dropped_nested_fact":
        beat["required_fact_refs"] = []
    elif mutation == "derived_id":
        requirement["evidence_requirement_id"] = "sha256:" + "f" * 64
    elif mutation == "role":
        beat["narrative_role"] = "unknown-role"
    elif mutation == "duplicate_alternative":
        alternative = requirement["alternatives"][0]
        duplicated = dict(alternative)
        duplicated["alternative_id"] = alternative["alternative_id"]
        requirement["alternatives"].append(duplicated)
    else:
        mapping["blueprints"][0]["ordering_constraints"] = [{
            "constraint_type": "precedes", "before_ordinal": 0, "after_ordinal": len(mapping["blueprints"][0]["beats"]),
        }]
    with pytest.raises(ValueError):
        EditorialBlueprintProjection.from_mapping(mapping)


@pytest.mark.parametrize("mutation", ("foreign_fact_owner", "dropped_nested_fact", "derived_id", "role"))
def test_business_decoder_rejects_fully_rehashed_nested_blueprint_tampering(mutation: str) -> None:
    _inputs, contexts, _draft, projection = editorial_case()
    members = list(compose_editorial_business_members(contexts, projection).members)
    payload = json.loads(members[0].payload_json)
    beat = payload["blueprint"]["beats"][0]
    requirement = beat["evidence_requirements"][0]
    if mutation == "foreign_fact_owner":
        beat["required_fact_refs"][0]["member_ref"]["content_hash"] = "sha256:" + "f" * 64
    elif mutation == "dropped_nested_fact":
        beat["required_fact_refs"] = []
    elif mutation == "derived_id":
        requirement["evidence_requirement_id"] = "sha256:" + "f" * 64
    else:
        beat["narrative_role"] = "unknown-role"
    raw = canonical_json_bytes(payload).decode("utf-8")
    members[0] = replace(members[0], payload_json=raw, content_hash=canonical_payload_hash(raw))
    with pytest.raises(ValueError):
        decode_editorial_business_members(tuple(members), contexts=contexts)


@pytest.mark.parametrize("mutation", ("expanded_duration", "teaser_strategy"))
def test_projection_preserves_proposal_duration_bounds_and_teaser_strategy(mutation: str) -> None:
    inputs, contexts, draft, _projection = editorial_case()
    story = draft.stories[0]
    proposal = inputs.portfolio.values.business.proposal_set.proposals[
        inputs.portfolio.values.business.portfolio.selections[0].proposal_index
    ].proposal
    if mutation == "expanded_duration":
        changed = replace(
            story,
            story_duration_seconds=type(story.story_duration_seconds)(
                proposal.target_duration_seconds.minimum,
                proposal.target_duration_seconds.minimum,
                proposal.target_duration_seconds.maximum + 1,
            ),
        )
    else:
        changed = replace(story, teaser_intent=replace(story.teaser_intent, strategy="foreign-strategy"))
    with pytest.raises(EditorialBlueprintError):
        project_editorial_blueprints(
            inputs.narrative.values, inputs.portfolio.values,
            replace(draft, stories=(changed, *draft.stories[1:])),
            expected_input_binding_sha256=contexts.input_binding_sha256,
            strategy_version=contexts.policy.strategy,
        )
