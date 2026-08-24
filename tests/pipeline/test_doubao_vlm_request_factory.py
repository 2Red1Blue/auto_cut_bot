from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
from typing import cast
from uuid import uuid4

import pytest
from autocut_kernel.media import (
    Coverage,
    CoverageOutcome,
    EvidenceContext,
    FramePtsIndexSet,
    MediaKind,
    PTSIndex,
    TickRange,
    TimeBase,
)
from autocut_kernel.media.types import canonical_sha256
from autocut_kernel.pipeline import (
    VLM_PARSER_STRATEGY_VERSION,
    GenerateVlmEvidenceRequest,
)
from autocut_kernel.store import BlobRef, Job
from autocut_kernel.store.models import (
    WholeSeriesSourceManifestReference,
    canonical_payload_hash,
    canonical_recipe_scope,
)
from autocut_kernel.vlm import (
    GENERATION_RETRY_STRATEGY_VERSION,
    GenerationRetryPolicy,
    ProxyTimelineMap,
    VlmParsePolicy,
    WindowFrameSample,
    WindowManifest,
    WindowManifestSet,
    WindowProxyBlobRef,
)

from auto_cut_bot.pipeline.source_prep.command import (
    PersistedPreparedSources,
    PreparedSeriesSources,
    PreparedSourceEpisode,
)
from auto_cut_bot.pipeline.source_prep.models import SeriesSource, SeriesSourceCensus
from auto_cut_bot.pipeline.source_prep.probe import SourceMediaProbe
from auto_cut_bot.pipeline.vlm import (
    DOUBAO_ARK_ADAPTER_STRATEGY_VERSION,
    DOUBAO_ARK_PROVIDER_ID,
    DOUBAO_VLM_STAGE_STRATEGY_VERSION,
    VLM_PROMPT_VERSION,
    DoubaoVlmRequestFactory,
    DoubaoVlmRequestPolicy,
    build_doubao_vlm_request,
    vlm_response_schema_json,
)


def _hash(digit: str) -> str:
    return "sha256:" + digit * 64


def _frame_pts_set(time_base: TimeBase) -> FramePtsIndexSet:
    ticks = PTSIndex((1_000, 1_010, 1_050, 1_090, 1_100))
    context = EvidenceContext(
        "source-001",
        _hash("a"),
        MediaKind.VIDEO,
        "video-clock-0",
        time_base,
        1_000,
        100,
        "test-decoder-v1",
        _hash("7"),
    )
    coverage = Coverage(
        "source-001",
        _hash("a"),
        "video-clock-0",
        time_base,
        1_000,
        1_100,
        CoverageOutcome.COMPLETE,
    )
    return FramePtsIndexSet(
        "frame-pts-root-v1",
        context,
        coverage,
        ticks,
        canonical_sha256(list(ticks.ticks)),
    )


def _prepared_episode() -> PreparedSourceEpisode:
    proxy = BlobRef(uuid4(), _hash("b"), 4_096, "video/mp4")
    time_base = TimeBase(1, 1_000)
    manifest = WindowManifest(
        source_id="source-001",
        source_clock_id="video-clock-0",
        source_sha256=_hash("a"),
        stream_index=0,
        source_time_base=time_base,
        source_range=TickRange(1_000, 1_100),
        core_range=TickRange(1_000, 1_100),
        frame_pts_index_set=_frame_pts_set(time_base),
        proxy_blob_ref=WindowProxyBlobRef(
            str(proxy.object_id),
            proxy.content_hash,
            proxy.byte_length,
            proxy.media_type,
        ),
        preprocess_policy_sha256=_hash("c"),
        window_sampling_policy_sha256=_hash("d"),
        timeline_map=ProxyTimelineMap.translation(
            time_base=time_base,
            proxy_range=TickRange(0, 100),
            source_start_pts=1_000,
            max_source_error_pts=1,
        ),
        frame_samples=(
            WindowFrameSample(1_010, 10, _hash("e")),
            WindowFrameSample(1_050, 50, _hash("f")),
        ),
    )
    manifest_set = WindowManifestSet(
        manifest.source_id,
        manifest.source_clock_id,
        manifest.source_sha256,
        manifest.stream_index,
        manifest.source_time_base,
        manifest.core_range,
        (manifest,),
    )
    return PreparedSourceEpisode(
        cast(SourceMediaProbe, _TestProbe()),
        proxy,
        manifest,
        manifest_set,
    )


