from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from array import array
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest
from autocut_kernel.media.types import TickRange, TimeBase
from autocut_kernel.physical_edit.candidate_dialogue_guard import CandidateDialogueGuard
from autocut_kernel.physical_edit.candidate_exact_span import CandidateExactSpanResult
from autocut_kernel.physical_edit.candidate_timed_speech_authority import (
    CandidateTimedSpeechAuthorityKind,
)
from autocut_kernel.physical_edit.dialogue_guard import DialogueGuardKind, DialogueRequirement
from autocut_kernel.physical_edit.editorial_exact_span import EditorialExactSpanQuery
from autocut_kernel.physical_edit.exact_span import (
    BoundaryProof,
    ExactAvSpanRequest,
    VideoClockRange,
)
from autocut_kernel.pipeline.production_recipe import (
    PRODUCTION_RECIPE_PRODUCER_ID,
    ProductionBeat,
    ProductionRecipe,
    ProductionSpan,
    ProductionStory,
)
from autocut_kernel.rendering.production_render_plan import (
    PRODUCTION_AV_H264_AAC_PROFILE,
    ProductionAvRenderProfile,
    ProductionRenderPlanError,
    bind_production_render_invocation,
    build_production_render_plan,
)
from autocut_kernel.store.models import ArtifactScope, BlobRef, CommittedArtifactMemberReference
from autocut_kernel.vlm.models import VlmEditingMode


def _hash(character: str) -> str:
    return "sha256:" + character * 64


def _source_manifest_ref(*, receipt_character: str = "2") -> CommittedArtifactMemberReference:
    return CommittedArtifactMemberReference(
        UUID(
            f"{receipt_character * 8}-{receipt_character * 4}-4{receipt_character * 3}-8{receipt_character * 3}-{receipt_character * 12}"
        ),
        UUID("33333333-3333-4333-8333-333333333333"),
        0,
        ArtifactScope("pipeline", "job", "run-1"),
        "whole_series_source_manifest",
        "whole_series_source_manifest",
        1,
        _hash("c"),
    )


def _query(
    *,
    requirement_id: str,
    candidate_id: str,
    source_sha256: str,
    source_id: str,
    video_time_base: TimeBase,
    video_in_tick: int,
    video_out_tick: int,
) -> EditorialExactSpanQuery:
    selected = TickRange(video_in_tick, video_out_tick)
    clock = VideoClockRange(
        source_id,
        source_sha256,
        f"{source_id}:video_pts",
        video_time_base,
        TickRange(video_in_tick - 1, video_out_tick + 1),
    )
    return EditorialExactSpanQuery(
        story_id="story-1",
        beat_id="beat-1",
        evidence_requirement_id=requirement_id,
        alternative_id=f"alternative-{requirement_id}",
        candidate_id=candidate_id,
        anchor_event_id="event-1",
        anchor_event_sha256=_hash("3"),
        span_intent="tight",
        dominant_editing_mode=VlmEditingMode.ACTION,
        policy_sha256=_hash("4"),
        blueprint_beat_sha256=_hash("2"),
        evidence_requirement_sha256=_hash("5"),
        alternative_sha256=_hash("6"),
        catalog_candidate_sha256=_hash("7"),
        semantic_pack_sha256=_hash("8"),
        timed_evidence_sha256=_hash("9"),
        dialogue_protection_kind="known_speech",
        request=ExactAvSpanRequest(
            clock, replace(clock, tick_range=selected), 1, DialogueRequirement.NOT_REQUIRED
        ),
    )


