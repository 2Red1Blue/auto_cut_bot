"""Exact semantic compatibility is not a replay or authorization decision."""

from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError, dataclass, replace
from uuid import UUID

import pytest
from autocut_kernel.context_pack import WindowContextPack
from autocut_kernel.media.types import TickRange
from autocut_kernel.pipeline.generate_vlm_evidence_command import GenerateVlmEvidenceRequest
from autocut_kernel.store.models import BlobRef, Job, canonical_recipe_scope
from autocut_kernel.vlm import GenerationRetryPolicy, WindowManifestSet
from autocut_kernel.vlm.reuse_identity import (
    VlmReuseIdentityV1,
    VlmSemanticPolicyIdentityV1,
)

from .test_models import _manifest, _policy

_TEMPLATE = "Describe only the supplied window.\n"
_PARAMETERS: dict[str, object] = {
    "adapter_strategy_version": "adapter-v1",
    "max_output_tokens": 32768,
    "temperature": 0.0,
    "video_fps": 1.0,
}


@dataclass(frozen=True)
class _ProviderScope:
    provider_scope_fingerprint: str = "sha256:" + "1" * 64
    debug_path: str = "/tmp/mac/debug"
    worker_count: int = 1


def _context_pack(rendered_context: str = "API context: Alice arrives.") -> WindowContextPack:
    return WindowContextPack(
        mode="video_only",
        source_binding_hash=None,
        normalized_context_hash=None,
        selection_policy_version="context-selection-v1",
        selection_policy_hash="sha256:" + "6" * 64,
        known_through_external_episode_ordinal=None,
        selected_refs=(),
        suppressed_reason_counts=(),
        rendered_context=rendered_context,
        video_only_reason_code="fixture-no-api-context",
    )


def _request(*, context_pack: WindowContextPack | None = None) -> GenerateVlmEvidenceRequest:
    manifest = _manifest()
    proxy_id = UUID("00000000-0000-0000-0000-000000000001")
    manifest = replace(
        manifest,
        proxy_blob_ref=replace(manifest.proxy_blob_ref, object_id=str(proxy_id)),
    )
    job = Job("original-run", "test")
    return GenerateVlmEvidenceRequest(
        job=job,
        idempotency_key="episode-0",
        artifact_scope=canonical_recipe_scope(job),
        artifact_revision=1,
        manifest=manifest,
        manifest_set=WindowManifestSet(
            manifest.source_id, manifest.source_clock_id, manifest.source_sha256,
            manifest.stream_index, manifest.source_time_base, manifest.core_range, (manifest,),
        ),
        proxy_blob=BlobRef(proxy_id, "sha256:" + "b" * 64, 4096, "video/mp4"),
        prompt_template=_TEMPLATE + "Exact context: 人物甲\nframe evidence",
        prompt_version="prompt-v1",
        response_schema_json='{"type":"object","properties":{"name":{"type":"string"}}}',
        request_parameters_json=json.dumps(_PARAMETERS),
        model_id="doubao-seed-2-1-pro-260628",
        provider_id="doubao-ark",
        parse_policy=_policy(),
        retry_policy=GenerationRetryPolicy("generation-retry-v1", 1, ()),
        source_provenance_sha256="sha256:" + "2" * 64,
        source_manifest_sha256="sha256:" + "5" * 64,
        context_pack=context_pack,
    )


def _identity(
    request: GenerateVlmEvidenceRequest,
    scope: _ProviderScope = _ProviderScope(),
) -> VlmReuseIdentityV1:
    policy = VlmSemanticPolicyIdentityV1.from_request(
        request, provider_scope=scope, prompt_template=_TEMPLATE,
    )
    return VlmReuseIdentityV1.from_request(request, semantic_policy=policy)