class _TestProbe:
    def to_mapping(self) -> dict[str, object]:
        return {"test_probe": "provenance-fixture-v1"}


def _parse_policy() -> VlmParsePolicy:
    return VlmParsePolicy(Decimal("0.80"), 1_000_000, 4, 128, 512)


def _retry_policy() -> GenerationRetryPolicy:
    return GenerationRetryPolicy(
        GENERATION_RETRY_STRATEGY_VERSION,
        3,
        (2, 8),
    )


def _source_bundle(
    prepared_episode: PreparedSourceEpisode | None = None,
    *,
    job: Job | None = None,
) -> PersistedPreparedSources:
    episode = prepared_episode or _prepared_episode()
    source_job = job or Job("run-001-window-001", "test")
    census = SeriesSourceCensus(
        "authorized-test-source",
        "series-001",
        "all_or_nothing",
        (SeriesSource("episode-001.mp4", "source-001", _hash("a"), 4_096),),
    )
    prepared = PreparedSeriesSources(census, (episode,))
    payload_json = json.dumps(
        prepared.to_mapping(), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    return PersistedPreparedSources(
        prepared=prepared,
        source_job=source_job,
        kernel_job_id=uuid4(),
        receipt_id=uuid4(),
        artifact_set_id=uuid4(),
        command_slot_id=uuid4(),
        artifact_reference=WholeSeriesSourceManifestReference(
            canonical_recipe_scope(source_job),
            "whole_series_source_manifest",
            1,
            canonical_payload_hash(payload_json),
        ),
    )


def _policy(**overrides: object) -> DoubaoVlmRequestPolicy:
    values: dict[str, object] = {
        "model_id": "doubao-seed-2-1-pro-260628",
        "parse_policy": _parse_policy(),
    }
    values.update(overrides)
    return DoubaoVlmRequestPolicy(**values)  # type: ignore[arg-type]


def test_policy_is_closed_immutable_and_canonically_binds_every_strategy_input() -> None:
    first = _policy()
    second = _policy()

    assert first.provider_id == DOUBAO_ARK_PROVIDER_ID
    assert first.adapter_strategy_version == DOUBAO_ARK_ADAPTER_STRATEGY_VERSION
    assert first.prompt_version == VLM_PROMPT_VERSION
    assert first.response_schema_json == vlm_response_schema_json()
    assert first.stage_strategy_version == DOUBAO_VLM_STAGE_STRATEGY_VERSION
    assert first.request_parameters_json == (
        '{"adapter_strategy_version":"doubao-ark-files-responses-stream-v1",'
        '"max_output_tokens":4096,"temperature":0.0,"video_fps":1.0}'
    )
    assert first.to_mapping() == second.to_mapping()
    assert first.canonical_hash == second.canonical_hash
    assert first.canonical_json == json.dumps(
        first.to_mapping(), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    with pytest.raises(FrozenInstanceError):
        first.model_id = "tampered"  # type: ignore[misc]
    with pytest.raises(TypeError):
        _policy(fallback_provider_id="qwen")


def test_factory_builds_one_exact_manifest_bound_kernel_request() -> None:
    prepared = _prepared_episode()
    job = Job("run-001-window-001", "test")
    source_bundle = _source_bundle(prepared, job=job)
    policy = _policy()

    request = build_doubao_vlm_request(
        source_bundle=source_bundle,
        episode_index=0,
        job=job,
        artifact_revision=3,
        idempotency_key="vlm-window-001",
        policy=policy,
        retry_policy=_retry_policy(),
    )
    via_factory = DoubaoVlmRequestFactory(policy, _retry_policy()).build(
        source_bundle=source_bundle,
        episode_index=0,
        job=job,
        artifact_revision=3,
        idempotency_key="vlm-window-001",
    )

    assert type(request) is GenerateVlmEvidenceRequest
    assert via_factory == request
    assert request.job == job
    assert request.artifact_scope == canonical_recipe_scope(job)
    assert request.artifact_revision == 3
    assert request.manifest is prepared.manifest
    assert request.manifest_set is prepared.manifest_set
    assert request.proxy_blob is prepared.proxy_blob
    assert request.prompt_version == VLM_PROMPT_VERSION
    assert request.response_schema_json == vlm_response_schema_json()
    assert request.request_parameters_json == policy.request_parameters_json
    assert request.model_id == policy.model_id
    assert request.provider_id == DOUBAO_ARK_PROVIDER_ID
    assert request.parse_policy is policy.parse_policy
    assert request.retry_policy == _retry_policy()
    assert request.parser_strategy_version == VLM_PARSER_STRATEGY_VERSION
    assert request.source_provenance_sha256 == source_bundle.canonical_hash
    assert json.loads(request.request_payload)["parser_strategy_version"] == (
        VLM_PARSER_STRATEGY_VERSION
    )
    assert "physical_cut" not in request.request_payload.decode()
    assert "edit_decision" not in request.request_payload.decode()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("provider_id", "qwen-vl", "Doubao provider"),
        ("provider_id", "other-provider", "Doubao provider"),
        ("model_id", "qwen-vl-max", "Qwen"),
        ("adapter_strategy_version", "qwen-http-v1", "adapter strategy"),
        ("adapter_strategy_version", "doubao-unregistered-v2", "adapter strategy"),
        ("prompt_version", "tampered-prompt-v2", "prompt version"),
        ("response_schema_json", '{"type":"object"}', "response schema"),
        ("stage_strategy_version", "automatic-provider-fallback-v1", "stage strategy"),
        ("parser_strategy_version", "strict-v2", "parser strategy"),
        ("video_fps", float("nan"), "video_fps"),
        ("video_fps", float("inf"), "video_fps"),
        ("max_output_tokens", True, "max_output_tokens"),
        ("temperature", float("nan"), "temperature"),
    ],
)
def test_policy_rejects_provider_model_schema_strategy_and_nonfinite_tampering(
    field: str,
    value: object,
    message: str,
) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        _policy(**{field: value})


