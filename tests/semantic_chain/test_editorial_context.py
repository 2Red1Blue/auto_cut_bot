"""Full contexts from real Commands/readers over synthetic in-memory I/O.

No PostgreSQL, media provider or model is executed. Setup uses actual Source,
Stage 1/2 compilers and independent readers, not a self-asserted Admission.
"""

import json
from dataclasses import FrozenInstanceError, replace

import pytest
from autocut_kernel.contracts.compiler.canonical import (
    canonical_json_bytes,
    canonical_json_hash,
    sha256_bytes,
)
from autocut_kernel.pipeline.compile_story_portfolio_command import CompileStoryPortfolioCommand
from autocut_kernel.pipeline.editorial_blueprint_inputs import (
    read_committed_editorial_blueprint_inputs,
)
from autocut_kernel.semantic_chain import editorial_context as owner
from autocut_kernel.semantic_chain.editorial_context import (
    EditorialContextBatch,
    build_editorial_contexts,
)
from autocut_kernel.semantic_chain.editorial_context_models import (
    EditorialContextManifest,
    EditorialContextPolicy,
    EvidenceClosureSet,
    ExactContextMember,
    MaterialEvidenceClosure,
    StoryEditorialContext,
)
from autocut_kernel.semantic_chain.member_refs import SemanticMemberIdentity
from autocut_kernel.store.models import ArtifactScope, canonical_payload_hash
from autocut_kernel.vlm.models import VlmRequestIdentity, derive_vlm_global_id

from tests.semantic_chain.test_compile_story_portfolio_command import command_case

POLICY = EditorialContextPolicy("unpartitioned-batch-v1", "bytes", 2_000_000, 2_000_000, 100)


@pytest.fixture(scope="module")
def case():
    store, provider, request, _ = command_case(job_change={"selected_story_count": 2, "source_reuse_policy": "allow"})
    result = CompileStoryPortfolioCommand(store, provider).execute(request)
    assert result.outcome.state == "succeeded"
    inputs = read_committed_editorial_blueprint_inputs(store, stage2_request=request, stage2_outcome=result.outcome)
    return inputs, request, store, provider


def _build(case, **changes):
    inputs, request, _, _ = case
    args = {"semantic": inputs.semantic, "stage1": inputs.narrative.values, "stage2": inputs.portfolio.values,
            "policy": POLICY, "scope": request.artifact_scope, "revision": 1,
            "job_policy": request.job_policy, "story_policy": request.story_policy, "candidate_policy": request.candidate_policy}
    args.update(changes)
    return build_editorial_contexts(**args)


@pytest.fixture(scope="module")
def batch(case):
    return _build(case)


def _rewrite(member, payload):
    raw = canonical_json_bytes(payload).decode()
    return replace(member, payload_json=raw, content_hash=canonical_payload_hash(raw))


def test_complete_exact_pool_is_deduplicated_and_contains_all_raw_and_unselected_content(case, batch):
    inputs, request, _, _ = case
    expected = [inputs.semantic.source_manifest.payload_json]
    for item in inputs.semantic.inputs:
        expected.extend((item.semantic_pack.source_child.payload_json, item.semantic_pack.payload_json))
    expected.extend(member.payload_json for member in (*inputs.narrative.values.members, *inputs.portfolio.values.members))
    assert [json.loads(member.payload_json) for member in batch.member_pool] == [json.loads(raw) for raw in expected]
    assert len(batch.member_pool) == 1 + 2 * len(inputs.semantic.inputs) + 8 + 5
    payload = json.loads(batch.prompt_payload)
    assert len(payload["member_pool"]) == len(batch.member_pool)
    assert len(payload["stories"]) == 2
    assert payload["member_pool"][0]["payload"]["census"] == inputs.semantic.source_grant.to_mapping()
    assert payload["stage2_policies"] == {"job_policy": request.job_policy.to_mapping(),
                                           "story_policy": request.story_policy.to_mapping(),
                                           "candidate_policy": request.candidate_policy.to_mapping()}
    for story in payload["stories"]:
        assert "member_pool" not in story
        assert "payload" not in story["closure_member"]["payload"]["member_refs"][0]
    # Every candidate, raw Fact and Graph state/dependency node remains exact;
    # membership is not reduced to selected-candidate measurements/summaries.
    pool = {item.member_ref.artifact_type: json.loads(item.payload_json) for item in batch.member_pool}
    assert pool["narrative_graph"] == inputs.narrative.values.coverage.narrative_graph.to_mapping()
    assert pool["candidate_catalog"] == inputs.portfolio.values.business.candidate_catalog.to_mapping()
    assert pool["proposal_set"] == inputs.portfolio.values.business.proposal_set.to_mapping()


