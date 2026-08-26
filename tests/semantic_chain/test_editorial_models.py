"""Synthetic intent values; no fixture here establishes committed authority."""

from dataclasses import FrozenInstanceError, replace

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_hash
from autocut_kernel.media.types import TimeBase
from autocut_kernel.semantic_chain.editorial_models import (
    Adjacent,
    DurationRange,
    EditingIntent,
    EditorialBeatDraft,
    EvidenceAlternative,
    EvidenceRequirementDraft,
    GapDuration,
    MaxGap,
    Precedes,
    SpanPolicy,
    StoryBlueprintDraft,
    TeaserIntent,
    decode_editorial_ordering,
)
from autocut_kernel.semantic_chain.member_refs import SemanticMemberIdentity, SemanticObjectRef
from autocut_kernel.semantic_chain.story_design_models import IntegerRange
from autocut_kernel.store.models import ArtifactScope
from autocut_kernel.vlm.models import VlmNarrativeFunction

SCOPE = ArtifactScope("semantic", "series", "synthetic-editorial")


def _ref(kind, object_type, object_id):
    member = SemanticMemberIdentity(kind, kind, 1, SCOPE, canonical_json_hash({"synthetic_kind": kind}))
    return SemanticObjectRef(member, object_type, object_id)


def _beat(index=0):
    event = _ref("event_card_set", "event", "event-1")
    candidate = _ref("candidate_catalog", "candidate", "candidate-1")
    return EditorialBeatDraft(
        "reveal", VlmNarrativeFunction.REVEAL, "真相揭示；保留原始事实。",
        (_ref("narrative_graph", "obligation", "obligation-1"),),
        (_ref("narrative_graph", "fact", "fact-1"),),
        (EvidenceRequirementDraft(f"material-{index}", "one_of", (
            EvidenceAlternative("alternative-1", (event,), (candidate,)),
        )),),
        (candidate,), SpanPolicy("scene", ("tight", "scene", "context"), ("scene", "context", "tight")),
        DurationRange(3, 4, 6),
    )


def _story(index=0):
    return StoryBlueprintDraft(
        canonical_json_hash({"synthetic_story": index}), _ref("proposal_set", "proposal", f"proposal-{index}"),
        (_beat(0), _beat(1)), (Precedes(0, 1), Adjacent(0, 1), MaxGap(0, 1, GapDuration(90000, TimeBase(1, 90000)))),
        DurationRange(6, 8, 12), EditingIntent("balanced", "high"), TeaserIntent("reveal", IntegerRange(1, 2)),
    )


def test_nested_models_roundtrip_fresh_mapping_and_immutability():
    story = _story()
    assert StoryBlueprintDraft.from_mapping(story.to_mapping()) == story
    values = [story, *story.beats, story.story_duration_seconds, story.editing_intent, story.teaser_intent,
              story.beats[0].span_policy, *story.beats[0].evidence_requirements,
              story.beats[0].evidence_requirements[0].alternative_sets[0], GapDuration(0, TimeBase(1, 1))]
    for value in values:
        assert type(value).from_mapping(value.to_mapping()) == value
    for value in story.ordering_constraints:
        assert decode_editorial_ordering(value.to_mapping()) == value
    mapping = story.to_mapping()
    mapping["beats"][0]["required_fact_refs"][0]["member_ref"]["scope"]["key"] = "changed"
    assert StoryBlueprintDraft.from_mapping(story.to_mapping()) == story
    assert story.canonical_hash == canonical_json_hash(story.to_mapping())
    with pytest.raises(FrozenInstanceError):
        story.beats = ()
    with pytest.raises(FrozenInstanceError):
        story.beats[0].summary = "rewritten"