def _result(
    query: EditorialExactSpanQuery,
    *,
    video_in_tick: int,
    video_out_tick: int,
    audio_in_tick: int,
    audio_out_tick: int,
    audio_time_base: TimeBase,
) -> CandidateExactSpanResult:
    video_time_base = query.request.desired_video_range.time_base
    source_id = query.request.desired_video_range.source_id
    proof = BoundaryProof(
        source_id,
        query.request.desired_video_range.source_sha256,
        f"{source_id}:video_pts",
        video_time_base,
        video_in_tick,
        video_out_tick,
        f"{source_id}:audio_sample",
        audio_time_base,
        audio_in_tick,
        audio_out_tick,
        _hash("a"),
        _hash("b"),
        _hash("c"),
        _hash("d"),
        _hash("e"),
    )
    guard = CandidateDialogueGuard(
        root_evidence_sha256=_hash("f"),
        candidate_evidence_sha256=query.timed_evidence_sha256,
        candidate_window_sha256=_hash("a"),
        window_plan_sha256=_hash("b"),
        timed_speech_authority_sha256=_hash("c"),
        original_authority_kind=CandidateTimedSpeechAuthorityKind.INSTALLED_CPU_PROFILE,
        original_authority_sha256=_hash("d"),
        guard_policy_sha256=_hash("e"),
        source_id=source_id,
        source_sha256=query.request.desired_video_range.source_sha256,
        source_audio_clock_id=f"{source_id}:audio_sample",
        source_audio_time_base=audio_time_base,
        source_audio_range=TickRange(audio_in_tick - 1, audio_out_tick + 1),
        requirement=DialogueRequirement.NOT_REQUIRED,
        kind=DialogueGuardKind.NOT_REQUIRED,
        reason="blueprint_does_not_require_complete_dialogue",
        protected_ranges=(),
    )
    return CandidateExactSpanResult(
        video_range=TickRange(video_in_tick, video_out_tick),
        audio_range=TickRange(audio_in_tick, audio_out_tick),
        boundary_proof=proof,
        dialogue_guard=guard,
        common_segment_ordinal=0,
        canonical_decision_key=(
            0,
            0,
            0,
            0,
            video_out_tick - video_in_tick,
            audio_out_tick - audio_in_tick,
            video_in_tick,
            video_out_tick,
            audio_in_tick,
            audio_out_tick,
        ),
        logical_cartesian_count_decimal="16",
        visited_av_pair_count=4,
        feasible_count=2,
        request_sha256=query.request.canonical_hash,
        policy_sha256=_hash("f"),
        candidate_domain_sha256=_hash("a"),
        feasible_relation_sha256=_hash("b"),
    )


def _span(
    *,
    ordinal: int,
    requirement_id: str,
    candidate_id: str,
    source_blob: BlobRef,
    source_manifest_ref: CommittedArtifactMemberReference | None = None,
    source_id: str = "source-1",
    video_time_base: TimeBase = TimeBase(1, 90_000),
    video_in_tick: int = 10,
    video_out_tick: int = 100,
    audio_time_base: TimeBase = TimeBase(1, 48_000),
    audio_in_tick: int = 5,
    audio_out_tick: int = 53,
) -> ProductionSpan:
    query = _query(
        requirement_id=requirement_id,
        candidate_id=candidate_id,
        source_sha256=source_blob.content_hash,
        source_id=source_id,
        video_time_base=video_time_base,
        video_in_tick=video_in_tick,
        video_out_tick=video_out_tick,
    )
    return ProductionSpan.from_exact_span(
        ordinal=ordinal,
        source_blob=source_blob,
        source_manifest_ref=source_manifest_ref or _source_manifest_ref(),
        query=query,
        result=_result(
            query,
            video_in_tick=video_in_tick,
            video_out_tick=video_out_tick,
            audio_in_tick=audio_in_tick,
            audio_out_tick=audio_out_tick,
            audio_time_base=audio_time_base,
        ),
    )


def _recipe(*spans: ProductionSpan) -> ProductionRecipe:
    selected = spans or (
        _span(
            ordinal=0,
            requirement_id="requirement-1",
            candidate_id="candidate-1",
            source_blob=BlobRef(
                UUID("11111111-1111-4111-8111-111111111111"),
                _hash("1"),
                4096,
                "video/mp4",
            ),
        ),
    )
    beat = ProductionBeat(0, "beat-1", _hash("2"), tuple(selected))
    story = ProductionStory(0, "story-1", _hash("3"), (beat,))
    return ProductionRecipe(
        PRODUCTION_RECIPE_PRODUCER_ID,
        "render-production-v1",
        _hash("4"),
        story,
    )


def test_builds_path_independent_exact_av_plan() -> None:
    recipe = _recipe()

    first = build_production_render_plan(recipe)
    second = build_production_render_plan(recipe)

    assert first == second
    assert first.recipe_sha256 == recipe.canonical_hash
    assert first.story_id == "story-1"
    assert len(first.inputs) == len(first.segments) == 1
    assert "trim=start_pts=10:end_pts=100" in first.filter_graph
    assert "atrim=start_pts=5:end_pts=53" in first.filter_graph
    assert "setpts=PTS-STARTPTS" in first.filter_graph
    assert "asetpts=PTS-STARTPTS" in first.filter_graph
    assert "source.mp4" not in str(first.to_mapping())
    assert first.output_timescale == 90_000