def test_manifests_match_independent_expanded_content_hash_and_bytes_without_self_reference(batch):
    wire = json.loads(batch.prompt_payload)
    for story in batch.stories:
        content = {"schema_version": "stage3-story-context-content-v1", "member_pool": wire["member_pool"],
                   "stage2_policies": wire["stage2_policies"], "closure": story.closure.to_mapping()}
        raw = canonical_json_bytes(content)
        assert story.manifest.context_content_sha256 == sha256_bytes(raw)
        assert story.manifest.context_byte_length == len(raw)
        assert story.manifest.context_policy_sha256 == POLICY.canonical_hash
        assert story.manifest.closure_set_ref == SemanticMemberIdentity.from_artifact_member(story.closure_member)
        assert "context_manifest" not in raw.decode()
        assert "request_hash" not in story.manifest.to_mapping()
        assert story.closure_member.logical_id == f"evidence_closure_set@{story.story_id}"
        assert story.context_member.logical_id == f"context_manifest@{story.story_id}"
    assert len(batch.prompt_payload) < sum(story.manifest.context_byte_length for story in batch.stories)


def test_binding_has_independent_domain_hash_oracle_and_full_target_order(case, batch):
    expected = {"schema_version": "stage3-editorial-input-binding-v1",
                "member_refs": [member.member_ref.to_mapping() for member in batch.member_pool],
                "context_policy_sha256": POLICY.canonical_hash,
                "job_policy_sha256": batch.job_policy.canonical_hash,
                "story_policy_sha256": batch.story_policy.canonical_hash,
                "candidate_policy_sha256": batch.candidate_policy.canonical_hash,
                "target_story_ids": list(batch.target_story_ids)}
    assert batch.input_binding_sha256 == canonical_json_hash(expected)
    assert batch.target_story_ids == case[0].portfolio.values.admission.target_story_ids
    assert batch.target_story_ids == case[0].portfolio.values.business.source_usage_ledger.target_story_ids
    assert batch.canonical_hash == sha256_bytes(batch.prompt_payload)


def test_determinism_closed_roundtrip_fresh_mappings_and_immutability(case, batch):
    assert _build(case) == batch
    assert EditorialContextBatch.from_mapping(json.loads(batch.prompt_payload)) == batch
    values = [POLICY, batch, batch.member_pool[0], batch.stories[0], batch.stories[0].closure,
              batch.stories[0].manifest, batch.stories[0].closure.requirements[0]]
    for value in values:
        assert type(value).from_mapping(value.to_mapping()) == value
        assert value.canonical_hash == canonical_json_hash(value.to_mapping())
        mapping = value.to_mapping()
        with pytest.raises(ValueError):
            type(value).from_mapping({**mapping, "admitted": True})
        for key in mapping:
            with pytest.raises(ValueError):
                type(value).from_mapping({name: item for name, item in mapping.items() if name != key})
    mapping = batch.to_mapping()
    mapping["member_pool"][0]["payload"]["census"]["series_id"] = "mutated"
    assert batch.to_mapping() != mapping
    with pytest.raises(FrozenInstanceError):
        batch.stories = ()
    with pytest.raises(FrozenInstanceError):
        batch.member_pool[0].payload_json = "{}"


def test_material_rows_cover_exact_selected_requirements_and_full_obligation_facts(case, batch):
    stage1, stage2 = case[0].narrative.values, case[0].portfolio.values
    nodes = {node.node_id: node for node in stage1.coverage.narrative_graph.nodes}
    for story, selection in zip(batch.stories, stage2.business.portfolio.selections, strict=True):
        proposal = stage2.business.proposal_set.proposals[selection.proposal_index].proposal
        assert story.closure.proposal_ref == selection.proposal_ref
        assert tuple(row.source_material_requirement_id for row in story.closure.requirements) == tuple(
            row.requirement_id for row in proposal.material_requirements
        )
        assert story.closure.member_refs == tuple(member.member_ref for member in batch.member_pool)
        for row in story.closure.requirements:
            assert tuple(ref.object_id for ref in row.required_fact_refs) == nodes[row.obligation_ref.object_id].attributes.required_fact_ids