def test_canonical_identity_is_stable_and_retains_original_request_proof() -> None:
    request = _request()
    identity = _identity(request)
    reordered = replace(
        request,
        response_schema_json=json.dumps(json.loads(request.response_schema_json), indent=2),
        request_parameters_json=json.dumps(dict(reversed(tuple(_PARAMETERS.items())))),
    )
    assert identity == _identity(reordered)
    assert identity.canonical_hash == _identity(reordered).canonical_hash
    assert identity.origin_request_identity == request.request_identity
    assert identity.to_mapping()["kind"] == "VlmReuseIdentity/v1"
    assert identity.semantic_policy.to_mapping()["kind"] == "VlmSemanticPolicyIdentity/v1"
    with pytest.raises(FrozenInstanceError):
        identity.source_provenance_sha256 = "sha256:" + "3" * 64  # type: ignore[misc]


def test_runtime_host_run_and_retry_changes_do_not_change_compatibility() -> None:
    original = _request()
    target_job = Job("windows-target-run", "test")
    target = replace(
        original, job=target_job, artifact_scope=canonical_recipe_scope(target_job),
        idempotency_key="another-command", artifact_revision=2,
        retry_policy=GenerationRetryPolicy("generation-retry-v1", 3, (1, 8)),
    )
    before = _identity(original)
    after = _identity(target, _ProviderScope(debug_path="C:\\debug", worker_count=10))
    assert original.request_hash != target.request_hash
    assert before.origin_request_identity != after.origin_request_identity
    assert before == after
    assert before.canonical_hash == after.canonical_hash


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_output_tokens", 16384), ("video_fps", 2.0), ("temperature", 0.5),
        ("adapter_strategy_version", "adapter-v2"),
    ],
)
def test_effective_request_parameter_changes_invalidate_identity(field: str, value: object) -> None:
    request = _request()
    changed = replace(request, request_parameters_json=json.dumps({**_PARAMETERS, field: value}))
    assert _identity(request).canonical_hash != _identity(changed).canonical_hash
    assert _identity(request).semantic_policy != _identity(changed).semantic_policy


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("prompt_template", _TEMPLATE + "Different exact 人物 context"),
        ("model_id", "doubao-seed-2-1-pro-260629"),
        ("provider_id", "other-provider"),
        ("prompt_version", "prompt-v2"),
        ("response_schema_json", '{"type":"object","additionalProperties":false}'),
        ("source_provenance_sha256", "sha256:" + "3" * 64),
        ("source_manifest_sha256", "sha256:" + "4" * 64),
        ("episode_index", 1),
    ],
)
def test_exact_request_fact_changes_invalidate_identity(field: str, value: object) -> None:
    request = _request()
    assert _identity(request).canonical_hash != _identity(replace(request, **{field: value})).canonical_hash


def test_rendered_context_is_not_batch_semantic_policy() -> None:
    request = _request()
    changed = replace(request, prompt_template=_TEMPLATE + "episode 2 context")
    assert _identity(request).semantic_policy == _identity(changed).semantic_policy
    assert _identity(request) != _identity(changed)


def test_context_pack_bytes_and_digest_are_closed_identity_facts() -> None:
    request = _request(context_pack=_context_pack())
    identity = _identity(request)
    changed = _request(context_pack=_context_pack("API context: Bob leaves."))

    assert identity.context_pack == request.context_pack
    assert identity.context_pack_sha256 == request.context_pack.canonical_hash
    assert identity.to_mapping()["context_pack_sha256"] == request.context_pack.canonical_hash
    assert identity.canonical_hash != _identity(changed).canonical_hash

    payload = json.loads(identity.origin_request_payload)
    payload["context_pack_sha256"] = "sha256:" + "7" * 64
    tampered_payload = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    tampered_origin = replace(
        identity.origin_request_identity,
        request_payload_sha256="sha256:" + hashlib.sha256(tampered_payload).hexdigest(),
    )
    with pytest.raises(ValueError, match="semantic policy facts"):
        replace(
            identity,
            origin_request_identity=tampered_origin,
            origin_request_payload=tampered_payload,
        )
    with pytest.raises(ValueError, match="context_pack_sha256"):
        replace(identity, context_pack_sha256="sha256:" + "8" * 64)


