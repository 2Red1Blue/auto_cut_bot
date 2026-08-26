"""Synthetic closed Stage 3 intent; passing parsing is not Story admission."""

import json
from dataclasses import FrozenInstanceError, replace

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes, canonical_json_hash
from autocut_kernel.semantic_chain import editorial_draft as owner
from autocut_kernel.semantic_chain.editorial_draft import (
    EDITORIAL_DRAFT_SCHEMA_VERSION,
    EditorialBlueprintDraft,
    EditorialDraftError,
    EditorialDraftPolicy,
    decode_editorial_draft,
    editorial_draft_response_schema,
)
from autocut_kernel.semantic_chain.editorial_models import EvidenceAlternative, Precedes
from jsonschema import Draft202012Validator

from tests.semantic_chain.test_editorial_models import _ref, _story

BINDING = "sha256:" + "b" * 64
POLICY = EditorialDraftPolicy(
    budget_unit="bytes", max_response_bytes=256_000, max_json_depth=24,
    max_stories=4, max_beats_per_story=8, max_total_beats=16,
    max_requirements_per_beat=8, max_total_requirements=32,
    max_alternatives_per_requirement=8, max_total_alternatives=64,
    max_references_per_field=16, max_total_references=256,
    max_ordering_constraints_per_story=16, max_total_ordering_constraints=32,
    max_text_characters=5000, max_total_text_characters=100_000,
)


def _draft():
    return EditorialBlueprintDraft(BINDING, (_story(0), _story(1)))


def _raw(value=None):
    return canonical_json_bytes((value or _draft()).to_mapping())


def _decode(raw, policy=POLICY, targets=None):
    return decode_editorial_draft(raw, expected_input_binding_sha256=BINDING, policy=policy,
                                  expected_target_story_ids=targets or tuple(story.story_id for story in _draft().stories))


def test_multistory_unicode_roundtrip_order_and_canonical_hash():
    draft = _draft()
    for raw in (_raw(), json.dumps(draft.to_mapping(), ensure_ascii=True, indent=2).encode(),
                json.dumps(dict(reversed(tuple(draft.to_mapping().items()))), ensure_ascii=False).encode()):
        decoded = _decode(raw)
        assert decoded == draft
        assert decoded.canonical_hash == canonical_json_hash(draft.to_mapping())
        assert tuple(story.story_id for story in decoded.stories) == tuple(story.story_id for story in draft.stories)
        assert decoded.stories[0].beats[0].summary == decoded.stories[0].beats[1].summary
    assert EditorialBlueprintDraft(BINDING, tuple(reversed(draft.stories))).canonical_hash != draft.canonical_hash
    assert "真相" in _raw().decode()


def test_policy_closed_explicit_frozen_and_all_limits_hash_bound():
    assert EditorialDraftPolicy.from_mapping(POLICY.to_mapping()) == POLICY
    with pytest.raises(TypeError):
        EditorialDraftPolicy()
    for key, value in POLICY.to_mapping().items():
        missing = POLICY.to_mapping()
        del missing[key]
        with pytest.raises(ValueError):
            EditorialDraftPolicy.from_mapping(missing)
        if key == "budget_unit":
            continue
        assert replace(POLICY, **{key: value + 1}).canonical_hash != POLICY.canonical_hash
        for bad in (0, -1, True, False, 1.0, "1", None, 2**53):
            with pytest.raises(ValueError):
                replace(POLICY, **{key: bad})
    for unit in ("tokens", "characters", "cost", None, True):
        with pytest.raises(ValueError):
            replace(POLICY, budget_unit=unit)
    with pytest.raises(ValueError):
        EditorialDraftPolicy.from_mapping({**POLICY.to_mapping(), "tokenizer_id": "fake"})
    with pytest.raises(FrozenInstanceError):
        POLICY.max_response_bytes = 1
    with pytest.raises(FrozenInstanceError):
        _draft().stories = ()