@pytest.mark.parametrize("field", ["max_story_context_bytes", "max_batch_context_bytes", "max_source_members"])
@pytest.mark.parametrize("bad", [0, -1, True, False, 1.0, "1", None, 2**53])
def test_explicit_positive_safe_policy_limits_never_coerce(field, bad):
    with pytest.raises(ValueError):
        replace(POLICY, **{field: bad})


def test_policy_has_no_defaults_partition_fallback_or_fake_token_budget():
    with pytest.raises(TypeError):
        EditorialContextPolicy()
    for changes in ({"strategy": "partitioned"}, {"strategy": "optional-pruning"}, {"budget_unit": "tokens"},
                    {"max_story_context_bytes": 64 * 1024 * 1024 + 1}, {"max_source_members": 8193}):
        with pytest.raises(ValueError):
            replace(POLICY, **changes)


def test_exact_byte_boundaries_and_complete_batch_overflow_are_not_truncation(case, batch):
    length = max(story.manifest.context_byte_length for story in batch.stories)
    exact = _build(case, policy=replace(POLICY, max_story_context_bytes=length))
    assert all(story.manifest.context_byte_length <= length for story in exact.stories)
    with pytest.raises(ValueError, match="byte"):
        _build(case, policy=replace(POLICY, max_story_context_bytes=length - 1))
    policy = replace(POLICY, max_batch_context_bytes=len(batch.prompt_payload))
    boundary = _build(case, policy=policy)
    policy = replace(policy, max_batch_context_bytes=len(boundary.prompt_payload))
    boundary = _build(case, policy=policy)
    assert len(boundary.prompt_payload) == policy.max_batch_context_bytes
    with pytest.raises(ValueError, match="byte"):
        _build(case, policy=replace(policy, max_batch_context_bytes=policy.max_batch_context_bytes - 1))
    assert _build(case, policy=replace(POLICY, max_source_members=len(batch.member_pool))).member_pool == batch.member_pool


def test_member_count_guard_precedes_projection_and_has_no_hidden_io(case, batch, monkeypatch):
    def forbidden(*args, **kwargs):
        raise RuntimeError("not allowed after bounded context input check")

    monkeypatch.setattr(owner, "project_candidate_catalog", forbidden)
    with pytest.raises(ValueError, match="member bound"):
        _build(case, policy=replace(POLICY, max_source_members=len(batch.member_pool) - 1))


def test_oversized_complete_pool_is_rejected_before_story_content_expansion(case, batch, monkeypatch):
    def forbidden(*args, **kwargs):
        raise RuntimeError("oversized pool must not expand once per Story")

    monkeypatch.setattr(owner, "_content", forbidden)
    lower_bound = sum(len(member.payload_json.encode()) for member in batch.member_pool)
    with pytest.raises(ValueError, match="pool exceeds"):
        _build(case, policy=replace(POLICY, max_batch_context_bytes=lower_bound - 1))


def test_builder_never_reads_store_generates_or_executes_upstream(case, batch, monkeypatch):
    store, provider = case[2:]

    def forbidden(*args, **kwargs):
        raise RuntimeError("pure context construction attempted I/O")

    for persistence in (store, store.predecessor):
        for method in ("read_committed_semantic_inputs", "read_committed_artifact_set", "read_immutable_blob",
                       "claim_command", "put_immutable_blob", "commit_generation_success"):
            monkeypatch.setattr(persistence, method, forbidden)
    monkeypatch.setattr(provider, "dispatch", forbidden)
    monkeypatch.setattr(provider, "reconcile", forbidden)
    monkeypatch.setattr(CompileStoryPortfolioCommand, "execute", forbidden)
    assert _build(case) == batch


@pytest.mark.parametrize("field", ["context_content_sha256", "context_byte_length", "context_policy_sha256"])
def test_rehashed_manifest_self_claim_cannot_replace_actual_complete_projection(batch, field):
    story = batch.stories[0]
    value = story.manifest.context_byte_length - 1 if field == "context_byte_length" else "sha256:" + "f" * 64
    manifest = replace(story.manifest, **{field: value})
    changed = replace(story, context_member=_rewrite(story.context_member, manifest.to_mapping()))
    with pytest.raises(ValueError, match="projection"):
        replace(batch, stories=(changed, batch.stories[1]))


