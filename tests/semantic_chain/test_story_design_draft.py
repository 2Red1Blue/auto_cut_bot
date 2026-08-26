"""Synthetic draft boundary probes; hashes here do not prove committed inputs."""

import json
from dataclasses import FrozenInstanceError, replace

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes
from autocut_kernel.semantic_chain.member_refs import SemanticObjectRef
from autocut_kernel.semantic_chain.story_design_draft import (
    STORY_DESIGN_DRAFT_SCHEMA_VERSION,
    ProposalDraftSet,
    StoryDesignDraftError,
    StoryDesignDraftPolicy,
    decode_story_design_draft,
    story_design_draft_response_schema,
)
from autocut_kernel.semantic_chain.story_design_models import ProposalDraft
from jsonschema import Draft202012Validator

from tests.semantic_chain.test_story_design_models import GRAPH, _proposal

BINDING = "sha256:" + "c" * 64
POLICY = StoryDesignDraftPolicy(
    max_response_bytes=256_000, max_json_depth=16, max_proposals=4,
    max_material_requirements_per_proposal=4, max_total_material_requirements=8,
    max_references_per_field=16, max_total_references=128, max_genre_tags=4,
    max_text_characters=5000, max_total_text_characters=100_000,
)


def _draft():
    return ProposalDraftSet(BINDING, (_proposal(),))


def _raw(draft=None):
    return canonical_json_bytes((draft or _draft()).to_mapping())


def _decode(raw, policy=POLICY):
    return decode_story_design_draft(raw, expected_input_binding_sha256=BINDING, policy=policy)


def test_unicode_roundtrip_and_canonical_determinism_preserves_proposal_order():
    draft = ProposalDraftSet(BINDING, (_proposal("z-first"), _proposal("a-second")))
    variants = (
        _raw(draft), json.dumps(draft.to_mapping(), ensure_ascii=True, indent=2).encode(),
        json.dumps(dict(reversed(tuple(draft.to_mapping().items()))), ensure_ascii=False).encode(),
    )
    for raw in variants:
        decoded = _decode(raw)
        assert decoded == draft
        assert decoded.canonical_hash == draft.canonical_hash
        assert tuple(item.proposal_id for item in decoded.proposals) == ("z-first", "a-second")
    assert ProposalDraftSet(BINDING, tuple(reversed(draft.proposals))).canonical_hash != draft.canonical_hash
    assert _raw(draft).decode().find("发现") >= 0


def test_empty_proposals_are_retained_for_compiler_count_failure_not_admitted():
    empty = ProposalDraftSet(BINDING, ())
    assert _decode(_raw(empty)) == empty
    assert set(empty.to_mapping()) == {"schema_version", "input_binding_sha256", "proposals"}


def test_limits_have_no_defaults_and_are_closed_frozen_and_hash_bound():
    assert StoryDesignDraftPolicy.from_mapping(POLICY.to_mapping()) == POLICY
    with pytest.raises(TypeError):
        StoryDesignDraftPolicy()
    for key, value in POLICY.to_mapping().items():
        changed = replace(POLICY, **{key: value + 1})
        assert changed.canonical_hash != POLICY.canonical_hash
        incomplete = POLICY.to_mapping()
        del incomplete[key]
        with pytest.raises(ValueError):
            StoryDesignDraftPolicy.from_mapping(incomplete)
        for bad in (0, -1, True, False, 1.0, "1", None, 2**53):
            with pytest.raises(ValueError):
                replace(POLICY, **{key: bad})
    with pytest.raises(ValueError):
        StoryDesignDraftPolicy.from_mapping({**POLICY.to_mapping(), "default_tokens": 2048})
    with pytest.raises(FrozenInstanceError):
        POLICY.max_response_bytes = 1


def test_limits_have_implementation_ceilings_not_unbounded_safe_integers():
    ceilings = {
        "max_response_bytes": 16 * 1024 * 1024, "max_json_depth": 64,
        "max_proposals": 256, "max_material_requirements_per_proposal": 128,
        "max_total_material_requirements": 1024, "max_references_per_field": 1024,
        "max_total_references": 8192, "max_genre_tags": 64,
        "max_text_characters": 64 * 1024, "max_total_text_characters": 2 * 1024 * 1024,
    }
    maximum = StoryDesignDraftPolicy.from_mapping(ceilings)
    assert maximum.to_mapping() == ceilings
    for key, limit in ceilings.items():
        with pytest.raises(ValueError):
            replace(maximum, **{key: limit + 1})
    with pytest.raises(ValueError):
        replace(POLICY, max_total_text_characters=10)