def test_provider_isolation_and_static_template_change_semantic_policy() -> None:
    request = _request()
    original = _identity(request)
    assert original != _identity(request, _ProviderScope("sha256:" + "4" * 64))
    changed = VlmSemanticPolicyIdentityV1.from_request(
        request, provider_scope=_ProviderScope(), prompt_template="Describe only",
    )
    assert changed.canonical_hash != original.semantic_policy.canonical_hash


def test_parse_budget_and_manifest_timeline_and_frames_are_bound() -> None:
    request = _request()
    assert _identity(request) != _identity(
        replace(request, parse_policy=replace(request.parse_policy, max_facts=32))
    )
    for manifest in (
        replace(request.manifest, frame_samples=request.manifest.frame_samples[:-1]),
        replace(
            request.manifest,
            timeline_map=replace(
                request.manifest.timeline_map,
                segments=(replace(request.manifest.timeline_map.segments[0], max_source_error_pts=2),),
            ),
        ),
    ):
        changed = replace(
            request, manifest=manifest,
            manifest_set=replace(request.manifest_set, manifests=(manifest,)),
        )
        assert _identity(request) != _identity(changed)


@pytest.mark.parametrize(
    "parameters",
    [
        '{"max_output_tokens":32768}',
        json.dumps({**_PARAMETERS, "unknown": 1}),
        json.dumps({**_PARAMETERS, "max_output_tokens": True}),
        json.dumps({**_PARAMETERS, "video_fps": "1"}),
        json.dumps({**_PARAMETERS, "temperature": float("nan")}),
        json.dumps({**_PARAMETERS, "video_fps": float("inf")}),
        json.dumps({**_PARAMETERS, "temperature": False}),
        json.dumps(_PARAMETERS)[:-1] + ',"temperature":0.5}',
        json.dumps(_PARAMETERS).replace('"temperature": 0.0', '"temperature": 1e999'),
        json.dumps({**_PARAMETERS, "temperature": 10 ** 1000}),
    ],
)
def test_invalid_or_incomplete_effective_parameters_fail_closed(parameters: str) -> None:
    with pytest.raises((TypeError, ValueError)):
        _identity(replace(_request(), request_parameters_json=parameters))


def test_missing_provenance_or_scope_fails_without_defaults() -> None:
    with pytest.raises(ValueError, match="source_provenance"):
        _identity(replace(_request(), source_provenance_sha256=None))
    with pytest.raises(ValueError, match="source_manifest"):
        _identity(replace(_request(), source_manifest_sha256=None))
    with pytest.raises(ValueError, match="source_manifest"):
        replace(_identity(_request()), source_manifest_sha256=None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="provider_scope"):
        _identity(_request(), _ProviderScope(""))
    with pytest.raises((TypeError, ValueError)):
        VlmSemanticPolicyIdentityV1.from_request(
            _request(), provider_scope=None, prompt_template=_TEMPLATE,  # type: ignore[arg-type]
        )


def test_policy_cannot_be_applied_to_different_request_parameters() -> None:
    request = _request()
    policy = _identity(request).semantic_policy
    changed = replace(request, request_parameters_json=json.dumps({**_PARAMETERS, "video_fps": 2.0}))
    with pytest.raises(ValueError, match="semantic policy"):
        VlmReuseIdentityV1.from_request(changed, semantic_policy=policy)


def test_template_must_be_exact_prefix_not_a_hash_assertion() -> None:
    with pytest.raises(ValueError, match="template"):
        VlmSemanticPolicyIdentityV1.from_request(
            _request(), provider_scope=_ProviderScope(), prompt_template="unrelated template",
        )