def test_rehashed_requirement_omission_is_not_a_full_closure(batch):
    story = batch.stories[0]
    requirement = story.closure.requirements[0]
    changed = replace(story.closure, requirements=(replace(requirement, source_material_requirement_id="foreign-material"),))
    closure_member = _rewrite(story.closure_member, changed.to_mapping())
    manifest = replace(story.manifest, closure_set_ref=SemanticMemberIdentity.from_artifact_member(closure_member))
    changed_story = replace(story, closure_member=closure_member, context_member=_rewrite(story.context_member, manifest.to_mapping()))
    with pytest.raises(ValueError, match="mandatory"):
        replace(batch, stories=(changed_story, batch.stories[1]))


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "reorder", "target_omit", "target_reorder", "target_duplicate"])
def test_complete_pool_and_frozen_target_collections_are_not_partially_accepted(batch, mutation):
    pool, stories = batch.member_pool, batch.stories
    if mutation == "missing":
        pool = pool[1:]
    elif mutation == "duplicate":
        pool = (pool[0], pool[1], pool[2], pool[1], pool[2], *pool[3:])
    elif mutation == "reorder":
        pool = (pool[0], pool[2], pool[1], *pool[3:])
    elif mutation == "target_omit":
        stories = stories[:1]
    elif mutation == "target_reorder":
        stories = tuple(reversed(stories))
    else:
        stories = stories * 2
    with pytest.raises(ValueError):
        replace(batch, member_pool=pool, stories=stories)


def test_source_grant_scope_policy_and_mixed_typed_member_views_are_rejected(case):
    inputs, request, _, _ = case
    grant = inputs.semantic.source_grant
    bad_semantic = replace(inputs.semantic, source_grant=replace(
        grant, policy=replace(grant.policy, authorized_purposes=("semantic_analysis",)),
    ))
    for changes in (
        {"semantic": bad_semantic}, {"scope": ArtifactScope("other", "series", "foreign")},
        {"stage1": replace(inputs.narrative.values, admission=replace(inputs.narrative.values.admission, raw_draft_sha256="sha256:" + "f" * 64))},
        {"job_policy": replace(request.job_policy, max_search_states=request.job_policy.max_search_states + 1)},
        {"story_policy": replace(request.story_policy, allowed_genre_tags=("foreign-genre",))},
    ):
        with pytest.raises(ValueError):
            _build(case, **changes)


def test_payload_hash_duplicate_keys_unknown_wire_and_input_binding_tampering(batch):
    member = batch.member_pool[0]
    with pytest.raises(ValueError):
        replace(member, payload_json="{}")
    raw = member.payload_json.replace('"census":', '"census":{},"census":', 1)
    with pytest.raises(ValueError):
        replace(member, payload_json=raw)
    with pytest.raises(ValueError):
        replace(member, payload_json=json.dumps(json.loads(member.payload_json), indent=2))
    with pytest.raises(ValueError):
        ExactContextMember.from_mapping({**member.to_mapping(), "omissions": []})
    mapping = batch.to_mapping()
    mapping["input_binding_sha256"] = "sha256:" + "f" * 64
    with pytest.raises(ValueError, match="binding"):
        EditorialContextBatch.from_mapping(mapping)
    for value in (EvidenceClosureSet, EditorialContextManifest, StoryEditorialContext, MaterialEvidenceClosure):
        with pytest.raises(ValueError):
            value.from_mapping({"pass": True})


def test_context_policy_changes_bind_new_content_without_changing_predecessor_payloads(case, batch):
    changed = _build(case, policy=replace(POLICY, max_batch_context_bytes=POLICY.max_batch_context_bytes + 1))
    assert changed.member_pool == batch.member_pool
    assert changed.input_binding_sha256 != batch.input_binding_sha256
    assert changed.canonical_hash != batch.canonical_hash
    assert changed.stories[0].context_member.content_hash != batch.stories[0].context_member.content_hash