@pytest.mark.parametrize("raw", [
    b"", b"\xff", b"{} trailing", b"null", b"[]", b"true",
    b'{"x":1,"x":2}', b'{"x":{"nested":1,"nested":2}}',
    b'{"x":NaN}', b'{"x":Infinity}', b'{"x":-Infinity}',
    b'{"x":1.0}', b'{"x":1e0}', b'{"x":9007199254740992}',
    b'{"x":"\\ud800"}',
])
def test_strict_json_rejects_duplicates_nonfinite_floats_unsafe_ints_and_utf8(raw):
    with pytest.raises(StoryDesignDraftError):
        _decode(raw)


def test_duplicate_nested_real_fields_are_rejected_before_typed_decoder():
    raw = _raw().replace(b'"minimum_usable_seconds":12',
                         b'"minimum_usable_seconds":12,"minimum_usable_seconds":13')
    with pytest.raises(StoryDesignDraftError):
        _decode(raw)
    raw = _raw().replace(b'"input_binding_sha256":', b'"schema_version":"forged","input_binding_sha256":')
    with pytest.raises(StoryDesignDraftError):
        _decode(raw)


@pytest.mark.parametrize("bad", [True, 12.0, "12", 0, -1, 2**53])
def test_wire_integer_fields_are_not_coerced(bad):
    data = _draft().to_mapping()
    data["proposals"][0]["material_requirements"][0]["minimum_usable_seconds"] = bad
    with pytest.raises(StoryDesignDraftError):
        _decode(json.dumps(data).encode())


@pytest.mark.parametrize("key", ["story_id", "pass", "material_support", "selected", "rule_results", "tainted_by"])
def test_producer_cannot_supply_compiler_or_acceptance_fields_at_any_level(key):
    for level in ("root", "proposal", "requirement", "physical", "source_constraints"):
        data = _draft().to_mapping()
        proposal = data["proposals"][0]
        requirement = proposal["material_requirements"][0]
        target = {
            "root": data, "proposal": proposal, "requirement": requirement,
            "physical": requirement["physical_requirements"][0],
            "source_constraints": requirement["source_constraints"],
        }[level]
        target[key] = "claimed"
        with pytest.raises(StoryDesignDraftError):
            _decode(json.dumps(data).encode())


def test_exact_input_binding_and_version_are_required_not_self_claims():
    for binding in ("sha256:" + "d" * 64, "sha256:" + "0" * 64, "sha256:" + "C" * 64, True):
        with pytest.raises(StoryDesignDraftError):
            decode_story_design_draft(_raw(), expected_input_binding_sha256=binding, policy=POLICY)
    for changed in (
        {**_draft().to_mapping(), "schema_version": "stage2-story-design-draft-v0"},
        {**_draft().to_mapping(), "input_binding_sha256": "sha256:" + "d" * 64},
    ):
        with pytest.raises(StoryDesignDraftError):
            _decode(json.dumps(changed).encode())
    with pytest.raises(StoryDesignDraftError):
        decode_story_design_draft(_raw(), expected_input_binding_sha256=BINDING, policy={})


def test_byte_bound_and_depth_are_checked_before_shared_recursive_parser(monkeypatch):
    import autocut_kernel.semantic_chain.story_design_draft as owner

    def forbidden(*args, **kwargs):
        raise AssertionError("parser must not run past the byte/depth guard")

    monkeypatch.setattr(owner, "load_canonical_json_bytes", forbidden)
    with pytest.raises(StoryDesignDraftError, match="byte"):
        _decode(_raw(), replace(POLICY, max_response_bytes=len(_raw()) - 1))
    with pytest.raises(StoryDesignDraftError, match="depth"):
        _decode(b"[" * 17 + b"0" + b"]" * 17)


def test_depth_guard_does_not_count_brackets_or_escaped_quotes_inside_strings():
    proposal = replace(_proposal(), title='[\\\"{]' * 100)
    draft = ProposalDraftSet(BINDING, (proposal,))
    assert _decode(_raw(draft)) == draft
    assert _decode(_raw(), replace(POLICY, max_response_bytes=len(_raw()))) == _draft()


