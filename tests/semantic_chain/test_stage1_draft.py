"""Synthetic Store-shaped inputs with genuine VLM parsing, not durable acceptance."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from uuid import UUID, uuid4

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes
from autocut_kernel.semantic_chain.stage1_draft import (
    STAGE1_DRAFT_SCHEMA_VERSION,
    Stage1DraftError,
    Stage1DraftPolicy,
    decode_stage1_draft,
    stage1_draft_prompt_inputs,
    stage1_draft_response_schema,
)
from autocut_kernel.source_manifest import (
    DecodedSeriesSource,
    SourceOperationGrant,
    SourceOperationPolicy,
)
from autocut_kernel.store import (
    BlobRef,
    CommittedArtifactMemberReference,
    CommittedSemanticInputs,
    CommittedVlmSemanticInput,
    Job,
    PersistedVlmGenerationChild,
    PersistedVlmSemanticPack,
    PersistedWholeSeriesSourceManifest,
    SourceWindowIdentity,
    VlmRequestRecordReference,
    VlmSemanticPackReference,
    WholeSeriesSourceManifestReference,
)
from autocut_kernel.store.models import canonical_payload_hash, canonical_recipe_scope
from autocut_kernel.vlm import VlmRequestIdentity, WindowProxyBlobRef, parse_vlm_response
from jsonschema import Draft202012Validator

from tests.vlm.test_parser import _context, _payload, _raw

HASH = "sha256:" + "a" * 64
POLICY = Stage1DraftPolicy(64_000, 64_000, 4, 32, 4, 4, 4, 4, 8, 256, 2048)


def _blob(blob):
    return {
        "object_id": str(blob.object_id),
        "content_hash": blob.content_hash,
        "byte_length": blob.byte_length,
        "media_type": blob.media_type,
    }


def _synthetic_inputs() -> CommittedSemanticInputs:
    """No object.__new__ bypasses: real persisted DTO constructors, fake persistence."""
    job = Job("stage1-draft-synthetic", "shadow")
    scope = canonical_recipe_scope(job)
    proxies = tuple(BlobRef(UUID(int=index + 100), HASH, 4096, "video/mp4") for index in range(2))
    source_json = json.dumps({"synthetic_source": "not a real Store acceptance"})
    source = PersistedWholeSeriesSourceManifest(
        WholeSeriesSourceManifestReference(
            scope, "whole_series_source_manifest", 1, canonical_payload_hash(source_json)
        ),
        source_json,
        proxies,
        UUID(int=1),
        UUID(int=2),
        UUID(int=3),
        UUID(int=4),
        job,
    )
    grant = SourceOperationGrant(
        SourceOperationPolicy(
            "synthetic-authorization", "synthetic-series", 1, ("semantic_analysis",)
        ),
        "all_or_nothing",
        (DecodedSeriesSource("episode.mp4", "source-001", HASH, 1),),
    )
    members = []
    for index, proxy in enumerate(proxies):
        manifest, manifest_set, parse_policy, template = _context()
        manifest = replace(
            manifest,
            stream_index=index,
            proxy_blob_ref=WindowProxyBlobRef(
                str(proxy.object_id),
                proxy.content_hash,
                proxy.byte_length,
                proxy.media_type,
            ),
        )
        manifest_set = replace(manifest_set, stream_index=index, manifests=(manifest,))
        identity = VlmRequestIdentity.from_manifest(
            manifest,
            manifest_set,
            prompt_template_sha256=template.prompt_template_sha256,
            prompt_version=template.prompt_version,
            response_schema_sha256=template.response_schema_sha256,
            model_id=template.model_id,
            provider_id=template.provider_id,
            request_parameters_sha256=template.request_parameters_sha256,
            request_payload_sha256=template.request_payload_sha256,
            parse_policy=parse_policy,
        )
        raw = _raw(_payload(manifest))
        pack = parse_vlm_response(
            raw,
            manifest=manifest,
            manifest_set=manifest_set,
            request_identity=identity,
            policy=parse_policy,
        )
        request_blob = BlobRef(
            UUID(int=200 + index), identity.request_payload_sha256, 20, "application/json"
        )
        raw_blob = BlobRef(
            UUID(int=300 + index), pack.raw_response_sha256, len(raw), "application/json"
        )
        attempt, receipt, artifact_set, slot = (UUID(int=400 + index * 10 + n) for n in range(4))
        request = {
            "attempt_id": str(attempt),
            "episode_index": index,
            "idempotency_key": f"request-{index}",
            "provider_idempotency_key": f"provider-{index}",
            "proxy_blob": _blob(proxy),
            "request_hash": HASH,
            "request_identity": identity.to_mapping(),
            "request_identity_sha256": identity.canonical_hash,
            "request_payload_blob": _blob(request_blob),
            "source_manifest_sha256": source.reference.content_hash,
            "source_provenance_sha256": source.canonical_hash,
            "window_manifest_set_sha256": identity.window_manifest_set_sha256,
            "window_manifest_sha256": identity.window_manifest_sha256,
        }
        request_json = json.dumps(request)
        child = PersistedVlmGenerationChild(
            VlmRequestRecordReference(
                scope,
                f"vlm_request_{identity.window_manifest_sha256[7:31]}",
                1,
                canonical_payload_hash(request_json),
            ),
            request_json,
            job,
            source.job_id,
            slot,
            f"request-{index}",
            HASH,
            attempt,
            f"provider-{index}",
            request_blob,
            receipt,
            artifact_set,
            index,
            identity.window_manifest_sha256,
            identity.window_manifest_set_sha256,
            source.reference.content_hash,
            source.canonical_hash,
            identity.canonical_hash,
        )
        pack_json = json.dumps(pack.to_mapping())
        persisted = PersistedVlmSemanticPack(
            VlmSemanticPackReference(
                scope,
                f"semantic_pack_{identity.window_manifest_sha256[7:39]}",
                1,
                canonical_payload_hash(pack_json),
            ),
            pack_json,
            pack,
            child,
        )
        response_json = json.dumps(
            {
                "attempt_id": str(attempt),
                "provider_request_id": f"response-{index}",
                "raw_response_blob": _blob(raw_blob),
                "raw_response_sha256": pack.raw_response_sha256,
            }
        )
        response = CommittedArtifactMemberReference(
            receipt,
            artifact_set,
            1,
            scope,
            "vlm_response_record",
            f"vlm_response_{identity.window_manifest_sha256[7:31]}",
            1,
            canonical_payload_hash(response_json),
        )
        window = SourceWindowIdentity(
            index,
            index,
            1000,
            1100,
            identity.window_manifest_sha256,
            identity.source_id,
            identity.source_sha256,
            identity.source_clock_id,
            identity.window_manifest_set_sha256,
            proxy,
        )
        members.append(CommittedVlmSemanticInput(window, identity, persisted, response, raw_blob))
    aggregate = CommittedArtifactMemberReference(
        UUID(int=10),
        UUID(int=11),
        0,
        scope,
        "vlm_semantic_pack_set",
        "vlm_semantic_pack_set",
        1,
        HASH,
    )
    policy = members[0].semantic_pack.source_child.request_policy
    return CommittedSemanticInputs(source, grant, aggregate, policy, tuple(members))


@pytest.fixture
def inputs():
    return _synthetic_inputs()


def _ref(inputs, index, kind):
    pack = inputs.inputs[index].semantic_pack.semantic_pack
    item = getattr(pack, {"entity": "entities", "fact": "facts", "event": "events"}[kind])[0]
    return {
        "window_manifest_sha256": pack.window_manifest_sha256,
        "object_type": kind,
        "object_id": getattr(item, f"{kind}_id"),
    }


def _draft(inputs):
    return {
        "schema_version": STAGE1_DRAFT_SCHEMA_VERSION,
        "input_binding_sha256": stage1_draft_prompt_inputs(inputs, policy=POLICY)[
            "input_binding_sha256"
        ],
        "beats": [
            {
                "beat_id": "beat_1",
                "summary": "A discovery.",
                "phase": "reveal",
                "event_refs": [_ref(inputs, 0, "event")],
                "obligation_ids": ["obligation_1"],
            }
        ],
        "obligations": [
            {
                "obligation_id": "obligation_1",
                "description": "Explain the discovery.",
                "required_fact_refs": [_ref(inputs, 0, "fact")],
                "success_criteria": "Retain the visible action.",
            }
        ],
        "story_threads": [
            {
                "story_thread_id": "thread_1",
                "title": "Discovery",
                "premise": "A key changes the story.",
                "obligation_ids": ["obligation_1"],
            }
        ],
        "merge_proposals": [
            {
                "merge_id": "merge_1",
                "entity_refs": [_ref(inputs, 0, "entity"), _ref(inputs, 1, "entity")],
                "evidence_refs": [_ref(inputs, 0, "fact"), _ref(inputs, 1, "event")],
                "rationale": "Possible same person; identity still requires independent validation.",
            }
        ],
    }


def _decode(payload, inputs, policy=POLICY):
    return decode_stage1_draft(json.dumps(payload).encode(), inputs=inputs, policy=policy)


def test_real_vlm_parse_draft_roundtrip_schema_and_no_authority(inputs):
    payload = _draft(inputs)
    schema = stage1_draft_response_schema(POLICY)
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(payload)
    first = _decode(payload, inputs)
    second = decode_stage1_draft(
        canonical_json_bytes(first.to_mapping()), inputs=inputs, policy=POLICY
    )
    assert first == second
    assert first.canonical_hash == second.canonical_hash
    assert len(first.merge_proposals) == 1
    assert set(first.to_mapping()) == set(payload)
    assert not hasattr(first, "decision") and not hasattr(first, "canonical_entities")
    # Response artifact content is NOT the raw response content identity.
    assert all(
        item.response_record.content_hash != item.raw_response.content_hash
        for item in inputs.inputs
    )


def test_empty_draft_is_not_coverage_or_admission(inputs):
    payload = _draft(inputs)
    for name in ("beats", "obligations", "story_threads", "merge_proposals"):
        payload[name] = []
    result = _decode(payload, inputs)
    assert (
        result.beats == result.obligations == result.story_threads == result.merge_proposals == ()
    )
    assert not hasattr(result, "admission")


def test_deep_immutable_output_and_fresh_schema_prompt(inputs):
    draft = _decode(_draft(inputs), inputs)
    with pytest.raises(FrozenInstanceError):
        draft.beats[0].summary = "modified"
    with pytest.raises(FrozenInstanceError):
        draft.merge_proposals[0].entity_refs[0].object_id = HASH
    mapping = draft.to_mapping()
    mapping["beats"][0]["event_refs"][0]["object_id"] = HASH
    assert draft.to_mapping() != mapping
    schema = stage1_draft_response_schema(POLICY)
    schema["properties"]["beats"]["items"]["properties"].clear()
    assert stage1_draft_response_schema(POLICY) != schema
    prompt = stage1_draft_prompt_inputs(inputs, policy=POLICY)
    prompt["windows"][0]["facts"].clear()
    assert stage1_draft_prompt_inputs(inputs, policy=POLICY) != prompt


def test_prompt_has_exact_allowlist_and_only_semantic_fields(inputs):
    prompt = stage1_draft_prompt_inputs(inputs, policy=POLICY)
    assert len(prompt["allowed_refs"]) == 6
    assert len(prompt["windows"]) == 2
    rendered = json.dumps(prompt)
    for prohibited in (
        "candidate",
        "confidence",
        "measurements",
        "support",
        "source_interval",
        "pts",
        "asr",
        "vad",
        "score",
    ):
        assert prohibited not in rendered
    for window in prompt["windows"]:
        assert window["entities"] and window["facts"] and window["events"]
        assert window["window_summary"] and window["continuity"]


def test_array_order_and_json_format_do_not_change_content_hash(inputs):
    payload = _draft(inputs)
    other = deepcopy(payload)
    other["merge_proposals"][0]["entity_refs"].reverse()
    other["merge_proposals"][0]["evidence_refs"].reverse()
    assert _decode(payload, inputs) == _decode(other, inputs)
    raw = json.dumps(payload, ensure_ascii=True, indent=2).encode() + b"\n"
    assert (
        decode_stage1_draft(raw, inputs=inputs, policy=POLICY).canonical_hash
        == _decode(payload, inputs).canonical_hash
    )


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"\xff",
        b"[]",
        b"null",
        b"{}",
        b"\xef\xbb\xbf{}",
        b"{}{}",
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b'{"x":-Infinity}',
        b'{"x":1.0}',
        b'{"x":"\\ud800"}',
        b'{"x":9007199254740992}',
        b"[" * 17 + b"]" * 17,
    ],
)
def test_strict_json_rejects_malformed_nonfinite_float_and_nesting(inputs, raw):
    with pytest.raises(Stage1DraftError):
        decode_stage1_draft(raw, inputs=inputs, policy=POLICY)


@pytest.mark.parametrize("nested", [False, True])
def test_duplicate_json_keys_rejected_at_any_depth(inputs, nested):
    raw = json.dumps(_draft(inputs)).encode()
    token = b'"summary":' if nested else b'"schema_version":'
    replacement = (
        b'"summary":"duplicate","summary":'
        if nested
        else b'"schema_version":"duplicate","schema_version":'
    )
    with pytest.raises(Stage1DraftError):
        decode_stage1_draft(raw.replace(token, replacement, 1), inputs=inputs, policy=POLICY)


@pytest.mark.parametrize(
    "field",
    [
        "coverage_status",
        "admission",
        "rule_pass",
        "start_pts",
        "asr",
        "canonical_entity_id",
        "ready",
    ],
)
@pytest.mark.parametrize("location", ["root", "beat", "merge", "ref"])
def test_no_caller_claims_or_extra_fields_at_any_object(inputs, field, location):
    payload = _draft(inputs)
    target = {
        "root": payload,
        "beat": payload["beats"][0],
        "merge": payload["merge_proposals"][0],
        "ref": payload["beats"][0]["event_refs"][0],
    }[location]
    target[field] = "claimed"
    with pytest.raises(Stage1DraftError):
        _decode(payload, inputs)


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("schema_version", "stage1-complete-v1"),
        ("input_binding_sha256", HASH),
        ("input_binding_sha256", "sha256:" + "0" * 64),
        ("beats", {}),
        ("obligations", True),
        ("story_threads", None),
        ("merge_proposals", 1),
    ],
)
def test_wrong_root_field_types_or_binding(inputs, field, value):
    payload = _draft(inputs)
    payload[field] = value
    with pytest.raises(Stage1DraftError):
        _decode(payload, inputs)


@pytest.mark.parametrize(
    "change",
    [
        "unknown",
        "wrong_window",
        "local_id",
        "zero",
        "uppercase",
        "wrong_type",
        "duplicate",
        "missing",
        "object_type_map",
    ],
)
def test_evidence_must_be_exact_owner_bound_global_typed_ref(inputs, change):
    payload = _draft(inputs)
    ref = payload["beats"][0]["event_refs"][0]
    if change == "unknown":
        ref["object_id"] = HASH
    elif change == "wrong_window":
        ref["window_manifest_sha256"] = inputs.inputs[1].source_window.window_manifest_sha256
    elif change == "local_id":
        ref["object_id"] = "event_1"
    elif change == "zero":
        ref["object_id"] = "sha256:" + "0" * 64
    elif change == "uppercase":
        ref["object_id"] = ref["object_id"].upper()
    elif change == "wrong_type":
        payload["beats"][0]["event_refs"] = [_ref(inputs, 0, "fact")]
    elif change == "duplicate":
        payload["beats"][0]["event_refs"].append(deepcopy(ref))
    elif change == "missing":
        del ref["object_type"]
    else:
        ref["object_type"] = {}
    with pytest.raises(Stage1DraftError):
        _decode(payload, inputs)


@pytest.mark.parametrize(
    "change",
    [
        "entity_only_evidence",
        "empty_evidence",
        "single_entity",
        "same_entity",
        "unknown_obligation",
        "duplicate_obligation",
        "empty_obligation",
        "bad_phase",
        "bad_id",
        "duplicate_id",
        "float",
        "bool",
        "blank",
        "missing",
    ],
)
def test_draft_graph_local_closure_and_unaccepted_merge_candidates(inputs, change):
    payload = _draft(inputs)
    merge, beat = payload["merge_proposals"][0], payload["beats"][0]
    if change == "entity_only_evidence":
        merge["evidence_refs"] = [_ref(inputs, 0, "entity")]
    elif change == "empty_evidence":
        merge["evidence_refs"] = []
    elif change == "single_entity":
        merge["entity_refs"].pop()
    elif change == "same_entity":
        merge["entity_refs"][1] = deepcopy(merge["entity_refs"][0])
    elif change == "unknown_obligation":
        beat["obligation_ids"] = ["unknown"]
    elif change == "duplicate_obligation":
        beat["obligation_ids"] *= 2
    elif change == "empty_obligation":
        beat["obligation_ids"] = []
    elif change == "bad_phase":
        beat["phase"] = "accepted"
    elif change == "bad_id":
        beat["beat_id"] = "../elsewhere"
    elif change == "duplicate_id":
        payload["beats"].append(deepcopy(beat))
    elif change == "float":
        beat["summary"] = 1.5
    elif change == "bool":
        beat["summary"] = True
    elif change == "blank":
        beat["summary"] = " \n "
    else:
        del beat["summary"]
    with pytest.raises(Stage1DraftError):
        _decode(payload, inputs)


@pytest.mark.parametrize(
    "bound",
    [
        "max_response_bytes",
        "max_input_windows",
        "max_input_objects",
        "max_text_characters",
        "max_total_text_characters",
        "max_references_per_item",
    ],
)
def test_explicit_operational_bounds(inputs, bound):
    with pytest.raises(Stage1DraftError):
        _decode(_draft(inputs), inputs, replace(POLICY, **{bound: 1}))


@pytest.mark.parametrize("array", ["beats", "obligations", "story_threads", "merge_proposals"])
def test_array_count_bounds(inputs, array):
    payload = _draft(inputs)
    payload[array] *= 5
    with pytest.raises(Stage1DraftError, match="count bound"):
        _decode(payload, inputs)


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.5, "4", 2**53])
def test_policy_rejects_bool_float_nonpositive_and_inexact_integer(value):
    with pytest.raises(Stage1DraftError):
        replace(POLICY, max_response_bytes=value)


@pytest.mark.parametrize(
    "change",
    [
        "aggregate_hash",
        "aggregate_receipt",
        "aggregate_set",
        "source_grant",
        "response_hash",
        "raw_id",
        "raw_size",
        "window_range",
        "generation_slot",
    ],
)
def test_binding_changes_for_every_durable_input_dimension(inputs, change):
    payload = _draft(inputs)
    first = inputs.inputs[0]
    if change.startswith("aggregate_"):
        field = {
            "aggregate_hash": "content_hash",
            "aggregate_receipt": "receipt_id",
            "aggregate_set": "artifact_set_id",
        }[change]
        value = "sha256:" + "b" * 64 if field == "content_hash" else uuid4()
        changed = replace(
            inputs, vlm_semantic_pack_set=replace(inputs.vlm_semantic_pack_set, **{field: value})
        )
    elif change == "source_grant":
        changed = replace(
            inputs,
            source_grant=replace(
                inputs.source_grant,
                policy=replace(inputs.source_grant.policy, authorization_id="different"),
            ),
        )
    else:
        if change == "response_hash":
            first = replace(
                first,
                response_record=replace(first.response_record, content_hash="sha256:" + "b" * 64),
            )
        elif change == "raw_id":
            first = replace(first, raw_response=replace(first.raw_response, object_id=uuid4()))
        elif change == "raw_size":
            first = replace(first, raw_response=replace(first.raw_response, byte_length=100))
        elif change == "window_range":
            first = replace(first, source_window=replace(first.source_window, core_end_pts=1200))
        else:
            first = replace(
                first,
                semantic_pack=replace(
                    first.semantic_pack,
                    source_child=replace(first.semantic_pack.source_child, command_slot_id=uuid4()),
                ),
            )
        changed = replace(inputs, inputs=(first, inputs.inputs[1]))
    assert (
        stage1_draft_prompt_inputs(changed, policy=POLICY)["input_binding_sha256"]
        != payload["input_binding_sha256"]
    )
    with pytest.raises(Stage1DraftError, match="binding"):
        _decode(payload, changed)


@pytest.mark.parametrize(
    "change",
    [
        "raw_hash",
        "window_hash",
        "source_hash",
        "source_id",
        "source_clock",
        "window_set",
        "response_receipt",
        "response_set",
        "response_ordinal",
        "response_type",
        "response_logical_id",
        "response_revision",
        "episode",
        "aggregate_policy",
        "aggregate_scope",
        "request_identity",
    ],
)
def test_inconsistent_input_projection_rejected_before_prompt_or_decode(inputs, change):
    first = inputs.inputs[0]
    if change == "aggregate_policy":
        inputs = replace(
            inputs,
            vlm_aggregate_policy=replace(inputs.vlm_aggregate_policy, model_id="other-model"),
        )
    elif change == "aggregate_scope":
        inputs = replace(
            inputs,
            vlm_semantic_pack_set=replace(
                inputs.vlm_semantic_pack_set,
                scope=replace(inputs.vlm_semantic_pack_set.scope, key="foreign-job"),
            ),
        )
    else:
        if change == "raw_hash":
            first = replace(first, raw_response=replace(first.raw_response, content_hash=HASH))
        elif change == "request_identity":
            first = replace(
                first, request_identity=replace(first.request_identity, model_id="foreign-model")
            )
        elif change.startswith("response_"):
            field = {
                "response_receipt": "receipt_id",
                "response_set": "artifact_set_id",
                "response_ordinal": "member_ordinal",
                "response_type": "artifact_type",
                "response_logical_id": "logical_id",
                "response_revision": "revision",
            }[change]
            value = (
                uuid4()
                if field in ("receipt_id", "artifact_set_id")
                else 2
                if field in ("member_ordinal", "revision")
                else "other_type"
            )
            first = replace(first, response_record=replace(first.response_record, **{field: value}))
        else:
            field = {
                "window_hash": "window_manifest_sha256",
                "source_hash": "source_sha256",
                "source_id": "source_id",
                "source_clock": "source_clock_id",
                "window_set": "window_manifest_set_sha256",
                "episode": "episode_index",
            }[change]
            value = (
                1
                if field == "episode_index"
                else "other"
                if field in ("source_id", "source_clock_id")
                else "sha256:" + "b" * 64
            )
            first = replace(first, source_window=replace(first.source_window, **{field: value}))
        inputs = replace(inputs, inputs=(first, inputs.inputs[1]))
    with pytest.raises(Stage1DraftError, match="identities"):
        stage1_draft_prompt_inputs(inputs, policy=POLICY)


def test_unicode_and_json_punctuation_are_text_not_json_nesting(inputs):
    payload = _draft(inputs)
    payload["beats"][0]["summary"] = '发现线索，字符串中的 [{\\" 不会增加层数。'
    decoded = _decode(payload, inputs)
    assert decoded.beats[0].summary == payload["beats"][0]["summary"]
    assert (
        decode_stage1_draft(
            canonical_json_bytes(decoded.to_mapping()), inputs=inputs, policy=POLICY
        )
        == decoded
    )


def test_wrong_input_and_policy_types_are_not_authority(inputs):
    with pytest.raises(Stage1DraftError):
        stage1_draft_prompt_inputs({}, policy=POLICY)
    with pytest.raises(Stage1DraftError):
        stage1_draft_response_schema({})
    with pytest.raises(Stage1DraftError):
        decode_stage1_draft(b"{}", inputs=inputs, policy={})


def test_prompt_is_bounded_as_whole_bytes_without_truncation_and_policy_is_hashable(inputs):
    prompt = stage1_draft_prompt_inputs(inputs, policy=POLICY)
    size = len(canonical_json_bytes(prompt))
    assert (
        stage1_draft_prompt_inputs(inputs, policy=replace(POLICY, max_prompt_bytes=size)) == prompt
    )
    with pytest.raises(Stage1DraftError, match="prompt projection"):
        stage1_draft_prompt_inputs(inputs, policy=replace(POLICY, max_prompt_bytes=size - 1))
    assert replace(POLICY).canonical_hash == POLICY.canonical_hash
    assert replace(POLICY, max_prompt_bytes=size).canonical_hash != POLICY.canonical_hash
    mapping = POLICY.to_mapping()
    mapping["max_prompt_bytes"] = 1
    assert POLICY.max_prompt_bytes == 64_000


def test_exact_semantic_pack_content_is_part_of_input_binding(inputs):
    payload = _draft(inputs)
    first = inputs.inputs[0]
    old = first.semantic_pack
    pack = replace(
        old.semantic_pack,
        window_summary=replace(
            old.semantic_pack.window_summary,
            summary="Changed committed semantic interpretation.",
        ),
    )
    pack_json = json.dumps(pack.to_mapping())
    persisted = replace(
        old,
        semantic_pack=pack,
        payload_json=pack_json,
        reference=replace(
            old.reference,
            content_hash=canonical_payload_hash(pack_json),
        ),
    )
    changed = replace(inputs, inputs=(replace(first, semantic_pack=persisted), inputs.inputs[1]))
    assert (
        stage1_draft_prompt_inputs(changed, policy=POLICY)["input_binding_sha256"]
        != payload["input_binding_sha256"]
    )
    with pytest.raises(Stage1DraftError, match="binding"):
        _decode(payload, changed)


@pytest.mark.parametrize(
    "array,id_key",
    [
        ("beats", "beat_id"),
        ("obligations", "obligation_id"),
        ("story_threads", "story_thread_id"),
        ("merge_proposals", "merge_id"),
    ],
)
def test_all_graph_arrays_normalize_by_local_id_and_reject_duplicate_identity(
    inputs, array, id_key
):
    payload = _draft(inputs)
    second = deepcopy(payload[array][0])
    second[id_key] += "_second"
    payload[array].append(second)
    decoded = _decode(payload, inputs)
    payload[array].reverse()
    assert _decode(payload, inputs) == decoded
    payload[array][0][id_key] = payload[array][1][id_key]
    with pytest.raises(Stage1DraftError, match="identities"):
        _decode(payload, inputs)