@pytest.mark.parametrize(
    "schema_json",
    [
        json.dumps(json.loads(vlm_response_schema_json()), indent=2, sort_keys=True),
        vlm_response_schema_json().replace('{"$schema"', '{"type":"object","$schema"'),
        '{"type":NaN}',
    ],
)
def test_policy_rejects_noncanonical_ambiguous_or_nonfinite_schema_json(
    schema_json: str,
) -> None:
    with pytest.raises(ValueError, match="response schema"):
        _policy(response_schema_json=schema_json)


def test_parse_policy_and_parameters_change_policy_and_kernel_request_identity() -> None:
    prepared = _prepared_episode()
    job = Job("run-identity", "shadow")
    source_bundle = _source_bundle(prepared, job=job)
    first_policy = _policy()
    changed_parse = _policy(
        parse_policy=VlmParsePolicy(Decimal("0.90"), 1_000_000, 4, 128, 512)
    )
    changed_parameters = _policy(max_output_tokens=8_192)

    def build(policy: DoubaoVlmRequestPolicy) -> GenerateVlmEvidenceRequest:
        return build_doubao_vlm_request(
            source_bundle=source_bundle,
            episode_index=0,
            job=job,
            artifact_revision=1,
            idempotency_key="identity-window",
            policy=policy,
            retry_policy=_retry_policy(),
        )

    assert len(
        {
            build(first_policy).request_hash,
            build(changed_parse).request_hash,
            build(changed_parameters).request_hash,
        }
    ) == 3
    assert len(
        {
            first_policy.canonical_hash,
            changed_parse.canonical_hash,
            changed_parameters.canonical_hash,
        }
    ) == 3