def _rehashed_pool_wire(batch, pool):
    """Rehash every context descendant, not just the altered raw member.

    The predecessor Stage members deliberately stay intact: their evidence must
    still resolve to the supplied raw pool even if all context hashes agree.
    """
    wire = batch.to_mapping()
    wire["member_pool"] = [member.to_mapping() for member in pool]
    refs = [member.member_ref.to_mapping() for member in pool]
    for story in wire["stories"]:
        closure, context = story["closure_member"], story["context_member"]
        closure["payload"]["member_refs"] = refs
        closure["member_ref"]["content_hash"] = canonical_payload_hash(canonical_json_bytes(closure["payload"]).decode())
        context["payload"]["closure_set_ref"] = closure["member_ref"]
        expanded = canonical_json_bytes({"schema_version": "stage3-story-context-content-v1", "member_pool": wire["member_pool"],
                                         "stage2_policies": wire["stage2_policies"], "closure": closure["payload"]})
        context["payload"]["context_content_sha256"] = sha256_bytes(expanded)
        context["payload"]["context_byte_length"] = len(expanded)
        context["member_ref"]["content_hash"] = canonical_payload_hash(canonical_json_bytes(context["payload"]).decode())
    wire["input_binding_sha256"] = canonical_json_hash({"schema_version": "stage3-editorial-input-binding-v1",
        "member_refs": refs, "context_policy_sha256": batch.policy.canonical_hash,
        "job_policy_sha256": batch.job_policy.canonical_hash, "story_policy_sha256": batch.story_policy.canonical_hash,
        "candidate_policy_sha256": batch.candidate_policy.canonical_hash, "target_story_ids": list(batch.target_story_ids)})
    return wire


def _assert_closed_pool_rejected(batch, pool, *, match=None):
    wire = _rehashed_pool_wire(batch, pool)
    # Every value/closure/manifest hash is internally valid before the batch
    # checks actual request/pack/Source and nested semantic evidence joins.
    stories = tuple(StoryEditorialContext.from_mapping(story) for story in wire["stories"])
    with pytest.raises(ValueError, match=match):
        replace(batch, member_pool=pool, stories=stories)
    with pytest.raises(ValueError, match=match):
        EditorialContextBatch.from_mapping(wire)


def _changed_pool_member(batch, ordinal, payload):
    changed = ExactContextMember.from_artifact_member(_rewrite(batch.member_pool[ordinal].as_artifact_member(), payload))
    return (*batch.member_pool[:ordinal], changed, *batch.member_pool[ordinal + 1:])


def test_rehash_oracle_preserves_valid_complete_batch(batch):
    assert EditorialContextBatch.from_mapping(_rehashed_pool_wire(batch, batch.member_pool)) == batch


@pytest.mark.parametrize("mutation", ["empty", "missing", "extra", "identity_missing", "identity_extra",
    "identity_bool", "identity_zero", "attempt_type", "attempt_malformed", "episode_bool", "episode_float",
    "empty_key", "proxy_extra", "proxy_bool_length", "proxy_bad_uuid", "payload_hash", "source_hash",
    "source_provenance_zero", "episode_owner", "window_hash", "window_set", "identity_hash", "identity_rehashed"])
def test_request_record_schema_and_pair_binding_survive_no_fully_rehashed_substitution(batch, mutation):
    payload = json.loads(batch.member_pool[1].payload_json)
    bad_hash = "sha256:" + "f" * 64
    if mutation == "empty":
        payload = {}
    elif mutation == "missing":
        del payload["request_hash"]
    elif mutation == "extra":
        payload["accepted"] = True
    elif mutation == "identity_missing":
        del payload["request_identity"]["model_id"]
    elif mutation == "identity_extra":
        payload["request_identity"]["accepted"] = True
    elif mutation == "identity_bool":
        payload["request_identity"]["model_id"] = True
    elif mutation == "identity_zero":
        payload["request_identity"]["parse_policy_sha256"] = "sha256:" + "0" * 64
    elif mutation == "attempt_type":
        payload["attempt_id"] = 123
    elif mutation == "attempt_malformed":
        payload["attempt_id"] = "not-a-uuid"
    elif mutation == "episode_bool":
        payload["episode_index"] = True
    elif mutation == "episode_float":
        payload["episode_index"] = 0.0
    elif mutation == "empty_key":
        payload["provider_idempotency_key"] = " "
    elif mutation == "proxy_extra":
        payload["proxy_blob"]["path"] = "/not-a-source"
    elif mutation == "proxy_bool_length":
        payload["proxy_blob"]["byte_length"] = True
    elif mutation == "proxy_bad_uuid":
        payload["proxy_blob"]["object_id"] = "invalid"
    elif mutation == "payload_hash":
        payload["request_payload_blob"]["content_hash"] = bad_hash
    elif mutation == "source_hash":
        payload["source_manifest_sha256"] = bad_hash
    elif mutation == "source_provenance_zero":
        payload["source_provenance_sha256"] = "sha256:" + "0" * 64
    elif mutation == "episode_owner":
        payload["episode_index"] = 1
    elif mutation == "window_hash":
        payload["window_manifest_sha256"] = bad_hash
    elif mutation == "window_set":
        payload["window_manifest_set_sha256"] = bad_hash
    elif mutation == "identity_hash":
        payload["request_identity_sha256"] = bad_hash
    else:
        payload["request_identity"]["model_id"] = "different-model"
        payload["request_identity_sha256"] = VlmRequestIdentity(**payload["request_identity"]).canonical_hash
    if mutation == "episode_float":
        # Strict canonical JSON disallows floats before any member can exist.
        with pytest.raises(ValueError):
            _changed_pool_member(batch, 1, payload)
    else:
        _assert_closed_pool_rejected(batch, _changed_pool_member(batch, 1, payload))