def test_preserves_span_order_while_deduplicating_source_inputs() -> None:
    source = BlobRef(
        UUID("11111111-1111-4111-8111-111111111111"),
        _hash("1"),
        4096,
        "video/mp4",
    )
    first = _span(
        ordinal=0,
        requirement_id="requirement-1",
        candidate_id="candidate-1",
        source_blob=source,
    )
    second = _span(
        ordinal=1,
        requirement_id="requirement-2",
        candidate_id="candidate-2",
        source_blob=source,
        video_in_tick=110,
        video_out_tick=200,
        audio_in_tick=60,
        audio_out_tick=108,
    )

    plan = build_production_render_plan(_recipe(first, second))

    assert len(plan.inputs) == 1
    assert tuple(item.candidate_id for item in plan.segments) == (
        "candidate-1",
        "candidate-2",
    )
    assert plan.filter_graph.endswith("[v0][a0][v1][a1]concat=n=2:v=1:a=1[vout][aout]")


def test_rejects_unreconciled_av_duration_difference() -> None:
    source = BlobRef(
        UUID("11111111-1111-4111-8111-111111111111"),
        _hash("1"),
        4096,
        "video/mp4",
    )
    span = _span(
        ordinal=0,
        requirement_id="requirement-1",
        candidate_id="candidate-1",
        source_blob=source,
        audio_out_tick=54,
    )

    with pytest.raises(ProductionRenderPlanError, match="durations differ"):
        build_production_render_plan(_recipe(span))


def test_rejects_duration_not_representable_as_integer_output_samples() -> None:
    source = BlobRef(
        UUID("11111111-1111-4111-8111-111111111111"),
        _hash("1"),
        4096,
        "video/mp4",
    )
    span = _span(
        ordinal=0,
        requirement_id="requirement-1",
        candidate_id="candidate-1",
        source_blob=source,
        video_time_base=TimeBase(1, 44_100),
        video_in_tick=10,
        video_out_tick=11,
        audio_time_base=TimeBase(1, 44_100),
        audio_in_tick=20,
        audio_out_tick=21,
    )

    with pytest.raises(ProductionRenderPlanError, match="output audio samples"):
        build_production_render_plan(_recipe(span))


def test_binding_requires_exact_blob_set_and_keeps_paths_out_of_plan_hash(
    tmp_path: Path,
) -> None:
    plan = build_production_render_plan(_recipe())
    source = plan.inputs[0].source_blob
    source_path = (tmp_path / "source.mp4").absolute()
    output_path = (tmp_path / "output.mp4").absolute()
    before = plan.canonical_hash

    invocation = bind_production_render_invocation(
        plan,
        source_paths={source: source_path},
        output_path=output_path,
    )

    assert invocation.plan_sha256 == before == plan.canonical_hash
    assert str(source_path) in invocation.argv
    assert str(output_path) == invocation.argv[-1]
    assert "-ss" not in invocation.argv and "-to" not in invocation.argv
    assert "-c:a" in invocation.argv and "-an" not in invocation.argv
    with pytest.raises(ProductionRenderPlanError, match="every exact input"):
        bind_production_render_invocation(
            plan,
            source_paths={},
            output_path=output_path,
        )


def test_rejects_odd_output_geometry_and_profile_drift(tmp_path: Path) -> None:
    with pytest.raises(ProductionRenderPlanError, match="dimensions must be even"):
        ProductionAvRenderProfile(width=721)
    plan = build_production_render_plan(_recipe())
    source = plan.inputs[0].source_blob
    changed = replace(PRODUCTION_AV_H264_AAC_PROFILE, crf=20)

    with pytest.raises(ProductionRenderPlanError, match="profile differs"):
        bind_production_render_invocation(
            plan,
            source_paths={source: (tmp_path / "source.mp4").absolute()},
            output_path=(tmp_path / "output.mp4").absolute(),
            profile=changed,
        )

    injected = replace(plan, filter_graph="movie=/etc/passwd[v0];anullsrc[a0]")
    with pytest.raises(ProductionRenderPlanError, match="filter graph is not trusted"):
        bind_production_render_invocation(
            injected,
            source_paths={source: (tmp_path / "source.mp4").absolute()},
            output_path=(tmp_path / "output.mp4").absolute(),
        )


def test_rejects_conflicting_authority_for_one_source_object() -> None:
    source = BlobRef(
        UUID("11111111-1111-4111-8111-111111111111"),
        _hash("1"),
        4096,
        "video/mp4",
    )
    first = _span(
        ordinal=0,
        requirement_id="requirement-1",
        candidate_id="candidate-1",
        source_blob=source,
    )
    second = _span(
        ordinal=1,
        requirement_id="requirement-2",
        candidate_id="candidate-2",
        source_blob=source,
        source_manifest_ref=_source_manifest_ref(receipt_character="4"),
        video_in_tick=110,
        video_out_tick=200,
        audio_in_tick=60,
        audio_out_tick=108,
    )

    with pytest.raises(ProductionRenderPlanError, match="conflicting authority"):
        build_production_render_plan(_recipe(first, second))