def test_factory_fails_closed_on_cross_manifest_and_blob_tampering() -> None:
    prepared = _prepared_episode()
    other = _prepared_episode()
    job = Job("run-tamper", "test")
    factory = DoubaoVlmRequestFactory(_policy(), _retry_policy())

    with pytest.raises(ValueError, match="manifest_set"):
        factory.build(
            source_bundle=_source_bundle(
                replace(prepared, manifest_set=other.manifest_set), job=job
            ),
            episode_index=0,
            job=job,
            artifact_revision=1,
            idempotency_key="manifest-set-tamper",
        )
    with pytest.raises(ValueError, match="proxy_blob"):
        factory.build(
            source_bundle=_source_bundle(replace(prepared, proxy_blob=other.proxy_blob), job=job),
            episode_index=0,
            job=job,
            artifact_revision=1,
            idempotency_key="blob-tamper",
        )
    with pytest.raises(TypeError, match="exact PersistedPreparedSources"):
        factory.build(
            source_bundle=cast(PersistedPreparedSources, object()),
            episode_index=0,
            job=job,
            artifact_revision=1,
            idempotency_key="wrong-input-type",
        )


def test_raw_episode_without_persisted_provenance_has_no_public_factory_entry() -> None:
    with pytest.raises(TypeError, match="prepared_episode"):
        build_doubao_vlm_request(  # type: ignore[call-arg]
            prepared_episode=_prepared_episode(),
            episode_index=0,
            job=Job("untrusted-episode", "test"),
            artifact_revision=1,
            idempotency_key="untrusted-episode",
            policy=_policy(),
            retry_policy=_retry_policy(),
        )


def test_persisted_bundle_rejects_artifact_content_tampering() -> None:
    bundle = _source_bundle()

    with pytest.raises(ValueError, match="artifact content hash"):
        replace(
            bundle,
            artifact_reference=replace(bundle.artifact_reference, content_hash=_hash("8")),
        )


def test_equivalent_provider_numbers_have_one_canonical_policy_identity() -> None:
    integer_temperature = _policy(temperature=0, video_fps=1)
    float_temperature = _policy(temperature=0.0, video_fps=1.0)

    assert integer_temperature.temperature == float_temperature.temperature == 0.0
    assert integer_temperature.video_fps == float_temperature.video_fps == 1.0
    assert integer_temperature.to_mapping() == float_temperature.to_mapping()
    assert integer_temperature.canonical_hash == float_temperature.canonical_hash


def test_kernel_request_rejects_parser_drift_and_binds_source_provenance() -> None:
    prepared = _prepared_episode()
    job = Job("provenance-bound-request", "test")
    first_bundle = _source_bundle(prepared, job=job)
    second_bundle = _source_bundle(prepared, job=job)

    def build(bundle: PersistedPreparedSources) -> GenerateVlmEvidenceRequest:
        return build_doubao_vlm_request(
            source_bundle=bundle,
            episode_index=0,
            job=job,
            artifact_revision=1,
            idempotency_key="provenance-bound-request",
            policy=_policy(),
            retry_policy=_retry_policy(),
        )

    first = build(first_bundle)
    second = build(second_bundle)

    assert first.request_identity.canonical_hash == second.request_identity.canonical_hash
    assert first.request_hash != second.request_hash
    assert first.provider_idempotency_key != second.provider_idempotency_key
    with pytest.raises(ValueError, match="parser_strategy_version is not registered"):
        replace(first, parser_strategy_version="strict-v2")