def test_every_count_and_text_limit_rejects_without_truncation():
    proposal = _proposal()
    second_fact = SemanticObjectRef(GRAPH, "fact", "fact-two")
    requirement = proposal.material_requirements[0]
    cases = (
        (ProposalDraftSet(BINDING, (proposal, _proposal("second"))), {"max_proposals": 1}),
        (ProposalDraftSet(BINDING, (proposal, _proposal("second"))), {"max_total_material_requirements": 1}),
        (ProposalDraftSet(BINDING, (replace(proposal, material_requirements=(
            requirement, replace(requirement, requirement_id="second"),
        )),)), {"max_material_requirements_per_proposal": 1}),
        (ProposalDraftSet(BINDING, (replace(proposal, required_fact_refs=(
            *proposal.required_fact_refs, second_fact,
        )),)), {"max_references_per_field": 1}),
        (_draft(), {"max_total_references": 4}),
        (ProposalDraftSet(BINDING, (replace(proposal, genre_tags=("suspense", "growth")),)),
         {"max_genre_tags": 1}),
        (ProposalDraftSet(BINDING, (replace(proposal, title="x" * 257),)),
         {"max_text_characters": 256}),
        (_draft(), {"max_text_characters": 256, "max_total_text_characters": 256}),
    )
    for draft, changes in cases:
        with pytest.raises(StoryDesignDraftError):
            _decode(_raw(draft), replace(POLICY, **changes))


def test_proposal_set_rejects_duplicate_ids_mixed_owners_and_mutable_inputs():
    with pytest.raises(ValueError):
        ProposalDraftSet(BINDING, (_proposal(), replace(_proposal(), title="changed")))
    with pytest.raises(ValueError):
        ProposalDraftSet(BINDING, [_proposal()])
    with pytest.raises(ValueError):
        ProposalDraftSet(BINDING, ({},))
    mapping = _proposal("foreign").to_mapping()

    def rewrite(value):
        if isinstance(value, list):
            for item in value:
                rewrite(item)
        elif isinstance(value, dict):
            if value.get("artifact_type") == "narrative_graph":
                value["revision"] = 2
            for item in value.values():
                rewrite(item)

    rewrite(mapping)
    foreign = ProposalDraft.from_mapping(mapping)
    with pytest.raises(ValueError, match="owners"):
        ProposalDraftSet(BINDING, (_proposal(), foreign))


def test_wire_uses_exact_semantic_references_not_bare_ids_or_legacy_artifact_uuid():
    for bad in ("fact-one", {"artifact_id": "uuid", "object_type": "fact", "object_id": "fact-one"}):
        mapping = _draft().to_mapping()
        mapping["proposals"][0]["required_fact_refs"] = [bad]
        with pytest.raises(StoryDesignDraftError):
            _decode(json.dumps(mapping).encode())


def test_schema_is_closed_bounded_fresh_and_accepts_actual_typed_wire():
    schema = story_design_draft_response_schema(POLICY)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    validator.validate(_draft().to_mapping())
    root_props = schema["properties"]
    assert root_props["schema_version"]["const"] == STORY_DESIGN_DRAFT_SCHEMA_VERSION
    assert root_props["proposals"]["maxItems"] == POLICY.max_proposals
    proposal = root_props["proposals"]["items"]["properties"]
    assert proposal["title"]["maxLength"] == POLICY.max_text_characters
    assert proposal["material_requirements"]["maxItems"] == POLICY.max_material_requirements_per_proposal
    for key in ("ready", "story_id", "rule_results"):
        mapping = _draft().to_mapping()
        mapping["proposals"][0][key] = True
        assert list(validator.iter_errors(mapping))
    for kind, mode in (("unknown", "complete"), ("dialogue_integrity", "pass")):
        mapping = _draft().to_mapping()
        physical = mapping["proposals"][0]["material_requirements"][0]["physical_requirements"][0]
        physical.update(requirement_kind=kind, mode=mode)
        assert list(validator.iter_errors(mapping))
    proposal["title"]["maxLength"] = 1
    assert story_design_draft_response_schema(POLICY)["properties"]["proposals"]["items"]["properties"]["title"]["maxLength"] == POLICY.max_text_characters


def test_deep_immutability_of_draft_does_not_make_it_authority():
    draft = _draft()
    with pytest.raises(FrozenInstanceError):
        draft.input_binding_sha256 = "sha256:" + "d" * 64
    mapping = draft.to_mapping()
    mapping["proposals"][0]["material_requirements"].clear()
    assert len(draft.proposals[0].material_requirements) == 1
    assert not hasattr(draft, "accepted")
    assert not hasattr(draft, "story_id")