@pytest.mark.parametrize("ordinal,field,value", [(1, "logical_id", "foreign-request"),
    (2, "logical_id", "foreign-pack"), (2, "revision", 2)])
def test_pair_owner_identity_cannot_be_relabelled_with_fully_rehashed_context(batch, ordinal, field, value):
    pool = list(batch.member_pool)
    pool[ordinal] = replace(pool[ordinal], member_ref=replace(pool[ordinal].member_ref, **{field: value}))
    _assert_closed_pool_rejected(batch, tuple(pool))


def test_changed_pack_identity_cannot_leave_stage1_and_catalog_raw_owners_dangling(batch):
    payload = json.loads(batch.member_pool[2].payload_json)
    payload["provenance"]["raw_response_sha256"] = "sha256:" + "f" * 64
    # Same valid window/request and all raw object IDs, but a different actual
    # pack owner. Both Stage 1 and Catalog still refer to the original owner.
    _assert_closed_pool_rejected(batch, _changed_pool_member(batch, 2, payload), match="actual pool owner/object")


@pytest.mark.parametrize("field", ["request_identity_sha256", "window_manifest_sha256"])
def test_pack_provenance_must_join_the_actual_request_record(batch, field):
    payload = json.loads(batch.member_pool[2].payload_json)
    payload["provenance"][field] = "sha256:" + "f" * 64
    _assert_closed_pool_rejected(batch, _changed_pool_member(batch, 2, payload))


@pytest.mark.parametrize("field", ["source_id", "source_clock_id", "source_sha256", "frame_samples_sha256",
                                 "frame_pts_index_set_sha256", "proxy_blob_ref_sha256", "window_sampling_policy_sha256"])
def test_agreeing_rehashed_request_and_pack_still_require_exact_source_manifest_binding(batch, field):
    request = json.loads(batch.member_pool[1].payload_json)
    request["request_identity"][field] = "sha256:" + "f" * 64 if field.endswith("_sha256") else "foreign-source"
    request["request_identity_sha256"] = VlmRequestIdentity(**request["request_identity"]).canonical_hash
    pool = list(_changed_pool_member(batch, 1, request))
    pack = json.loads(pool[2].payload_json)
    pack["provenance"]["request_identity_sha256"] = request["request_identity_sha256"]
    replacements = {
        item[f"{kind}_id"]: derive_vlm_global_id(kind, item[f"local_{kind}_id"], request["request_identity_sha256"])
        for kind, collection in (("entity", "entities"), ("fact", "facts"), ("event", "events"), ("candidate", "candidate_hypotheses"))
        for item in pack[collection]
    }

    def rebind(value):
        if isinstance(value, str):
            return replacements.get(value, value)
        if isinstance(value, list):
            return [rebind(item) for item in value]
        if isinstance(value, dict):
            return {key: rebind(item) for key, item in value.items()}
        return value

    pack = rebind(pack)
    # Keep this malicious pair structurally valid under the real VLM decoder;
    # rejection must come from Source binding, not stale request-derived IDs.
    owner.decode_vlm_semantic_pack(pack)
    pool[2] = ExactContextMember.from_artifact_member(_rewrite(pool[2].as_artifact_member(), pack))
    _assert_closed_pool_rejected(batch, tuple(pool), match="manifest binding mismatch")
