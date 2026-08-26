"""Join admitted editorial alternatives to exact committed timed-media owners.

This read-only seam makes no physical choice or edit admission. All alternatives
survive; the Stage 3 material assignment is only a semantic feasibility witness.
Returned values retain semantic content and compact media references, never all
episodes' decoded roots, transcripts, candidate windows or clock certificates.
Constructing these DTOs directly proves neither commitment nor admission.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..media.types import canonical_sha256, sha256_prefixed
from ..registry.installed_runtime import InstalledLocalRunProfileResolver
from ..semantic_chain.candidate_catalog import Candidate
from ..semantic_chain.candidate_projection import decode_candidate_source_context
from ..semantic_chain.editorial_blueprint import (
    BlueprintEvidenceRequirement,
    EditorialBlueprintBeat,
)
from ..semantic_chain.editorial_models import EvidenceAlternative
from ..semantic_chain.member_refs import SemanticMemberIdentity, SemanticObjectRef
from ..store.models import (
    CommandOutcome,
    CommittedArtifactMemberReference,
    CommittedVlmSemanticInput,
)
from ..vlm.models import VlmEditingMode
from .build_editorial_blueprint_command import (
    PersistedEditorialBlueprintSet,
    read_committed_editorial_blueprints,
)
from .build_editorial_blueprint_request import BuildEditorialBlueprintRequest
from .build_narrative_graph_command import NarrativeGraphStore
from .committed_outcome import succeeded_outcome_mapping
from .committed_timed_media import TimedMediaReadLimits
from .editorial_blueprint_inputs import (
    CommittedEditorialBlueprintInputs,
    read_committed_editorial_blueprint_inputs,
)
from .finalize_timed_media_evidence_batch_command import (
    FinalizeTimedMediaEvidenceBatchRequest,
    FinalizeTimedMediaEvidenceBatchResult,
    TimedMediaEvidenceBatchStore,
    read_committed_timed_media_evidence_batch,
)


class EditorialTimedMediaInputError(ValueError):
    """Editorial and timed-media predecessors do not have the same exact owners."""


class EditorialTimedMediaStore(NarrativeGraphStore, TimedMediaEvidenceBatchStore, Protocol):
    """The existing audited semantic and timed-media reader interfaces."""


@dataclass(frozen=True, slots=True)
class EditorialTimedCandidateBinding:
    candidate_ref: SemanticObjectRef
    vlm_candidate_ref: SemanticObjectRef
    source_ref: SemanticObjectRef
    source_window_ref: SemanticObjectRef
    episode_index: int
    candidate_ordinal: int
    raw_candidate_sha256: str
    editing_modes: tuple[VlmEditingMode, ...]
    child_member_references: tuple[CommittedArtifactMemberReference, ...]

    def __post_init__(self) -> None:
        for ref, kind, object_type in (
            (self.candidate_ref, "candidate_catalog", "candidate"),
            (self.vlm_candidate_ref, "vlm_semantic_pack", "vlm_candidate"),
            (self.source_ref, "whole_series_source_manifest", "source"),
            (self.source_window_ref, "whole_series_source_manifest", "source_window"),
        ):
            if (type(ref) is not SemanticObjectRef or ref.member_ref.artifact_type != kind  # noqa: E721
                    or ref.object_type != object_type):
                raise EditorialTimedMediaInputError("candidate binding has an invalid semantic owner")
        if (self.source_ref.member_ref != self.source_window_ref.member_ref
                or len({ref.member_ref.scope for ref in (
                    self.candidate_ref, self.vlm_candidate_ref, self.source_ref,
                )}) != 1):
            raise EditorialTimedMediaInputError("candidate binding mixes Source owners or scopes")
        if any(type(value) is not int or not 0 <= value <= 9_007_199_254_740_991  # noqa: E721
               for value in (self.episode_index, self.candidate_ordinal)):
            raise EditorialTimedMediaInputError("candidate ordinals must be exact nonnegative integers")
        sha256_prefixed(self.raw_candidate_sha256, "raw_candidate_sha256")
        if (type(self.editing_modes) is not tuple or not self.editing_modes  # noqa: E721
                or any(type(mode) is not VlmEditingMode for mode in self.editing_modes)  # noqa: E721
                or len(set(self.editing_modes)) != len(self.editing_modes)):
            raise EditorialTimedMediaInputError("candidate editing modes must retain the VLM enum")
        refs = self.child_member_references
        if (type(refs) is not tuple or len(refs) != 5  # noqa: E721
                or any(type(ref) is not CommittedArtifactMemberReference for ref in refs)  # noqa: E721
                or tuple(ref.member_ordinal for ref in refs) != (0, 1, 2, 3, 4)
                or len({(ref.receipt_id, ref.artifact_set_id, ref.scope) for ref in refs}) != 1
                or refs[0].scope != self.source_ref.member_ref.scope):
            raise EditorialTimedMediaInputError("candidate binding requires the exact five-member owner")


@dataclass(frozen=True, slots=True)
class EditorialTimedAlternativeBinding:
    story_id: str
    beat: EditorialBlueprintBeat
    requirement: BlueprintEvidenceRequirement
    alternative: EvidenceAlternative
    candidates: tuple[EditorialTimedCandidateBinding, ...]

    def __post_init__(self) -> None:
        sha256_prefixed(self.story_id, "story_id")
        if (type(self.beat) is not EditorialBlueprintBeat  # noqa: E721
                or type(self.requirement) is not BlueprintEvidenceRequirement  # noqa: E721
                or type(self.alternative) is not EvidenceAlternative  # noqa: E721
                or self.requirement not in self.beat.evidence_requirements
                or self.alternative not in self.requirement.alternatives):
            raise EditorialTimedMediaInputError("alternative binding lost its original Beat/requirement")
        if (type(self.candidates) is not tuple  # noqa: E721
                or any(type(item) is not EditorialTimedCandidateBinding for item in self.candidates)  # noqa: E721
                or tuple(item.candidate_ref for item in self.candidates) != self.alternative.candidate_refs):
            raise EditorialTimedMediaInputError("alternative candidate grouping or order differs")


@dataclass(frozen=True, slots=True)
class CommittedEditorialTimedMediaInputs:
    editorial: PersistedEditorialBlueprintSet
    predecessors: CommittedEditorialBlueprintInputs
    media_batch_request: FinalizeTimedMediaEvidenceBatchRequest
    media_batch: FinalizeTimedMediaEvidenceBatchResult
    alternatives: tuple[EditorialTimedAlternativeBinding, ...]

    def __post_init__(self) -> None:
        if (type(self.editorial) is not PersistedEditorialBlueprintSet  # noqa: E721
                or type(self.predecessors) is not CommittedEditorialBlueprintInputs  # noqa: E721
                or type(self.media_batch_request) is not FinalizeTimedMediaEvidenceBatchRequest  # noqa: E721
                or type(self.media_batch) is not FinalizeTimedMediaEvidenceBatchResult  # noqa: E721
                or type(self.alternatives) is not tuple  # noqa: E721
                or any(type(item) is not EditorialTimedAlternativeBinding for item in self.alternatives)):  # noqa: E721
            raise EditorialTimedMediaInputError("joined inputs require exact immutable values")
        expected = tuple(
            (story.story_id, beat, requirement, alternative)
            for story in self.editorial.values.business.projection.blueprints
            for beat in story.beats for requirement in beat.evidence_requirements
            for alternative in requirement.alternatives
        )
        actual = tuple((row.story_id, row.beat, row.requirement, row.alternative)
                       for row in self.alternatives)
        if actual != expected:
            raise EditorialTimedMediaInputError("joined inputs dropped or reordered editorial alternatives")


def _pack_identity(committed: CommittedVlmSemanticInput) -> SemanticMemberIdentity:
    ref = committed.semantic_pack.reference
    return SemanticMemberIdentity(ref.artifact_type, ref.logical_id, ref.revision, ref.scope, ref.content_hash)


def _bind_candidate(
    ref: SemanticObjectRef, candidate: Candidate, committed: CommittedVlmSemanticInput,
    source_identity: SemanticMemberIdentity, batch_request: FinalizeTimedMediaEvidenceBatchRequest,
    batch: FinalizeTimedMediaEvidenceBatchResult,
) -> EditorialTimedCandidateBinding:
    window = committed.source_window
    episode_index = window.episode_index
    if not 0 <= episode_index < len(batch_request.children):
        raise EditorialTimedMediaInputError("candidate episode is absent from the complete media batch")
    child = batch_request.children[episode_index].request
    pack = committed.semantic_pack.semantic_pack
    if (candidate.candidate_ref.member_ref != _pack_identity(committed)
            or candidate.source_ref != SemanticObjectRef(source_identity, "source", window.source_id)
            or candidate.source_window_ref != SemanticObjectRef(
                source_identity, "source_window", window.window_manifest_sha256)
            or child.window_manifest.canonical_hash != window.window_manifest_sha256
            or child.window_manifest.source_id != window.source_id
            or child.window_manifest.source_sha256 != window.source_sha256
            or child.window_manifest.source_clock_id != window.source_clock_id
            or child.frame_pts_index != child.window_manifest.frame_pts_index_set
            or child.window_manifest.frame_pts_index_set_sha256 != committed.request_identity.frame_pts_index_set_sha256
            or child.semantic_pack.canonical_hash != pack.canonical_hash
            or child.semantic_pack.request_identity_sha256 != committed.request_identity.canonical_hash
            or child.source_blob != window.proxy_blob):
        raise EditorialTimedMediaInputError("candidate does not join the exact VLM/Source/window/frame owner")
    matches = tuple((ordinal, raw) for ordinal, raw in enumerate(pack.candidate_hypotheses)
                    if raw.candidate_id == candidate.candidate_ref.object_id)
    if len(matches) != 1:
        raise EditorialTimedMediaInputError("Catalog candidate has no unique raw VLM hypothesis")
    ordinal, raw = matches[0]
    if (raw.local_candidate_id != candidate.local_candidate_id
            or raw.support.core_owner_window_manifest_sha256 != window.window_manifest_sha256
            or raw.support.source_interval.source_time_base != child.window_manifest.source_time_base
            or tuple(mode.value for mode in raw.editing_modes) != candidate.editing_modes):
        raise EditorialTimedMediaInputError("raw candidate support or editing modes differ from Catalog")
    # The full batch reader already replays every candidate in raw pack order.
    # Keep the index member plus ordinal, not a fabricated per-candidate member.
    return EditorialTimedCandidateBinding(
        ref, candidate.candidate_ref, candidate.source_ref, candidate.source_window_ref,
        episode_index, ordinal, canonical_sha256(raw.to_mapping()), raw.editing_modes,
        batch.child_member_references[episode_index],
    )


def read_committed_editorial_timed_media_inputs(
    store: EditorialTimedMediaStore, *, stage3_request: BuildEditorialBlueprintRequest,
    stage3_outcome: CommandOutcome, media_batch_request: FinalizeTimedMediaEvidenceBatchRequest,
    media_batch_outcome: CommandOutcome,
    authority_profile_resolver: InstalledLocalRunProfileResolver, limits: TimedMediaReadLimits,
) -> CommittedEditorialTimedMediaInputs:
    """Audit both predecessors, then join all alternatives without choosing cuts.

    Physical compilation later uses a binding's episode index and the retained
    actual batch request with read_committed_timed_media_evidence. Neither ASR
    nor VAD is fed back into semantic generation by this seam.
    """
    if (type(stage3_request) is not BuildEditorialBlueprintRequest  # noqa: E721
            or type(media_batch_request) is not FinalizeTimedMediaEvidenceBatchRequest  # noqa: E721
            or type(authority_profile_resolver) is not InstalledLocalRunProfileResolver  # noqa: E721
            or type(limits) is not TimedMediaReadLimits):  # noqa: E721
        raise EditorialTimedMediaInputError("join requires exact requests, installed resolver and limits")
    succeeded_outcome_mapping(stage3_outcome)
    succeeded_outcome_mapping(media_batch_outcome)
    selector = stage3_request.stage2_request.stage1_request.inputs
    if (stage3_request.job != media_batch_request.job
            or stage3_request.artifact_scope != media_batch_request.artifact_scope
            or stage3_outcome.job_id != media_batch_outcome.job_id
            or any(child.request.semantic_inputs_request != selector
                   or child.outcome.job_id != stage3_outcome.job_id
                   for child in media_batch_request.children)):
        raise EditorialTimedMediaInputError("editorial/media Job or complete semantic selector differs")
    editorial = read_committed_editorial_blueprints(store, stage3_request, stage3_outcome)
    predecessors = read_committed_editorial_blueprint_inputs(
        store, stage2_request=stage3_request.stage2_request,
        stage2_outcome=stage3_request.stage2_outcome,
    )
    semantic = predecessors.semantic
    source = semantic.source_manifest
    source_identity = SemanticMemberIdentity.from_committed_member_reference(selector.source_manifest)
    if (editorial.record.job_id != source.job_id
            or predecessors.narrative.record.job_id != source.job_id
            or predecessors.portfolio.record.job_id != source.job_id
            or semantic.vlm_semantic_pack_set != selector.vlm_semantic_pack_set
            or source.receipt_id != selector.source_manifest.receipt_id
            or source.artifact_set_id != selector.source_manifest.artifact_set_id
            or any(child.request.source_manifest_receipt_id != source.receipt_id
                   or child.request.source_manifest_artifact_set_id != source.artifact_set_id
                   or child.request.source_manifest_command_slot_id != source.command_slot_id
                   or child.request.source_manifest_reference != source.reference
                   or child.request.source_provenance_sha256 != source.canonical_hash
                   for child in media_batch_request.children)):
        raise EditorialTimedMediaInputError("editorial/media committed Source or predecessor Job differs")
    semantic.source_grant.require_purpose("semantic_analysis")
    decoded_source = decode_candidate_source_context(semantic)  # Also requires render_source.
    if len(decoded_source.episodes) != len(media_batch_request.children):
        raise EditorialTimedMediaInputError("media batch does not cover the complete semantic Source")
    batch = read_committed_timed_media_evidence_batch(
        store, media_batch_request, media_batch_outcome,
        authority_profile_resolver=authority_profile_resolver, limits=limits,
    )
    catalog = predecessors.portfolio.values.business.candidate_catalog
    catalog_identity = SemanticMemberIdentity.from_artifact_member(predecessors.portfolio.record.artifacts[0])
    candidates = {SemanticObjectRef(catalog_identity, "candidate", value.candidate_id): value
                  for value in catalog.candidates}
    packs = {_pack_identity(value): value for value in semantic.inputs}
    if len(packs) != len(semantic.inputs):
        raise EditorialTimedMediaInputError("committed VLM pack owners are not unique")
    cached: dict[SemanticObjectRef, EditorialTimedCandidateBinding] = {}
    alternatives: list[EditorialTimedAlternativeBinding] = []
    for story in editorial.values.business.projection.blueprints:
        for beat in story.beats:
            for requirement in beat.evidence_requirements:
                for alternative in requirement.alternatives:
                    bindings: list[EditorialTimedCandidateBinding] = []
                    for ref in alternative.candidate_refs:
                        if ref not in cached:
                            candidate = candidates.get(ref)
                            if candidate is None:
                                raise EditorialTimedMediaInputError("alternative has a foreign Catalog candidate")
                            committed = packs.get(candidate.candidate_ref.member_ref)
                            if committed is None:
                                raise EditorialTimedMediaInputError("Catalog candidate has a foreign raw VLM owner")
                            cached[ref] = _bind_candidate(
                                ref, candidate, committed, source_identity, media_batch_request, batch,
                            )
                        bindings.append(cached[ref])
                    alternatives.append(EditorialTimedAlternativeBinding(
                        story.story_id, beat, requirement, alternative, tuple(bindings),
                    ))
    return CommittedEditorialTimedMediaInputs(
        editorial, predecessors, media_batch_request, batch, tuple(alternatives),
    )
