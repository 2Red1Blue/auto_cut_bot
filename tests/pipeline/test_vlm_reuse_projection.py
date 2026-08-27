"""The existing factory can recover an exact compatibility projection."""

from dataclasses import replace
from uuid import uuid4

import pytest
from autocut_kernel.store import Job
from autocut_kernel.store.models import canonical_recipe_scope

from auto_cut_bot.pipeline.vlm.doubao_ark_provider import DoubaoArkVlmProviderConfig
from auto_cut_bot.pipeline.vlm.request_factory import build_doubao_vlm_request
from auto_cut_bot.pipeline.vlm.reuse import derive_vlm_reuse_identity
from tests.pipeline.test_doubao_vlm_request_factory import _policy, _retry_policy, _source_bundle


def _case():
    bundle = _source_bundle()
    request = build_doubao_vlm_request(
        source_bundle=bundle, episode_index=0, job=bundle.source_job,
        artifact_revision=1, idempotency_key="original-episode", policy=_policy(),
        retry_policy=_retry_policy(),
    )
    scope = DoubaoArkVlmProviderConfig(
        api_key="fixture-key-not-used", tenant_id="tenant-1", project_id="project-1",
    )
    return bundle, request, scope


def test_existing_factory_recovers_manifest_from_provenance_without_rewriting_request():
    bundle, request, scope = _case()
    original_hash, original_payload = request.request_hash, request.request_payload
    assert request.source_manifest_sha256 is None
    projected = derive_vlm_reuse_identity(request, source_bundle=bundle, provider_scope=scope)
    assert projected.source_manifest_sha256 == bundle.artifact_reference.content_hash
    assert projected.source_provenance_sha256 == bundle.canonical_hash
    assert projected.origin_request_identity == request.request_identity
    assert projected.origin_request_payload == original_payload
    assert request.request_hash == original_hash
    assert request.source_manifest_sha256 is None


@pytest.mark.parametrize("field", ["receipt_id", "artifact_set_id", "command_slot_id", "kernel_job_id"])
def test_different_original_source_producer_cannot_supply_missing_identity(field):
    bundle, request, scope = _case()
    changed = replace(bundle, **{field: uuid4()})
    with pytest.raises(ValueError, match="producer"):
        derive_vlm_reuse_identity(request, source_bundle=changed, provider_scope=scope)


def test_target_job_is_not_permission_to_relabel_original_source():
    bundle, request, scope = _case()
    target = Job("another-run", "test")
    changed = replace(request, job=target, artifact_scope=canonical_recipe_scope(target))
    with pytest.raises(ValueError, match="producer"):
        derive_vlm_reuse_identity(
            changed,
            source_bundle=bundle, provider_scope=scope,
        )


@pytest.mark.parametrize("change", ["index", "manifest_hash", "provenance"])
def test_rejects_inconsistent_source_facts(change):
    bundle, request, scope = _case()
    changed = {
        "index": replace(request, episode_index=1),
        "manifest_hash": replace(request, source_manifest_sha256="sha256:" + "9" * 64),
        "provenance": replace(request, source_provenance_sha256="sha256:" + "9" * 64),
    }[change]
    with pytest.raises(ValueError):
        derive_vlm_reuse_identity(changed, source_bundle=bundle, provider_scope=scope)


def test_changed_provider_partition_is_not_compatible_but_credentials_are_not_in_identity():
    bundle, request, scope = _case()
    original = derive_vlm_reuse_identity(request, source_bundle=bundle, provider_scope=scope)
    rotated_key = derive_vlm_reuse_identity(
        request, source_bundle=bundle, provider_scope=replace(scope, api_key="rotated-fixture-key"),
    )
    other_scope = derive_vlm_reuse_identity(
        request, source_bundle=bundle, provider_scope=replace(scope, project_id="other-project"),
    )
    assert original.canonical_hash == rotated_key.canonical_hash
    assert original.canonical_hash != other_scope.canonical_hash
    assert "fixture-key" not in repr(original.to_mapping())


def test_changed_generation_budget_changes_semantic_compatibility():
    bundle, request, scope = _case()
    changed = build_doubao_vlm_request(
        source_bundle=bundle, episode_index=0, job=bundle.source_job,
        artifact_revision=1, idempotency_key="other-command", policy=_policy(max_output_tokens=8192),
        retry_policy=_retry_policy(),
    )
    original = derive_vlm_reuse_identity(request, source_bundle=bundle, provider_scope=scope)
    target = derive_vlm_reuse_identity(changed, source_bundle=bundle, provider_scope=scope)
    assert original.canonical_hash != target.canonical_hash