def test_implementation_ceilings_are_not_unbounded_safe_int_defaults():
    ceilings = {
        "max_response_bytes": 16 * 1024 * 1024, "max_json_depth": 64,
        "max_stories": 128, "max_beats_per_story": 128, "max_total_beats": 1024,
        "max_requirements_per_beat": 64, "max_total_requirements": 4096,
        "max_alternatives_per_requirement": 128, "max_total_alternatives": 8192,
        "max_references_per_field": 1024, "max_total_references": 65536,
        "max_ordering_constraints_per_story": 1024, "max_total_ordering_constraints": 8192,
        "max_text_characters": 65536, "max_total_text_characters": 4 * 1024 * 1024,
    }
    maximum = EditorialDraftPolicy.from_mapping({"budget_unit": "bytes", **ceilings})
    for name, bound in ceilings.items():
        with pytest.raises(ValueError):
            replace(maximum, **{name: bound + 1})
    with pytest.raises(ValueError):
        replace(POLICY, max_total_text_characters=1)


@pytest.mark.parametrize("raw", [
    b"", b"\xff", b"{} trailing", b"null", b"[]", b"true",
    b'{"x":1,"x":2}', b'{"x":{"a":1,"a":2}}',
    b'{"x":NaN}', b'{"x":Infinity}', b'{"x":-Infinity}',
    b'{"x":1.0}', b'{"x":1e0}', b'{"x":9007199254740992}', b'{"x":"\\ud800"}',
])
def test_strict_json_rejects_ambiguity_floats_nonfinite_unsafe_integer_and_invalid_utf8(raw):
    with pytest.raises(EditorialDraftError):
        _decode(raw)


def test_duplicate_real_fields_are_rejected_at_every_depth():
    for token, duplicate in (
        (b'"input_binding_sha256":', b'"input_binding_sha256":"foreign","input_binding_sha256":'),
        (b'"tick":90000', b'"tick":90000,"tick":1'),
    ):
        raw = _raw().replace(token, duplicate, 1)
        assert raw != _raw()
        with pytest.raises(EditorialDraftError):
            _decode(raw)


@pytest.mark.parametrize("bad", [True, False, 1.0, "1", -1, None, 2**53])
def test_wire_integer_fields_never_coerce(bad):
    for where, field in (("duration", "min"), ("ordering", "before_ordinal"), ("gap", "tick"), ("clock", "den")):
        mapping = _draft().to_mapping()
        story = mapping["stories"][0]
        target = {"duration": story["beats"][0]["duration_seconds"], "ordering": story["ordering_constraints"][0],
                  "gap": story["ordering_constraints"][2]["maximum_gap"],
                  "clock": story["ordering_constraints"][2]["maximum_gap"]["time_base"]}[where]
        target[field] = bad
        with pytest.raises(EditorialDraftError):
            _decode(json.dumps(mapping).encode())


@pytest.mark.parametrize("field", ["artifact_id", "blueprint_beat_id", "ordinal", "start_pts", "end_pts",
                                    "physical_requirements", "physical_requirements_hash", "required_candidates",
                                    "transcript", "vad", "fulfilled", "pass", "rule_results"])
def test_model_cannot_supply_ids_physical_evidence_or_acceptance_at_any_level(field):
    for level in ("root", "story", "beat", "requirement", "alternative"):
        mapping = _draft().to_mapping()
        story = mapping["stories"][0]
        beat = story["beats"][0]
        requirement = beat["evidence_requirements"][0]
        target = {"root": mapping, "story": story, "beat": beat, "requirement": requirement,
                  "alternative": requirement["alternative_sets"][0]}[level]
        target[field] = "untrusted"
        with pytest.raises(EditorialDraftError):
            _decode(json.dumps(mapping).encode())


