"""Recover semantic compatibility facts without changing an original command.

The caller supplies the original request and an exact SourcePrep Store result.
This projection performs no IO, grants no cross-Job access, and does not prove a
successful VLM result. Those remain the recompute admission/Store's obligations.
"""

from __future__ import annotations

from dataclasses import replace

from autocut_kernel.pipeline import GenerateVlmEvidenceRequest
from autocut_kernel.vlm.reuse_identity import (
    VlmProviderScopeFacts,
    VlmReuseIdentityV1,
    VlmSemanticPolicyIdentityV1,
)
from autocut_kernel.vlm.reuse_identity_v2 import VlmReuseIdentityV2

from auto_cut_bot.pipeline.source_prep.command import PersistedPreparedSources

from .prompt import resolve_vlm_prompt_template


def derive_vlm_reuse_identity(
    request: GenerateVlmEvidenceRequest,
    *,
    source_bundle: PersistedPreparedSources,
    provider_scope: VlmProviderScopeFacts,
) -> VlmReuseIdentityV1:
    """Project a verified original source binding; never guess a missing hash.

Earlier factory requests omit source_manifest_sha256 while binding that exact
manifest transitively through source_provenance_sha256. Recover it from the
matching committed bundle for compatibility only. The caller's request/hash,
payload, Job, Receipt and ArtifactSet are not rewritten or redispatched.
    """
    if type(request) is not GenerateVlmEvidenceRequest:  # noqa: E721
        raise TypeError("reuse requires an exact original GenerateVlmEvidenceRequest")
    if type(source_bundle) is not PersistedPreparedSources:  # noqa: E721
        raise TypeError("reuse requires exact committed SourcePrep facts")
    if (
        request.job != source_bundle.source_job
        or request.source_provenance_sha256 != source_bundle.canonical_hash
    ):
        raise ValueError("original request does not bind the supplied source producer")
    source_bundle.prepared.census.require_purpose("semantic_analysis")
    episodes = source_bundle.prepared.episodes
    if not 0 <= request.episode_index < len(episodes):
        raise ValueError("original request episode index is outside the source manifest")
    episode = episodes[request.episode_index]
    source = source_bundle.prepared.census.sources[request.episode_index]
    if (
        request.manifest != episode.manifest
        or request.manifest_set != episode.manifest_set
        or request.proxy_blob != episode.proxy_blob
        or source.source_id != request.manifest.source_id
        or source.content_sha256 != request.manifest.source_sha256
    ):
        raise ValueError("original request does not bind the exact authorized source episode")
    manifest_hash = source_bundle.artifact_reference.content_hash
    if request.source_manifest_sha256 not in (None, manifest_hash):
        raise ValueError("original source manifest hash disagrees with committed SourcePrep")
    # This local value is identity input only, never a replacement command.
    facts = replace(request, source_manifest_sha256=manifest_hash)
    policy = VlmSemanticPolicyIdentityV1.from_request(
        facts, provider_scope=provider_scope,
        prompt_template=resolve_vlm_prompt_template(request.prompt_version),
    )
    return VlmReuseIdentityV1.from_request(facts, semantic_policy=policy)


def derive_vlm_reuse_identity_v2(
    request: GenerateVlmEvidenceRequest,
    *,
    source_bundle: PersistedPreparedSources,
    provider_scope: VlmProviderScopeFacts,
) -> VlmReuseIdentityV2:
    """Explicit portable projection with the same exact source/request checks.

The v1 entry point and historical hashes remain unchanged. This function does
not bind a result to a target Job or expand the source operation grant.
    """
    return VlmReuseIdentityV2(derive_vlm_reuse_identity(
        request, source_bundle=source_bundle, provider_scope=provider_scope,
    ))