@pytest.mark.parametrize("bad", [True, False, 1.0, "1", None, -1, 2**53])
def test_integer_durations_and_ordinals_never_coerce(bad):
    for factory in (
        lambda: DurationRange(bad, 2, 3), lambda: DurationRange(1, bad, 3),
        lambda: DurationRange(1, 2, bad), lambda: GapDuration(bad, TimeBase(1, 1)),
        lambda: Precedes(bad, 2), lambda: Adjacent(0, bad), lambda: MaxGap(0, bad, GapDuration(1, TimeBase(1, 1))),
    ):
        with pytest.raises(ValueError):
            factory()


def test_duration_range_order_reduced_clock_and_no_source_endpoint_fields():
    for values in ((0, 1, 2), (3, 2, 4), (1, 4, 3)):
        with pytest.raises(ValueError):
            DurationRange(*values)
    with pytest.raises(ValueError):
        GapDuration.from_mapping({"tick": 1, "time_base": {"num": 2, "den": 4}})
    with pytest.raises(ValueError):
        GapDuration(1, TimeBase(1, 2**53))
    for key in ("source_id", "start_pts", "end_pts", "clock_ref"):
        with pytest.raises(ValueError):
            GapDuration.from_mapping({**GapDuration(1, TimeBase(1, 2)).to_mapping(), key: "claimed"})
    assert GapDuration(0, TimeBase(1, 90000)).tick == 0


@pytest.mark.parametrize("changes", [
    {"preferred": "unknown"}, {"preferred": "scene", "allowed": ("tight",)},
    {"allowed": ("tight", "tight")}, {"allowed": []},
    {"fallback_order": ("scene", "tight")}, {"fallback_order": ("tight", "tight", "scene")},
])
def test_span_policy_is_closed_nonempty_and_exact_permutation(changes):
    with pytest.raises(ValueError):
        replace(_beat().span_policy, **changes)


def test_function_reuses_exact_v3_owner_and_is_not_narrative_role():
    for function in VlmNarrativeFunction:
        beat = replace(_beat(), narrative_function=function)
        assert EditorialBeatDraft.from_mapping(beat.to_mapping()).narrative_function is function
    for value in ("reveal", "hook_and_orient", "emotional_payoff", None, True):
        with pytest.raises(ValueError):
            replace(_beat(), narrative_function=value)
    for role in ("hook", "aftermath", "unknown", True):
        with pytest.raises(ValueError):
            replace(_beat(), narrative_role=role)
    # Role/Function are independent vocabularies, not automatic aliases.
    assert replace(_beat(), narrative_role="consequence", narrative_function=VlmNarrativeFunction.AFTERMATH)


@pytest.mark.parametrize("field,kind,object_type", [
    ("required_fact_refs", "narrative_graph", "event"),
    ("required_obligation_refs", "event_card_set", "obligation"),
    ("candidate_preferences", "vlm_semantic_pack", "candidate"),
])
def test_exact_owner_object_pairs_are_required(field, kind, object_type):
    with pytest.raises(ValueError):
        replace(_beat(), **{field: (_ref(kind, object_type, "wrong"),)})


def test_event_is_canonical_card_not_graph_alias_and_catalog_is_not_raw_vlm():
    alternative = _beat().evidence_requirements[0].alternative_sets[0]
    for field, ref in (
        ("event_refs", _ref("narrative_graph", "event", "event-1")),
        ("candidate_refs", _ref("vlm_semantic_pack", "candidate", "candidate-1")),
    ):
        with pytest.raises(ValueError):
            replace(alternative, **{field: (ref,)})


def test_owner_revision_scope_and_hash_drift_cannot_hide_under_same_object_id():
    beat = _beat()
    original = beat.required_fact_refs[0]
    for changes in ({"revision": 2}, {"content_hash": "sha256:" + "f" * 64},
                    {"scope": ArtifactScope("semantic", "series", "foreign")}):
        foreign = replace(original, member_ref=replace(original.member_ref, **changes))
        with pytest.raises(ValueError, match="scope|owner"):
            replace(beat, required_fact_refs=(foreign,))