def test_exact_complete_target_order_cannot_be_skipped_reordered_duplicated_or_forged():
    draft = _draft()
    for stories in ((), draft.stories[:1], tuple(reversed(draft.stories)), draft.stories * 2,
                    (*draft.stories, _story(2))):
        mapping = {**draft.to_mapping(), "stories": [story.to_mapping() for story in stories]}
        with pytest.raises(ValueError):
            _decode(json.dumps(mapping).encode())
    for expected in ((), (draft.stories[0].story_id,) * 2, list(story.story_id for story in draft.stories),
                     ("not-a-hash",), ("sha256:" + "0" * 64,)):
        with pytest.raises(ValueError):
            decode_editorial_draft(_raw(), expected_input_binding_sha256=BINDING,
                                   expected_target_story_ids=expected, policy=POLICY)


def test_binding_version_policy_and_typed_batch_are_not_caller_pass_claims():
    for binding in ("sha256:" + "f" * 64, "sha256:" + "0" * 64, "sha256:" + "B" * 64, True):
        with pytest.raises(ValueError):
            decode_editorial_draft(_raw(), expected_input_binding_sha256=binding,
                                   expected_target_story_ids=tuple(story.story_id for story in _draft().stories), policy=POLICY)
    for changes in ({"schema_version": "stage3-editorial-blueprint-draft-v0"},
                    {"input_binding_sha256": "sha256:" + "f" * 64}):
        with pytest.raises(ValueError):
            _decode(json.dumps({**_draft().to_mapping(), **changes}).encode())
    with pytest.raises(ValueError):
        _decode(_raw(), policy={})
    with pytest.raises(ValueError):
        EditorialBlueprintDraft(BINDING, list(_draft().stories))
    with pytest.raises(ValueError):
        EditorialBlueprintDraft(BINDING, (_story(0), replace(_story(1), proposal_ref=_story(0).proposal_ref)))


def test_byte_and_depth_checks_precede_shared_json_decoder(monkeypatch):
    def forbidden(*args, **kwargs):
        raise RuntimeError("must not parse a response beyond byte/depth ceiling")

    monkeypatch.setattr(owner, "load_canonical_json_bytes", forbidden)
    with pytest.raises(EditorialDraftError, match="byte"):
        _decode(_raw(), replace(POLICY, max_response_bytes=len(_raw()) - 1))
    with pytest.raises(EditorialDraftError, match="depth"):
        _decode(b"[" * 25 + b"0" + b"]" * 25)


def test_exact_utf8_byte_limit_and_quoted_brackets_are_not_tokens_or_depth():
    story = _story(0)
    story = replace(story, beats=(replace(story.beats[0], summary='[\\\"{]剧情' * 60), story.beats[1]))
    draft = EditorialBlueprintDraft(BINDING, (story, _story(1)))
    raw = _raw(draft)
    assert len(raw) > len(raw.decode())
    assert _decode(raw, replace(POLICY, max_response_bytes=len(raw))) == draft
    with pytest.raises(EditorialDraftError, match="byte"):
        _decode(raw, replace(POLICY, max_response_bytes=len(raw.decode())))


def test_all_per_item_and_batch_count_limits_fail_without_truncating():
    draft = _draft()
    story = draft.stories[0]
    beat = story.beats[0]
    req = beat.evidence_requirements[0]
    alt = req.alternative_sets[0]
    multiple_req = replace(beat, evidence_requirements=(req, replace(req, source_material_requirement_id="extra")))
    multiple_alt = replace(beat, evidence_requirements=(replace(req, alternative_sets=(
        alt, replace(alt, alternative_id="second"),
    )),))
    multiple_refs = replace(beat, required_fact_refs=(*beat.required_fact_refs, _ref("narrative_graph", "fact", "fact-2")))
    cases = [
        (draft, {"max_stories": 1}), (draft, {"max_beats_per_story": 1}),
        (draft, {"max_total_beats": 3}), (draft, {"max_total_requirements": 3}),
        (draft, {"max_total_alternatives": 3}), (draft, {"max_total_references": 1}),
        (draft, {"max_ordering_constraints_per_story": 2}), (draft, {"max_total_ordering_constraints": 5}),
        (draft, {"max_text_characters": 70}),
        (draft, {"max_text_characters": 256, "max_total_text_characters": 256}),
    ]
    for replacement, changes in ((multiple_req, {"max_requirements_per_beat": 1}),
                                 (multiple_alt, {"max_alternatives_per_requirement": 1}),
                                 (multiple_refs, {"max_references_per_field": 1})):
        cases.append((replace(draft, stories=(replace(story, beats=(replacement, story.beats[1])), draft.stories[1])), changes))
    for value, changes in cases:
        with pytest.raises(ValueError):
            _decode(_raw(value), replace(POLICY, **changes))