def test_bound_plan_executes_exact_two_source_av_concat(tmp_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.skip("ffmpeg and ffprobe are required")
    source_paths = (
        (tmp_path / "red.mp4").absolute(),
        (tmp_path / "blue.mp4").absolute(),
    )
    output_path = (tmp_path / "output.mp4").absolute()
    sources: list[BlobRef] = []
    for index, (path, color, frequency) in enumerate(
        zip(source_paths, ("red", "blue"), (1000, 2000), strict=True),
        start=1,
    ):
        generated = subprocess.run(
            [
                ffmpeg,
                "-nostdin",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"color=c={color}:size=64x48:rate=25:duration=1",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency={frequency}:sample_rate=48000:duration=1",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-shortest",
                str(path),
            ],
            capture_output=True,
            check=False,
        )
        assert generated.returncode == 0, generated.stderr.decode("utf-8", "replace")
        raw_source = path.read_bytes()
        sources.append(
            BlobRef(
                UUID(
                    f"{index * 11111111:08d}-{index * 1111:04d}-4111-8111-{index * 111111111111:012d}"
                ),
                f"sha256:{hashlib.sha256(raw_source).hexdigest()}",
                len(raw_source),
                "video/mp4",
            )
        )
    spans = tuple(
        _span(
            ordinal=index,
            requirement_id=f"requirement-{index + 1}",
            candidate_id=f"candidate-{index + 1}",
            source_blob=source,
            source_id=f"source-{index + 1}",
            video_time_base=TimeBase(1, 12_800),
            video_in_tick=0,
            video_out_tick=5_120,
            audio_in_tick=0,
            audio_out_tick=19_200,
        )
        for index, source in enumerate(sources)
    )
    profile = ProductionAvRenderProfile(profile_id="production-av-test-v1", width=64, height=48)
    plan = build_production_render_plan(_recipe(*spans), profile=profile)
    invocation = bind_production_render_invocation(
        plan,
        source_paths=dict(zip(sources, source_paths, strict=True)),
        output_path=output_path,
        profile=profile,
    )

    rendered = subprocess.run(
        [ffmpeg, *invocation.argv[1:]],
        capture_output=True,
        check=False,
    )

    assert rendered.returncode == 0, rendered.stderr.decode("utf-8", "replace")
    probed = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,time_base,start_pts,start_time,duration_ts,nb_frames:format=duration",
            "-of",
            "json",
            str(output_path),
        ],
        capture_output=True,
        check=True,
    )
    payload = json.loads(probed.stdout)
    streams = {stream["codec_type"]: stream for stream in payload["streams"]}
    assert set(streams) == {"video", "audio"}
    assert streams["video"]["time_base"] == "1/2880000"
    assert streams["audio"]["time_base"] == "1/48000"
    assert streams["video"]["start_pts"] == streams["audio"]["start_pts"] == 0
    assert streams["video"]["start_time"] == streams["audio"]["start_time"] == "0.000000"
    assert streams["video"]["duration_ts"] == 2_304_000
    assert streams["audio"]["duration_ts"] == 38_400
    assert int(streams["video"]["nb_frames"]) == 20

    def sample_rgb(at_seconds: str) -> tuple[int, int, int]:
        decoded = subprocess.run(
            [
                ffmpeg,
                "-v",
                "error",
                "-ss",
                at_seconds,
                "-i",
                str(output_path),
                "-frames:v",
                "1",
                "-vf",
                "scale=1:1",
                "-pix_fmt",
                "rgb24",
                "-f",
                "rawvideo",
                "-",
            ],
            capture_output=True,
            check=True,
        )
        assert len(decoded.stdout) == 3
        return tuple(decoded.stdout)  # type: ignore[return-value]

    early = sample_rgb("0.1")
    late = sample_rgb("0.6")
    assert early[0] > early[2]
    assert late[2] > late[0]

    def audio_zero_crossings(at_seconds: str) -> int:
        decoded = subprocess.run(
            [
                ffmpeg,
                "-v",
                "error",
                "-ss",
                at_seconds,
                "-i",
                str(output_path),
                "-t",
                "0.1",
                "-map",
                "0:a:0",
                "-ac",
                "1",
                "-ar",
                "48000",
                "-f",
                "s16le",
                "-",
            ],
            capture_output=True,
            check=True,
        )
        samples = array("h")
        samples.frombytes(decoded.stdout)
        return sum(
            1
            for left, right in zip(samples, samples[1:], strict=False)
            if (left < 0 <= right) or (left >= 0 > right)
        )

    early_crossings = audio_zero_crossings("0.1")
    late_crossings = audio_zero_crossings("0.6")
    assert early_crossings > 100
    assert late_crossings > early_crossings * 3 // 2
