"""Shared committed Source/VLM fixture for the editorial-to-media join tests.

Persistence, model replies and media detector I/O use deterministic test
doubles. Stage 1--3 Commands, Prepare, committed readers and the finalizer use
production code. Media rows retain the same Source manifest, VLM aggregate,
and Kernel Job identity as Stage 3; this is not real-model acceptance.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from typing import Any
from uuid import uuid4

from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes
from autocut_kernel.media.stage4_predecessor import (
    PresentationTrackSegment,
    RationalPresentationInterval,
)
from autocut_kernel.pipeline.build_editorial_blueprint_command import (
    BuildEditorialBlueprintCommand,
)
from autocut_kernel.pipeline.committed_timed_media import TimedMediaReadLimits
from autocut_kernel.pipeline.editorial_blueprint_inputs import (
    read_committed_editorial_blueprint_inputs,
)
from autocut_kernel.pipeline.finalize_timed_media_evidence_batch_command import (
    FINALIZE_TIMED_MEDIA_EVIDENCE_BATCH_COMMAND,
    FinalizeTimedMediaEvidenceBatchCommand,
    FinalizeTimedMediaEvidenceBatchRequest,
    TimedMediaEvidenceBatchChild,
)
from autocut_kernel.pipeline.prepare_timed_media_evidence_command import (
    PrepareTimedMediaEvidenceCommand,
    PrepareTimedMediaEvidenceRequest,
    ProducedTimedMediaEvidence,
    resolve_committed_timed_media_request,
    timed_media_request_hash,
)
from autocut_kernel.registry.installed_runtime import InstalledLocalRunProfileResolver
from autocut_kernel.registry.timed_speech import StoreAnchoredTimedSpeechProfileResolver
from autocut_kernel.semantic_chain.member_refs import SemanticMemberIdentity, SemanticObjectRef
from autocut_kernel.source_manifest import _expected_presentation_segments, decode_source_manifest
from autocut_kernel.store.models import (
    CommandOutcome,
    CommittedArtifactMemberReference,
    PersistedCommittedArtifactMember,
    PersistedCommittedArtifactSet,
    artifact_set_hash,
    canonical_payload_hash,
)

from tests.authority.test_committed_timed_media import (
    _installed_producer,
    _installed_resource,
    _limits,
    _ReaderStore,
)
from tests.authority.test_committed_timed_media_batch import _BatchStore
from tests.authority.test_installed_runtime import _bootstrapped
from tests.media.test_prepare_timed_media_evidence_command import _request
from tests.semantic_chain.test_build_editorial_blueprint_command import (
    ScriptedDraftProvider,
    command_case_stage3,
)
from tests.semantic_chain.test_material_support import (
    _long_material_inputs as _original_long_material_inputs,
)


def _long_av_inputs(payload_change=None) -> object:
    """Extend the existing long video fixture to a coherent ten-second A/V Source.

    The original Stage 2 fixture intentionally changes only video because it
    has no physical consumer.  This join fixture changes its Source audio
    clock/presentation rows *before* all Stage commands run, then updates the
    immutable VLM child Source provenance to the newly decoded Source.
    """
    def with_second_candidate(payload: object) -> None:
        if payload_change is not None:
            payload_change(payload)
        assert type(payload) is dict
        raw_candidates = payload["candidate_hypotheses"]
        assert type(raw_candidates) is list and raw_candidates
        second = deepcopy(raw_candidates[0])
        assert type(second) is dict
        second["local_candidate_id"] = "candidate_2"
        raw_candidates.append(second)

    inputs = _original_long_material_inputs(with_second_candidate)
    source = inputs.source_manifest
    decoded = decode_source_manifest(source.payload_json, source.proxy_blobs)
    episode = decoded.episodes[0]
    original = episode.media_probe.audio_sample_boundaries
    multiplier = 4_800
    audio_context = replace(original.context, duration_tick=original.context.duration_tick * multiplier)
    audio_coverage = replace(
        original.coverage,
        in_tick=audio_context.origin_tick,
        out_tick=audio_context.end_tick,
    )
    audio = replace(
        original,
        context=audio_context,
        coverage=audio_coverage,
        points=tuple(replace(point, tick=point.tick * multiplier) for point in original.points),
    )
    presentation = episode.media_probe.presentation_timeline_probe
    assert presentation is not None
    audio_boundaries = (
        (audio_context.origin_tick, audio_context.origin_tick + audio_context.duration_tick // 2),
        (audio_context.origin_tick + audio_context.duration_tick // 2, audio_context.end_tick),
    )
    audio_track = replace(
        presentation.audio,
        origin_tick=audio_context.origin_tick,
        end_tick=audio_context.end_tick,
        index_sha256=audio.canonical_hash,
        segments=tuple(PresentationTrackSegment(
            tick_range,
            RationalPresentationInterval.from_fractions(
                Fraction(tick_range.start_pts, 48_000),
                Fraction(tick_range.end_pts, 48_000),
            ),
            boundary_hash,
            continuity,
        ) for tick_range, continuity, boundary_hash in _expected_presentation_segments(audio_boundaries)),
    )
    presentation = replace(
        presentation,
        audio=audio_track,
        audio_sample_boundary_set_sha256=audio.canonical_hash,
    )
    probe = replace(
        episode.media_probe,
        audio_sample_boundaries=audio,
        presentation_timeline_probe=presentation,
        presentation_audio_frame_boundaries=audio_boundaries,
    )
    decoded = replace(decoded, episodes=(replace(episode, media_probe=probe),))
    payload_json = canonical_json_bytes(decoded.to_mapping()).decode("utf-8")
    assert decode_source_manifest(payload_json, source.proxy_blobs) == decoded
    source = replace(
        source,
        payload_json=payload_json,
        reference=replace(source.reference, content_hash=canonical_payload_hash(payload_json)),
    )
    committed = inputs.inputs[0]
    child = committed.semantic_pack.source_child
    request_payload = json.loads(child.payload_json)
    request_payload["source_manifest_sha256"] = source.reference.content_hash
    request_payload["source_provenance_sha256"] = source.canonical_hash
    request_json = canonical_json_bytes(request_payload).decode("utf-8")
    child = replace(
        child,
        source_manifest_sha256=source.reference.content_hash,
        source_provenance_sha256=source.canonical_hash,
        payload_json=request_json,
        reference=replace(child.reference, content_hash=canonical_payload_hash(request_json)),
    )
    persisted = replace(committed.semantic_pack, source_child=child)
    committed = replace(committed, semantic_pack=persisted)
    return replace(inputs, source_manifest=source, inputs=(committed,))


class EditorialTimedMediaStore:
    """Route exact semantic and exact timed-media Store reads to their rows."""

    def __init__(self, editorial: Any, media: _BatchStore) -> None:
        self.editorial = editorial
        self.media = media

    def read_committed_artifact_set(self, job, **expected):  # type: ignore[no-untyped-def]
        command = expected["expected_command_name"]
        if command in (
            "PrepareTimedMediaEvidence@2.1.3",
            FINALIZE_TIMED_MEDIA_EVIDENCE_BATCH_COMMAND,
        ):
            return self.media.read_committed_artifact_set(job, **expected)
        return self.editorial.read_committed_artifact_set(job, **expected)

    def read_committed_generation_attempt_chain(self, job, **expected):  # type: ignore[no-untyped-def]
        return self.editorial.read_committed_generation_attempt_chain(job, **expected)

    def read_immutable_blob(self, job, reference):  # type: ignore[no-untyped-def]
        return self.editorial.read_immutable_blob(job, reference)

    def read_committed_semantic_inputs(self, request):  # type: ignore[no-untyped-def]
        return self.editorial.read_committed_semantic_inputs(request)

    def read_whole_series_source_manifest(self, job, artifact_set_id):  # type: ignore[no-untyped-def]
        return self.media.read_whole_series_source_manifest(job, artifact_set_id)

    def read_bootstrapped_timed_speech_profile(self, snapshot):  # type: ignore[no-untyped-def]
        return self.media.read_bootstrapped_timed_speech_profile(snapshot)

    def materialize_immutable_blob(self, job, reference, limits):  # type: ignore[no-untyped-def]
        return self.media.materialize_immutable_blob(job, reference, limits)

    def __getattr__(self, name: str) -> object:
        return getattr(self.media, name)


def _draft_with_all_catalog_candidates(store: object, request: object, raw: bytes) -> bytes:
    """Keep an unchosen `one_of` candidate in every real editorial alternative."""
    predecessor = read_committed_editorial_blueprint_inputs(
        store,
        stage2_request=request.stage2_request,
        stage2_outcome=request.stage2_outcome,
    )
    catalog = predecessor.portfolio.record.artifacts[0]
    identity = SemanticMemberIdentity.from_artifact_member(catalog)
    # The typed Stage 2 reader value is already retained by the successful
    # command result.  Its Catalog is the only source for editorial references.
    options = tuple(
        SemanticObjectRef(identity, "candidate", item.candidate_id).to_mapping()
        for item in predecessor.portfolio.values.business.candidate_catalog.candidates
    )
    draft = json.loads(raw)
    assert type(draft) is dict
    stories = draft["stories"]
    assert type(stories) is list
    for story in stories:
        assert type(story) is dict
        beats = story["beats"]
        assert type(beats) is list
        for beat in beats:
            assert type(beat) is dict
            requirements = beat["evidence_requirements"]
            assert type(requirements) is list
            for requirement in requirements:
                assert type(requirement) is dict
                alternatives = requirement["alternative_sets"]
                assert type(alternatives) is list
                for alternative in alternatives:
                    assert type(alternative) is dict
                    alternative["candidate_refs"] = list(options)
                second_alternative = deepcopy(alternatives[0])
                second_alternative["alternative_id"] = "unchosen-secondary"
                second_alternative["candidate_refs"] = list(reversed(options))
                alternatives.append(second_alternative)
    return canonical_json_bytes(draft)


def _same_source_request(
    store: _BatchStore,
    template: PrepareTimedMediaEvidenceRequest,
    *,
    inputs: object,
) -> PrepareTimedMediaEvidenceRequest:
    """Close the existing deterministic request over the Stage 1 Source/VLM."""
    semantic = inputs
    source = semantic.source_manifest
    episode = semantic.inputs[0]
    manifest = decode_source_manifest(source.payload_json, source.proxy_blobs).episodes[0]
    selector = template.semantic_inputs_request
    selector = replace(
        selector,
        job=source.source_job,
        source_manifest=CommittedArtifactMemberReference(
            source.receipt_id,
            source.artifact_set_id,
            0,
            source.reference.scope,
            source.reference.artifact_type,
            source.reference.logical_id,
            source.reference.revision,
            source.reference.content_hash,
        ),
        vlm_semantic_pack_set=semantic.vlm_semantic_pack_set,
    )
    return replace(
        template,
        job=source.source_job,
        artifact_scope=source.reference.scope,
        source_blob=source.proxy_blobs[0],
        source_manifest_reference=source.reference,
        source_manifest_receipt_id=source.receipt_id,
        source_manifest_artifact_set_id=source.artifact_set_id,
        source_manifest_command_slot_id=source.command_slot_id,
        source_provenance_sha256=source.canonical_hash,
        semantic_inputs_request=selector,
        window_manifest=manifest.manifest,
        semantic_pack=episode.semantic_pack.semantic_pack,
        frame_pts_index=manifest.manifest.frame_pts_index_set,
        audio_sample_boundaries=manifest.media_probe.audio_sample_boundaries,
        frame_detector_sha256=manifest.media_probe.frame_detector_sha256,
        audio_detector_sha256=manifest.media_probe.audio_detector_sha256,
        adaptive_policy=replace(template.adaptive_policy, time_base=manifest.manifest.source_time_base),
    )


def _persist_media_record(
    store: _BatchStore,
    request: PrepareTimedMediaEvidenceRequest,
    resolver: InstalledLocalRunProfileResolver,
) -> CommandOutcome:
    command = PrepareTimedMediaEvidenceCommand(
        store,
        _same_source_producer(resolver.resource),
        StoreAnchoredTimedSpeechProfileResolver(resolver.snapshot),
    )
    result = command.execute(request)
    assert result.outcome.state == "succeeded", result.outcome.failure_detail_json
    success = store.successes[-1]
    source = store.source_manifest
    assert source is not None
    receipt_id, artifact_set_id = uuid4(), uuid4()
    members = tuple(
        PersistedCommittedArtifactMember(
            CommittedArtifactMemberReference(
                receipt_id, artifact_set_id, ordinal, artifact.scope, artifact.artifact_type,
                artifact.logical_id, artifact.revision, artifact.content_hash,
            ),
            artifact.payload_json,
            success.command_slot_id,
        )
        for ordinal, artifact in enumerate(success.artifacts)
    )
    resolved = resolve_committed_timed_media_request(store, request)
    record = PersistedCommittedArtifactSet(
        request.job,
        source.job_id,
        success.command_slot_id,
        receipt_id,
        artifact_set_id,
        timed_media_request_hash(resolved, resolver.snapshot),
        "PrepareTimedMediaEvidence@2.1.3",
        "deterministic",
        artifact_set_hash(success.artifacts),
        members,
    )
    store.record = record
    outcome = CommandOutcome(
        success.command_slot_id,
        "succeeded",
        receipt_id=receipt_id,
        artifact_set_id=artifact_set_id,
        job_id=source.job_id,
    )
    store.child_records[(outcome.command_slot_id, outcome.receipt_id, outcome.artifact_set_id)] = record
    return outcome


def _same_source_producer(resource: object) -> object:
    """Test I/O producer whose complete video evidence uses the Source clock.

    The existing producer fixture already rebuilds the exact Frame/Audio
    indexes and accepted ASR/VAD identity.  Its static visual fixture has the
    original video time base, so this narrow test adapter rebuilds the other
    *physical* video records over the committed request clock too.  It does
    not bypass the production root codec or producer validation.
    """
    base = _installed_producer(resource)

    class SameSourceProducer:
        def prepare(self, request, source):  # type: ignore[no-untyped-def]
            template = base.bundle
            target = request.frame_pts_index.context
            audio_target = request.audio_sample_boundaries.context

            def context(value):  # type: ignore[no-untyped-def]
                return replace(
                    value,
                    clock_id=target.clock_id,
                    time_base=target.time_base,
                    origin_tick=target.origin_tick,
                    duration_tick=target.duration_tick,
                )

            def coverage(value):  # type: ignore[no-untyped-def]
                return replace(
                    value,
                    clock_id=target.clock_id,
                    time_base=target.time_base,
                    in_tick=target.origin_tick,
                    out_tick=target.end_tick,
                )

            def audio_context(value):  # type: ignore[no-untyped-def]
                return replace(
                    value,
                    clock_id=audio_target.clock_id,
                    time_base=audio_target.time_base,
                    origin_tick=audio_target.origin_tick,
                    duration_tick=audio_target.duration_tick,
                )

            def audio_coverage(value):  # type: ignore[no-untyped-def]
                return replace(
                    value,
                    clock_id=audio_target.clock_id,
                    time_base=audio_target.time_base,
                    in_tick=audio_target.origin_tick,
                    out_tick=audio_target.end_tick,
                )

            audio_scale = audio_target.duration_tick // template.audio_sample_boundaries.context.duration_tick

            def audio_record(value):  # type: ignore[no-untyped-def]
                return replace(
                    value,
                    clock_id=audio_target.clock_id,
                    time_base=audio_target.time_base,
                    in_tick=value.in_tick * audio_scale,
                    out_tick=value.out_tick * audio_scale,
                )

            # _Producer._rebind_root_bundle constructs a RootMediaEvidenceBundle
            # before returning ProducedTimedMediaEvidence.  Make the static
            # visual test template coherent first, then let that real helper
            # bind it to the request's exact Frame/Audio indexes.
            frame = replace(
                template.frame_pts_index,
                context=context(template.frame_pts_index.context),
                coverage=coverage(template.frame_pts_index.coverage),
            )
            base.bundle = replace(
                template,
                frame_pts_index=frame,
                shot_boundaries=replace(
                    template.shot_boundaries,
                    context=context(template.shot_boundaries.context),
                    coverage=coverage(template.shot_boundaries.coverage),
                    frame_pts_index_set_sha256=frame.canonical_hash,
                    points=tuple(replace(point, clock_id=target.clock_id, time_base=target.time_base)
                                 for point in template.shot_boundaries.points),
                ),
                scene_boundaries=replace(
                    template.scene_boundaries,
                    context=context(template.scene_boundaries.context),
                    coverage=coverage(template.scene_boundaries.coverage),
                    frame_pts_index_set_sha256=frame.canonical_hash,
                    points=tuple(replace(point, clock_id=target.clock_id, time_base=target.time_base)
                                 for point in template.scene_boundaries.points),
                ),
                visual_validity=replace(
                    template.visual_validity,
                    context=context(template.visual_validity.context),
                    coverage=coverage(template.visual_validity.coverage),
                    intervals=tuple(replace(item, clock_id=target.clock_id, time_base=target.time_base)
                                    for item in template.visual_validity.intervals),
                ),
                subtitle_cues=replace(
                    template.subtitle_cues,
                    context=context(template.subtitle_cues.context),
                    coverage=coverage(template.subtitle_cues.coverage),
                    cues=tuple(replace(
                        item,
                        clock_id=target.clock_id,
                        time_base=target.time_base,
                        timing_error_bound=replace(item.timing_error_bound, time_base=target.time_base),
                    ) for item in template.subtitle_cues.cues),
                ),
                audio_sample_boundaries=replace(
                    template.audio_sample_boundaries,
                    context=audio_context(template.audio_sample_boundaries.context),
                    coverage=audio_coverage(template.audio_sample_boundaries.coverage),
                    points=tuple(replace(
                        point,
                        clock_id=audio_target.clock_id,
                        time_base=audio_target.time_base,
                        tick=point.tick * audio_scale,
                    ) for point in template.audio_sample_boundaries.points),
                ),
                transcript=replace(
                    template.transcript,
                    context=audio_context(template.transcript.context),
                    coverage=audio_coverage(template.transcript.coverage),
                    segments=tuple(audio_record(item) for item in template.transcript.segments),
                    words=tuple(audio_record(item) for item in template.transcript.words),
                    sentences=tuple(audio_record(item) for item in template.transcript.sentences),
                ),
                speech_activity=replace(
                    template.speech_activity,
                    context=audio_context(template.speech_activity.context),
                    coverage=audio_coverage(template.speech_activity.coverage),
                    segments=tuple(audio_record(item) for item in template.speech_activity.segments),
                ),
            )
            produced = base.prepare(request, source)
            if type(produced) is not ProducedTimedMediaEvidence:  # noqa: E721
                raise AssertionError("fixture producer returned an unexpected evidence value")
            assert produced.root_bundle.shot_boundaries.frame_pts_index_set_sha256 == request.frame_pts_index.canonical_hash, (
                produced.root_bundle.shot_boundaries.frame_pts_index_set_sha256,
                request.frame_pts_index.canonical_hash,
            )
            return produced

    return SameSourceProducer()


def editorial_timed_media_case(tmp_path: Path, monkeypatch) -> tuple[  # type: ignore[no-untyped-def]
    EditorialTimedMediaStore,
    object,
    CommandOutcome,
    FinalizeTimedMediaEvidenceBatchRequest,
    CommandOutcome,
    InstalledLocalRunProfileResolver,
    TimedMediaReadLimits,
]:
    """One actual Stage 1--3 chain plus its exact same-Source media batch."""
    import tests.semantic_chain.test_material_support as material_support

    with monkeypatch.context() as context:
        context.setattr(material_support, "_long_material_inputs", _long_av_inputs)
        editorial_store, _provider, stage3_request, raw = command_case_stage3()
    provider = ScriptedDraftProvider(_draft_with_all_catalog_candidates(
        editorial_store, stage3_request, raw,
    ))
    stage3 = BuildEditorialBlueprintCommand(editorial_store, provider).execute(stage3_request)
    assert stage3.outcome.state == "succeeded" and stage3.committed is not None

    resource, anchor = _installed_resource(tmp_path, monkeypatch)
    media = _BatchStore(anchor, tmp_path)
    semantic = editorial_store.inputs
    media.source_manifest = semantic.source_manifest
    media.semantic_inputs = semantic
    for reference in semantic.source_manifest.proxy_blobs:
        media.blobs[reference.object_id] = b"shared synthetic source"
    template_store = _ReaderStore(anchor, tmp_path)
    template = _request(template_store, with_candidates=True)
    request = _same_source_request(media, template, inputs=semantic)
    resolver = InstalledLocalRunProfileResolver(resource)
    bootstrapped = _bootstrapped(resource)
    media.bootstrapped_reference = bootstrapped.reference
    media.bootstrapped_entry = bootstrapped.entry
    media_outcome = _persist_media_record(media, request, resolver)
    batch_request = FinalizeTimedMediaEvidenceBatchRequest(
        request.job,
        "media-preflight:batch:editorial",
        request.artifact_scope,
        request.artifact_revision,
        (TimedMediaEvidenceBatchChild(request, media_outcome),),
    )
    batch_result = FinalizeTimedMediaEvidenceBatchCommand(media, resolver, _limits(request)).execute(batch_request)
    assert batch_result.outcome.state == "succeeded"
    return (
        EditorialTimedMediaStore(editorial_store, media),
        stage3_request,
        stage3.outcome,
        batch_request,
        batch_result.outcome,
        resolver,
        _limits(request),
    )