def test_repeated_owner_references_count_every_occurrence_not_only_distinct_refs():
    mapping = _draft().to_mapping()

    def count(value):
        if isinstance(value, list):
            return sum(count(item) for item in value)
        if isinstance(value, dict):
            return int("member_ref" in value) + sum(count(item) for item in value.values())
        return 0

    total = count(mapping)
    assert total > len(set(ref for story in _draft().stories for ref in story.references))
    assert _decode(_raw(), replace(POLICY, max_total_references=total)) == _draft()
    with pytest.raises(EditorialDraftError, match="references"):
        _decode(_raw(), replace(POLICY, max_total_references=total - 1))


def test_schema_closed_fresh_bounded_actual_v3_and_same_target_membership():
    targets = tuple(story.story_id for story in _draft().stories)
    schema = editorial_draft_response_schema(POLICY, target_story_ids=targets)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    assert not list(validator.iter_errors(_draft().to_mapping()))
    assert schema["properties"]["stories"]["minItems"] == schema["properties"]["stories"]["maxItems"] == 2
    assert schema["properties"]["schema_version"]["const"] == EDITORIAL_DRAFT_SCHEMA_VERSION
    beat = schema["properties"]["stories"]["items"]["properties"]["beats"]["items"]
    assert beat["properties"]["narrative_function"]["enum"] == [
        "hook", "setup", "escalation", "confrontation", "reveal", "reversal", "payoff", "aftermath",
    ]
    for key in ("artifact_id", "physical_requirements", "fulfilled", "required_candidates"):
        mapping = _draft().to_mapping()
        mapping["stories"][0]["beats"][0][key] = "forbidden"
        assert list(validator.iter_errors(mapping))
    schema["properties"]["stories"]["maxItems"] = 99
    assert editorial_draft_response_schema(POLICY, target_story_ids=targets)["properties"]["stories"]["maxItems"] == 2
    # Schema establishes shape; actual Beat count, owner joins and full target
    # ordering remain independent decoder checks, not a false schema proof.
    mapping = _draft().to_mapping()
    mapping["stories"][0]["ordering_constraints"] = [Precedes(0, 7).to_mapping()]
    assert not list(validator.iter_errors(mapping))
    with pytest.raises(EditorialDraftError):
        _decode(json.dumps(mapping).encode())


def test_parser_does_not_assert_material_existence_capability_or_admission():
    draft = _draft()
    story = draft.stories[0]
    beat = story.beats[0]
    alternative = EvidenceAlternative("unresolved", (_ref("event_card_set", "event", "unknown-event"),),
                                       (_ref("candidate_catalog", "candidate", "unknown-candidate"),))
    requirement = replace(beat.evidence_requirements[0], source_material_requirement_id="unknown-material",
                          alternative_sets=(alternative,))
    changed = replace(beat, evidence_requirements=(requirement,), candidate_preferences=())
    draft = replace(draft, stories=(replace(story, beats=(changed, story.beats[1])), draft.stories[1]))
    assert _decode(_raw(draft)) == draft
    assert "admission" not in draft.to_mapping()