def test_empty_duplicate_mutable_and_untyped_collections_are_rejected():
    beat = _beat()
    alternative = beat.evidence_requirements[0].alternative_sets[0]
    cases = (
        lambda: replace(alternative, event_refs=()), lambda: replace(alternative, candidate_refs=[]),
        lambda: replace(alternative, event_refs=(*alternative.event_refs, *alternative.event_refs)),
        lambda: replace(beat, required_fact_refs=(beat.required_fact_refs[0].to_mapping(),)),
        lambda: replace(beat, evidence_requirements=()),
        lambda: replace(beat, evidence_requirements=beat.evidence_requirements * 2),
        lambda: replace(beat, candidate_preferences=beat.candidate_preferences * 2),
        lambda: replace(beat, span_policy=beat.span_policy.to_mapping()),
        lambda: replace(beat, duration_seconds=beat.duration_seconds.to_mapping()),
        lambda: replace(_story(), beats=()), lambda: replace(_story(), beats=list(_story().beats)),
    )
    for factory in cases:
        with pytest.raises(ValueError):
            factory()


def test_alternative_ids_and_candidate_preferences_are_not_silently_repaired():
    req = _beat().evidence_requirements[0]
    with pytest.raises(ValueError):
        replace(req, alternative_sets=req.alternative_sets * 2)
    with pytest.raises(ValueError):
        replace(req, satisfaction="any")
    with pytest.raises(ValueError):
        replace(_beat(), candidate_preferences=(_ref("candidate_catalog", "candidate", "unknown"),))
    assert replace(_beat(), candidate_preferences=()).candidate_preferences == ()


def test_ordering_closed_union_local_ordinal_range_self_edges_and_duplicates():
    story = _story()
    for constraint in (Precedes(0, 2), Adjacent(1, 2), MaxGap(0, 2, GapDuration(0, TimeBase(1, 1)))):
        with pytest.raises(ValueError):
            replace(story, ordering_constraints=(constraint,))
    for factory in (lambda: Precedes(0, 0), lambda: Adjacent(1, 1),
                    lambda: MaxGap(0, 0, GapDuration(1, TimeBase(1, 1)))):
        with pytest.raises(ValueError):
            factory()
    for value in ({"constraint_type": "soft"}, {"constraint_type": "precedes", "before_beat_id": "0", "after_beat_id": "1"},
                  {**Precedes(0, 1).to_mapping(), "maximum_gap": GapDuration(1, TimeBase(1, 1)).to_mapping()}):
        with pytest.raises(ValueError):
            decode_editorial_ordering(value)
    with pytest.raises(ValueError):
        replace(story, ordering_constraints=(Precedes(0, 1), Precedes(0, 1)))
    # Shape is not a feasibility claim: the independent evaluator owns cycles.
    assert replace(story, ordering_constraints=(Precedes(0, 1), Precedes(1, 0)))


def test_same_summary_different_ordinals_remain_distinct_and_no_material_id_repeated():
    story = _story()
    assert story.beats[0].summary == story.beats[1].summary
    assert story.beats[0] != story.beats[1]
    assert "beat_id" not in story.beats[0].to_mapping()
    with pytest.raises(ValueError):
        replace(story, beats=(story.beats[0], story.beats[0]))


@pytest.mark.parametrize("text", ["", "   ", "\ud800", True, None])
def test_invalid_text_rejected_in_direct_construction(text):
    with pytest.raises(ValueError):
        replace(_beat(), summary=text)


def test_each_nested_mapping_rejects_unknown_and_missing_keys():
    story = _story()
    values = [story, story.beats[0], story.beats[0].evidence_requirements[0],
              story.beats[0].evidence_requirements[0].alternative_sets[0], story.beats[0].span_policy,
              story.story_duration_seconds, story.editing_intent, story.teaser_intent, GapDuration(1, TimeBase(1, 1))]
    for value in values:
        mapping = value.to_mapping()
        with pytest.raises(ValueError):
            type(value).from_mapping({**mapping, "pass": True})
        for key in mapping:
            with pytest.raises(ValueError):
                type(value).from_mapping({name: item for name, item in mapping.items() if name != key})