def test_other_window_changes_in_manifest_set_invalidate_the_same_window() -> None:
    request = _request()
    first = replace(request.manifest, core_range=TickRange(1000, 1050))
    second = replace(request.manifest, core_range=TickRange(1050, 1100))
    initial = replace(
        request, manifest=first,
        manifest_set=replace(request.manifest_set, manifests=(first, second)),
    )
    modified_second = replace(second, frame_samples=second.frame_samples[:-1])
    changed = replace(
        initial,
        manifest_set=replace(initial.manifest_set, manifests=(first, modified_second)),
    )
    assert initial.manifest == changed.manifest
    assert _identity(initial) != _identity(changed)


def test_changed_source_identity_cannot_reuse_old_episode() -> None:
    request = _request()
    source_hash = "sha256:" + "8" * 64
    frames = request.manifest.frame_pts_index_set
    frames = replace(
        frames,
        context=replace(frames.context, source_sha256=source_hash),
        coverage=replace(frames.coverage, source_sha256=source_hash),
    )
    manifest = replace(request.manifest, source_sha256=source_hash, frame_pts_index_set=frames)
    changed = replace(
        request, manifest=manifest,
        manifest_set=replace(request.manifest_set, source_sha256=source_hash, manifests=(manifest,)),
    )
    assert _identity(request) != _identity(changed)


@pytest.mark.parametrize("field", ["semantic_policy", "manifest", "manifest_set", "origin_request_identity"])
def test_reuse_constructor_rejects_untyped_boundary_values(field: str) -> None:
    with pytest.raises((TypeError, ValueError)):
        replace(_identity(_request()), **{field: {}})


def test_reuse_constructor_checks_parameters_and_parser_strategy_without_factory() -> None:
    identity = _identity(_request())
    for policy in (
        replace(identity.semantic_policy, request_parameters_json=json.dumps({**_PARAMETERS, "video_fps": 2.0})),
        replace(identity.semantic_policy, parser_strategy_version="different-parser"),
    ):
        with pytest.raises(ValueError, match="semantic policy"):
            replace(identity, semantic_policy=policy)


@pytest.mark.parametrize("extra", [{"unknown": 1}, {"prompt": "forged prompt"}])
def test_origin_payload_must_be_closed_and_match_request_facts(extra: dict[str, object]) -> None:
    identity = _identity(_request())
    payload = json.dumps({**json.loads(identity.origin_request_payload), **extra}).encode("utf-8")
    origin_identity = replace(
        identity.origin_request_identity,
        request_payload_sha256="sha256:" + hashlib.sha256(payload).hexdigest(),
    )
    with pytest.raises(ValueError, match="origin request payload"):
        replace(identity, origin_request_identity=origin_identity, origin_request_payload=payload)


def test_semantic_parameter_identity_uses_effective_numeric_values() -> None:
    request = _request()
    numeric_forms = replace(
        request, request_parameters_json=json.dumps({**_PARAMETERS, "temperature": 0, "video_fps": 1}),
    )
    assert request.request_identity != numeric_forms.request_identity
    assert _identity(request) == _identity(numeric_forms)


def test_mapping_mutation_does_not_mutate_frozen_identity() -> None:
    identity = _identity(_request())
    original_hash = identity.canonical_hash
    mapping = identity.semantic_policy.to_mapping()
    mapping["request_parameters"] = {"max_output_tokens": 1}
    assert identity.canonical_hash == original_hash


@pytest.mark.parametrize(
    "schema_json", ['{"type":"object","x":NaN}', '{"type":"object","x":"\\ud800"}'],
)
def test_nonfinite_or_invalid_unicode_schema_fails_during_construction(schema_json: str) -> None:
    with pytest.raises(ValueError):
        replace(_identity(_request()).semantic_policy, response_schema_json=schema_json)


def test_parser_strategy_is_an_explicit_semantic_dependency() -> None:
    original = _identity(_request()).semantic_policy
    # This tests the generic compatibility projection, not admission of the
    # now-registered v4 parser (which requires its explicit implementation hash).
    changed = replace(original, parser_strategy_version="strict-semantic-pack-test-alternate")
    assert original.canonical_hash != changed.canonical_hash
