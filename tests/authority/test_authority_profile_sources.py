from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable
from pathlib import Path

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_hash, sha256_bytes
from autocut_kernel.registry import (
    AUTHORITY_PROFILE_SOURCE_INVALID,
    AuthorityProfileSourceError,
    LocalRunProfileSource,
    ShadowCalibrationProfileSource,
    Stage1NarrativeProfileSource,
    UnresolvedAuthorityProfileSourceSet,
    decode_authority_profile_source_grammar,
    decode_stage1_narrative_profile_source,
)
from autocut_kernel.registry.authority_profiles import (
    decode_local_run_profile_source as _decode_local_run_profile_source,
)
from autocut_kernel.registry.authority_profiles import (
    decode_shadow_calibration_profile_source as _decode_shadow_calibration_profile_source,
)
from autocut_kernel.registry.timed_speech_contract import timed_speech_registry_contract_sha256
from autocut_kernel.semantic_chain.candidate_catalog import CandidateCatalogPolicy
from autocut_kernel.semantic_chain.coverage_analysis import Stage1CoveragePolicy
from autocut_kernel.semantic_chain.dependency_projection import DependencyProjectionPolicy
from autocut_kernel.semantic_chain.stage1_command_policy import (
    Stage1CommandPolicy,
    Stage1GenerationPolicy,
)
from autocut_kernel.semantic_chain.stage1_draft import Stage1DraftPolicy
from autocut_kernel.semantic_chain.story_design_command_policy import Stage2CommandPolicy
from autocut_kernel.semantic_chain.story_design_draft import StoryDesignDraftPolicy
from autocut_kernel.semantic_chain.story_design_models import (
    EditingProfileReference,
    IntegerRange,
    JobPolicy,
    PhysicalRequirement,
    SourceConstraints,
    StoryDesignPolicy,
)
from autocut_kernel.vlm.retry_policy import GenerationRetryPolicy
from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).parents[2]


def _hash(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode()).hexdigest()


def _raw(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _profile_contract_hash(schema_name: str) -> str:
    return sha256_bytes((REPO_ROOT / "governance" / "schemas" / schema_name).read_bytes())


def decode_shadow_calibration_profile_source(
    raw: bytes,
    *,
    narrative: Stage1NarrativeProfileSource,
    expected_profile_contract_sha256: str | None = None,
) -> ShadowCalibrationProfileSource:
    return _decode_shadow_calibration_profile_source(
        raw,
        narrative=narrative,
        expected_profile_contract_sha256=(
            expected_profile_contract_sha256
            or _profile_contract_hash("shadow-calibration-profile.schema.json")
        ),
    )


def decode_local_run_profile_source(
    raw: bytes,
    *,
    narrative: Stage1NarrativeProfileSource,
    shadow: ShadowCalibrationProfileSource,
    expected_profile_contract_sha256: str | None = None,
) -> LocalRunProfileSource:
    return _decode_local_run_profile_source(
        raw,
        narrative=narrative,
        shadow=shadow,
        expected_profile_contract_sha256=(
            expected_profile_contract_sha256
            or _profile_contract_hash("local-run-profile.schema.json")
        ),
    )


def synthetic_stage1_command_policy() -> Stage1CommandPolicy:
    """Explicit fake prompt/limits, not a calibrated or deployable profile."""
    return Stage1CommandPolicy(
        artifact_revision=1,
        generation=Stage1GenerationPolicy(
            provider_id="doubao-ark-text-responses-stream",
            model_id="doubao-seed-2-1-pro-260628",
            prompt_version="synthetic-stage1-draft-v1",
            prompt_template="Synthetic test prompt: produce only the closed cross-window draft.",
            adapter_strategy_version="doubao-ark-text-responses-stream-v1",
            max_output_tokens=4096,
            temperature="0",
        ),
        draft_policy=Stage1DraftPolicy(
            max_response_bytes=1_048_576, max_prompt_bytes=2_097_152,
            max_input_windows=100, max_input_objects=10_000,
            max_beats=100, max_obligations=100, max_story_threads=20,
            max_merge_proposals=100, max_references_per_item=100,
            max_text_characters=4096, max_total_text_characters=100_000,
        ),
        coverage_policy=Stage1CoveragePolicy("0.8", "strict_global"),
        dependency_policy=DependencyProjectionPolicy("semantic-dependencies-v1"),
        retry_policy=GenerationRetryPolicy("generation-retry-v1", 3, (2, 8)),
    )


def _narrative_mapping() -> dict[str, object]:
    command_policy = synthetic_stage1_command_policy()
    return {
        "schema_version": "autocut-stage1-narrative-profile-v2",
        "contract_version": "2.1.3",
        "profile_id": "stage1_narrative",
        "profile_version": "1",
        "profile_state": "stage1_narrative_v1",
        "provider": {
            "provider_id": "doubao-ark-responses-stream",
            "adapter_strategy_version": "doubao-ark-files-responses-stream-v2",
            "transport": "ark_responses_streaming",
        },
        "model": {"model_id": "doubao-seed-2-1-pro-260628"},
        "prompt": {"version": "vlm-semantic-pack-v3", "template_sha256": _hash("prompt")},
        "response_schema": {"schema_sha256": _hash("response-schema")},
        "parser": {
            "strategy_version": "strict-semantic-pack-v3",
            "contract_sha256": _hash("parser"),
        },
        "policies": {
            "request_parameters_sha256": _hash("request-parameters"),
            "parse_policy_sha256": _hash("parse-policy"),
            "retry_policy_sha256": _hash("retry-policy"),
            "window_sampling_policy_sha256": _hash("window-policy"),
            "stage1_command_policy_sha256": command_policy.canonical_hash,
        },
        "stage1_command_policy": command_policy.to_mapping(),
        "capabilities": {
            "narrative_evidence_generation": True,
            "stage1_compile": True,
            "external_publication": False,
        },
    }


def synthetic_stage2_command_policy() -> Stage2CommandPolicy:
    """Explicit synthetic prompt and budgets, never a production default."""
    story = StoryDesignPolicy(
        "synthetic-story", "1", ("drama",), (EditingProfileReference("synthetic-edit", "1"),),
        ("reveal",), (PhysicalRequirement("visual_validity", "endpoint_and_stable_region"),),
        "first_feasible_lexicographic_v1",
    )
    return Stage2CommandPolicy(
        artifact_revision=1,
        generation=Stage1GenerationPolicy(
            "doubao-ark-text-responses-stream", "doubao-seed-2-1-pro-260628",
            "synthetic-stage2-draft-v1", "Synthetic test prompt: propose complete stories, not physical endpoints.",
            "doubao-ark-text-responses-stream-v1", 4096, "0",
        ),
        max_prompt_bytes=2_097_152,
        draft_policy=StoryDesignDraftPolicy(
            max_response_bytes=1_048_576, max_json_depth=32, max_proposals=16,
            max_material_requirements_per_proposal=32, max_total_material_requirements=128,
            max_references_per_field=128, max_total_references=2048, max_genre_tags=16,
            max_text_characters=4096, max_total_text_characters=100_000,
        ),
        candidate_policy=CandidateCatalogPolicy("candidate-catalog-v1", "0.8", ()),
        job_policy=JobPolicy(
            "synthetic-job", "1", story.canonical_hash, IntegerRange(1, 16), 1, 10_000,
            IntegerRange(1, 120), "forbid", SourceConstraints((), (), "render_source"), "all_or_nothing",
        ),
        story_policy=story,
        retry_policy=GenerationRetryPolicy("generation-retry-v1", 3, (2, 8)),
    )


def _narrative_reference(narrative: dict[str, object]) -> dict[str, object]:
    provider = narrative["provider"]
    model = narrative["model"]
    prompt = narrative["prompt"]
    response_schema = narrative["response_schema"]
    parser = narrative["parser"]
    policies = narrative["policies"]
    assert isinstance(provider, dict)
    assert isinstance(model, dict)
    assert isinstance(prompt, dict)
    assert isinstance(response_schema, dict)
    assert isinstance(parser, dict)
    assert isinstance(policies, dict)
    return {
        "profile_id": narrative["profile_id"],
        "profile_version": narrative["profile_version"],
        "source_sha256": sha256_bytes(_raw(narrative)),
        "provider_id": provider["provider_id"],
        "adapter_strategy_version": provider["adapter_strategy_version"],
        "model_id": model["model_id"],
        "prompt_version": prompt["version"],
        "prompt_template_sha256": prompt["template_sha256"],
        "response_schema_sha256": response_schema["schema_sha256"],
        "parser_strategy_version": parser["strategy_version"],
        "parser_contract_sha256": parser["contract_sha256"],
        **policies,
    }


def _producer(kind: str) -> dict[str, object]:
    is_asr = kind == "asr"
    return {
        "producer_kind": kind,
        "producer_id": f"native-{kind}",
        "producer_version": "measured-1",
        "generation_policy_sha256": _hash(f"{kind}-generation"),
        "detector_sha256": _hash(f"{kind}-detector"),
        "calibration_policy_sha256": _hash(f"{kind}-calibration"),
        "model_id": "SenseVoiceSmall" if is_asr else "fsmn-vad",
        "model_revision": f"measured-{kind}-revision",
        "model_sha256": _hash(f"{kind}-model-tree"),
        "inference_kind": "sensevoice-word-timestamp" if is_asr else "fsmn-vad-direct",
        "service_sha256": _hash("funasr-service"),
    }


def _shadow_mapping(narrative: dict[str, object]) -> dict[str, object]:
    members = [
        {
            "member_id": "current-drama-001",
            "corpus_member_reference_sha256": _hash("member-ref-1"),
            "source_id": "current-drama-source-001",
            "source_sha256": _hash("source-1"),
            "source_blob_reference_sha256": _hash("source-blob-ref-1"),
            "expected_anchor_reference_sha256": _hash("anchor-ref-1"),
        },
        {
            "member_id": "golden-001",
            "corpus_member_reference_sha256": _hash("member-ref-2"),
            "source_id": "golden-source-001",
            "source_sha256": _hash("source-2"),
            "source_blob_reference_sha256": _hash("source-blob-ref-2"),
            "expected_anchor_reference_sha256": _hash("anchor-ref-2"),
        },
    ]
    return {
        "schema_version": "autocut-shadow-calibration-profile-v2",
        "contract_version": "2.1.3",
        "profile_id": "shadow_calibration",
        "profile_version": "1",
        "profile_state": "shadow_calibration_v1",
        "profile_contract_sha256": _profile_contract_hash(
            "shadow-calibration-profile.schema.json"
        ),
        "stage1_narrative_profile": _narrative_reference(narrative),
        "native_timed_speech": {
            "provider_id": "funasr-http-v1",
            "provider_version": "1.0.0",
            "service_sha256": _hash("funasr-service"),
            "funasr_version": "measured-funasr-version",
            "torch_version": "measured-torch-version",
            "device": "cpu",
            "word_timing_capability": "required",
            "max_request_bytes": 1_048_576,
            "native_port_identity_sha256": _hash("native-port"),
            "producers": [_producer("asr"), _producer("vad")],
        },
        "source_clock_policy": {
            "policy_sha256": _hash("source-clock-policy"),
            "clock_id": "source-audio",
            "time_base": {"numerator": 1, "denominator": 1_000},
            "origin_rule": "declared_source_audio_origin_tick",
            "range_rule": "complete_source_audio_range_only",
            "millisecond_conversion_rule": "floor_start_ceil_end_integer_v1",
        },
        "calibration_corpus": {
            "corpus_set_sha256": canonical_json_hash(members),
            "members": members,
        },
        "timing_policies": {
            "timed_speech_policy_sha256": _hash("timed-speech-policy"),
            "word_gap_policy_sha256": _hash("word-gap-policy"),
            "vad_merge_policy_sha256": _hash("vad-merge-policy"),
            "alignment_policy_sha256": _hash("alignment-policy"),
            "acceptance_policy_sha256": _hash("acceptance-policy"),
            "word_gap_ms": 180,
            "vad_merge_gap_ms": 120,
        },
        "capabilities": {
            "shadow_measurement": True,
            "authority_registry_compile": False,
            "authority_bootstrap": False,
            "http_media_preflight": False,
            "local_pipeline_run": False,
            "local_render_qc": False,
            "semantic_highlight_read": False,
            "external_publication": False,
            "runtime_profile_selection": False,
        },
        "calibration_acceptance": {
            "aggregation_strategy": "member-bound-calibration-statistics-v1",
            "alignment_strategy": "complete-ordered-one-to-one-v1",
            "require_complete_member_set": True,
            "require_zero_invalid_members": True,
            "require_positive_asr_bound": True,
            "require_positive_vad_bound": True,
            "max_successor_attempts": 1,
        },
    }


def _member_ref(
    *, artifact_type: str, ordinal: int, content_hash: str
) -> dict[str, object]:
    return {
        "artifact_set_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "artifact_type": artifact_type,
        "content_hash": content_hash,
        "logical_id": artifact_type.replace("_", "-"),
        "member_ordinal": ordinal,
        "receipt_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        "revision": 1,
        "scope": {
            "namespace": "autocut_authority",
            "kind": "calibration",
            "key": "shadow_calibration@1",
        },
    }


def _run_mapping(narrative: dict[str, object], shadow: dict[str, object]) -> dict[str, object]:
    stage2_policy = synthetic_stage2_command_policy()
    native = copy.deepcopy(shadow["native_timed_speech"])
    assert isinstance(native, dict)
    producers = native["producers"]
    assert isinstance(producers, list)
    asr_child = _hash("asr-child-record")
    vad_child = _hash("vad-child-record")
    for producer, child, bound in zip(producers, (asr_child, vad_child), (7, 11), strict=True):
        assert isinstance(producer, dict)
        producer["producer_record_sha256"] = child
        producer["timing_error_bound_tick"] = bound
    clock = copy.deepcopy(shadow["source_clock_policy"])
    timing = copy.deepcopy(shadow["timing_policies"])
    assert isinstance(clock, dict)
    assert isinstance(timing, dict)
    aggregate = _hash("aggregate-calibration-record")
    calibration = {
        "record_ref": _member_ref(
            artifact_type="calibration_record", ordinal=0, content_hash=aggregate
        ),
        "validation_receipt_ref": _member_ref(
            artifact_type="calibration_validation_receipt",
            ordinal=1,
            content_hash=_hash("validation-receipt"),
        ),
        "asr_producer_record_sha256": asr_child,
        "vad_producer_record_sha256": vad_child,
        "asr_timing_error_bound_tick": 7,
        "vad_timing_error_bound_tick": 11,
    }

    def requirement(index: int, child: str) -> dict[str, object]:
        producer = producers[index]
        assert isinstance(producer, dict)
        return {
            "adapter_sha256": native["native_port_identity_sha256"],
            "calibration_record_sha256": child,
            "clock_id": clock["clock_id"],
            "generation_policy_sha256": producer["generation_policy_sha256"],
            "model_sha256": producer["detector_sha256"],
            "producer_id": producer["producer_id"],
            "producer_kind": producer["producer_kind"],
            "inference_kind": producer["inference_kind"],
            "time_base": copy.deepcopy(clock["time_base"]),
        }

    return {
        "schema_version": "autocut-local-run-profile-v3",
        "contract_version": "2.1.3",
        "profile_id": "local_run",
        "profile_version": "1",
        "profile_state": "local_run_v1",
        "stage2_command_policy": stage2_policy.to_mapping(),
        "stage2_command_policy_sha256": stage2_policy.canonical_hash,
        "profile_contract_sha256": _profile_contract_hash("local-run-profile.schema.json"),
        "predecessor_shadow_profile": {
            "profile_id": "shadow_calibration",
            "profile_version": shadow["profile_version"],
            "source_sha256": sha256_bytes(_raw(shadow)),
            "registry_set_sha256": _hash("shadow-registry-set"),
            "authority_lock_sha256": _hash("shadow-authority-lock"),
        },
        "stage1_narrative_profile": _narrative_reference(narrative),
        "native_timed_speech": native,
        "source_clock_policy": clock,
        "timing_policies": timing,
        "capabilities": {
            "shadow_measurement": False,
            "authority_registry_compile": True,
            "authority_bootstrap": True,
            "http_media_preflight": True,
            "local_pipeline_run": True,
            "local_render_qc": True,
            "semantic_highlight_read": True,
            "external_publication": False,
            "runtime_profile_selection": False,
        },
        "calibration": calibration,
        "timed_speech_registry_entry": {
            "capability": "known_speech_only",
            "guard_policy": {
                "policy_sha256": timing["timed_speech_policy_sha256"],
                "post_roll_tick": 0,
                "pre_roll_tick": 0,
                "source_audio_clock_id": clock["clock_id"],
                "source_audio_time_base": copy.deepcopy(clock["time_base"]),
                "vad_merge_gap_tick": timing["vad_merge_gap_ms"],
                "word_gap_tick": timing["word_gap_ms"],
            },
            "kind": "sensevoice_word_guard_v1",
            "profile_id": "local_run",
            "profile_version": "1",
            "registry_contract_sha256": timed_speech_registry_contract_sha256(
                (REPO_ROOT / "governance/schemas/local-run-profile.schema.json").read_bytes()
            ),
            "transcript_requirement": requirement(0, asr_child),
            "vad_requirement": requirement(1, vad_child),
        },
    }


def _profiles() -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    narrative = _narrative_mapping()
    shadow = _shadow_mapping(narrative)
    return narrative, shadow, _run_mapping(narrative, shadow)


def _decoded_dependencies(
    narrative: dict[str, object], shadow: dict[str, object]
) -> tuple[Stage1NarrativeProfileSource, ShadowCalibrationProfileSource]:
    narrative_source = decode_stage1_narrative_profile_source(_raw(narrative))
    shadow_source = decode_shadow_calibration_profile_source(
        _raw(shadow), narrative=narrative_source
    )
    return narrative_source, shadow_source


def _assert_invalid(call: Callable[[], object], *, absent: str | None = None) -> None:
    with pytest.raises(AuthorityProfileSourceError) as raised:
        call()
    message = str(raised.value)
    assert AUTHORITY_PROFILE_SOURCE_INVALID in message
    if absent is not None:
        assert absent not in message


def test_decodes_all_three_unresolved_sources_and_existing_registry_projection() -> None:
    narrative, shadow, run = _profiles()
    decoded = decode_authority_profile_source_grammar(
        narrative_raw=_raw(narrative),
        shadow_raw=_raw(shadow),
        expected_shadow_profile_contract_sha256=_profile_contract_hash(
            "shadow-calibration-profile.schema.json"
        ),
        local_run_raw=_raw(run),
        expected_local_run_profile_contract_sha256=_profile_contract_hash(
            "local-run-profile.schema.json"
        ),
    )
    assert isinstance(decoded, UnresolvedAuthorityProfileSourceSet)
    assert decoded.resolution_state == "grammar_only_unresolved"
    assert isinstance(decoded.narrative, Stage1NarrativeProfileSource)
    assert isinstance(decoded.shadow, ShadowCalibrationProfileSource)
    assert isinstance(decoded.local_run, LocalRunProfileSource)
    assert decoded.narrative.source_sha256 == sha256_bytes(_raw(narrative))
    assert decoded.shadow.source_sha256 == sha256_bytes(_raw(shadow))
    assert decoded.local_run.source_sha256 == sha256_bytes(_raw(run))
    assert decoded.local_run.timed_speech_registry_entry.profile_id == "local_run"
    assert decoded.local_run.to_mapping() == run


def test_happy_sources_match_the_three_governance_schema_mirrors() -> None:
    root = Path(__file__).parents[2]
    narrative, shadow, run = _profiles()
    for source, schema_name in (
        (narrative, "stage1-narrative-profile.schema.json"),
        (shadow, "shadow-calibration-profile.schema.json"),
        (run, "local-run-profile.schema.json"),
    ):
        schema = json.loads((root / "governance" / "schemas" / schema_name).read_text())
        Draft202012Validator(schema).validate(source)


def test_raw_blob_identity_changes_while_canonical_semantics_stay_equal() -> None:
    """Equivalent bytes still require a new A/B/C identity; raw Git blobs are authority."""
    narrative = _narrative_mapping()
    compact = _raw(narrative)
    pretty_reordered = json.dumps(
        narrative,
        ensure_ascii=False,
        sort_keys=False,
        indent=2,
    ).encode()
    compact_source = decode_stage1_narrative_profile_source(compact)
    pretty_source = decode_stage1_narrative_profile_source(pretty_reordered)
    assert compact_source.source_sha256 != pretty_source.source_sha256
    assert compact_source.canonical_sha256 == pretty_source.canonical_sha256

    shadow = _shadow_mapping(narrative)
    _assert_invalid(
        lambda: decode_shadow_calibration_profile_source(
            _raw(shadow), narrative=pretty_source
        )
    )


@pytest.mark.parametrize("profile_kind", ["shadow", "local_run"])
def test_profile_contract_claim_must_match_independent_locked_schema_hash(
    profile_kind: str,
) -> None:
    narrative, shadow, run = _profiles()
    substituted = _hash("substituted-profile-contract")
    if profile_kind == "shadow":
        shadow["profile_contract_sha256"] = substituted
        narrative_source = decode_stage1_narrative_profile_source(_raw(narrative))
        _assert_invalid(
            lambda: _decode_shadow_calibration_profile_source(
                _raw(shadow),
                narrative=narrative_source,
                expected_profile_contract_sha256=_profile_contract_hash(
                    "shadow-calibration-profile.schema.json"
                ),
            ),
            absent=substituted,
        )
        return

    narrative_source, shadow_source = _decoded_dependencies(narrative, shadow)
    run["profile_contract_sha256"] = substituted
    _assert_invalid(
        lambda: _decode_local_run_profile_source(
            _raw(run),
            narrative=narrative_source,
            shadow=shadow_source,
            expected_profile_contract_sha256=_profile_contract_hash(
                "local-run-profile.schema.json"
            ),
        ),
        absent=substituted,
    )


def test_source_set_requires_local_bytes_and_expected_contract_together() -> None:
    narrative, shadow, run = _profiles()
    common = {
        "narrative_raw": _raw(narrative),
        "shadow_raw": _raw(shadow),
        "expected_shadow_profile_contract_sha256": _profile_contract_hash(
            "shadow-calibration-profile.schema.json"
        ),
    }
    _assert_invalid(
        lambda: decode_authority_profile_source_grammar(
            **common,
            local_run_raw=_raw(run),
        )
    )
    _assert_invalid(
        lambda: decode_authority_profile_source_grammar(
            **common,
            expected_local_run_profile_contract_sha256=_profile_contract_hash(
                "local-run-profile.schema.json"
            ),
        )
    )


def test_shadow_only_grammar_decode_does_not_create_a_run_profile() -> None:
    narrative, shadow, _run = _profiles()
    decoded = decode_authority_profile_source_grammar(
        narrative_raw=_raw(narrative),
        shadow_raw=_raw(shadow),
        expected_shadow_profile_contract_sha256=_profile_contract_hash(
            "shadow-calibration-profile.schema.json"
        ),
    )
    assert decoded.resolution_state == "grammar_only_unresolved"
    assert decoded.local_run is None


def test_duplicate_key_at_nested_depth_is_sanitized() -> None:
    narrative = _raw(_narrative_mapping())
    duplicate_value = "doubao-seed-2-1-pro-260628"
    altered = narrative.replace(
        b'"model_id":"doubao-seed-2-1-pro-260628"',
        b'"model_id":"doubao-seed-2-1-pro-260628","model_id":"duplicate-secret"',
    )
    _assert_invalid(
        lambda: decode_stage1_narrative_profile_source(altered), absent=duplicate_value
    )


@pytest.mark.parametrize("bad_value", [True, 1.5, 2**53])
def test_numeric_subset_rejects_bool_float_and_unsafe_integer(bad_value: object) -> None:
    narrative, shadow, _run = _profiles()
    native = shadow["native_timed_speech"]
    assert isinstance(native, dict)
    native["max_request_bytes"] = bad_value
    narrative_source = decode_stage1_narrative_profile_source(_raw(narrative))
    _assert_invalid(
        lambda: decode_shadow_calibration_profile_source(
            _raw(shadow), narrative=narrative_source
        )
    )


@pytest.mark.parametrize("mutation", ["extra", "missing", "unknown_schema", "unknown_state"])
def test_narrative_source_is_closed_and_has_no_version_fallback(mutation: str) -> None:
    narrative = _narrative_mapping()
    if mutation == "extra":
        narrative["endpoint"] = "https://example.invalid"
    elif mutation == "missing":
        narrative.pop("parser")
    elif mutation == "unknown_schema":
        narrative["schema_version"] = "autocut-stage1-narrative-profile-v3"
    else:
        narrative["profile_state"] = "runtime_selected"
    _assert_invalid(lambda: decode_stage1_narrative_profile_source(_raw(narrative)))


@pytest.mark.parametrize("bad_hash", ["sha256:" + "0" * 64, "sha256:" + "A" * 64])
def test_hashes_are_lowercase_nonzero_and_values_are_not_echoed(bad_hash: str) -> None:
    narrative = _narrative_mapping()
    prompt = narrative["prompt"]
    assert isinstance(prompt, dict)
    prompt["template_sha256"] = bad_hash
    _assert_invalid(
        lambda: decode_stage1_narrative_profile_source(_raw(narrative)), absent=bad_hash
    )


def test_sensitive_material_is_rejected_without_echoing_it() -> None:
    narrative = _narrative_mapping()
    secret = "sk-" + "x" * 32
    model = narrative["model"]
    assert isinstance(model, dict)
    model["model_id"] = secret
    _assert_invalid(
        lambda: decode_stage1_narrative_profile_source(_raw(narrative)), absent=secret
    )


@pytest.mark.parametrize(
    "forbidden_value",
    [
        "https://authority.invalid/model",
        "/Users/example/models/SenseVoiceSmall",
        "env:FUNASR_PROFILE",
        "postgresql://user:password@database.invalid/authority",
    ],
)
def test_locator_path_runtime_selector_and_dsn_material_are_forbidden(
    forbidden_value: str,
) -> None:
    narrative, shadow, _run = _profiles()
    native = shadow["native_timed_speech"]
    assert isinstance(native, dict)
    producers = native["producers"]
    assert isinstance(producers, list) and isinstance(producers[0], dict)
    producers[0]["model_revision"] = forbidden_value
    narrative_source = decode_stage1_narrative_profile_source(_raw(narrative))
    _assert_invalid(
        lambda: decode_shadow_calibration_profile_source(
            _raw(shadow), narrative=narrative_source
        ),
        absent=forbidden_value,
    )


@pytest.mark.parametrize(
    ("target", "replacement"),
    [
        (("model", "model_id"), "qwen-max"),
        (("provider", "provider_id"), "another-provider"),
        (("provider", "adapter_strategy_version"), "another-adapter"),
        (("provider", "adapter_strategy_version"), "doubao-ark-files-responses-stream-v1"),
    ],
)
def test_doubao_identity_matrix_is_fixed(
    target: tuple[str, str], replacement: str
) -> None:
    narrative = _narrative_mapping()
    parent = narrative[target[0]]
    assert isinstance(parent, dict)
    parent[target[1]] = replacement
    _assert_invalid(lambda: decode_stage1_narrative_profile_source(_raw(narrative)))


def test_shadow_rejects_capability_escalation_and_record_self_bootstrap() -> None:
    narrative, shadow, _run = _profiles()
    narrative_source = decode_stage1_narrative_profile_source(_raw(narrative))
    capabilities = shadow["capabilities"]
    assert isinstance(capabilities, dict)
    capabilities["http_media_preflight"] = True
    _assert_invalid(
        lambda: decode_shadow_calibration_profile_source(
            _raw(shadow), narrative=narrative_source
        )
    )
    capabilities["http_media_preflight"] = False
    native = shadow["native_timed_speech"]
    assert isinstance(native, dict)
    producers = native["producers"]
    assert isinstance(producers, list) and isinstance(producers[0], dict)
    producers[0]["producer_record_sha256"] = _hash("forbidden-record")
    _assert_invalid(
        lambda: decode_shadow_calibration_profile_source(
            _raw(shadow), narrative=narrative_source
        )
    )


@pytest.mark.parametrize("mutation", ["reversed", "same_id", "same_detector", "same_model"])
def test_shadow_requires_ordered_distinct_sensevoice_and_fsmn(mutation: str) -> None:
    narrative, shadow, _run = _profiles()
    native = shadow["native_timed_speech"]
    assert isinstance(native, dict)
    producers = native["producers"]
    assert isinstance(producers, list)
    if mutation == "reversed":
        producers.reverse()
    else:
        assert isinstance(producers[0], dict) and isinstance(producers[1], dict)
        key = {"same_id": "producer_id", "same_detector": "detector_sha256", "same_model": "model_sha256"}[mutation]
        producers[1][key] = producers[0][key]
    narrative_source = decode_stage1_narrative_profile_source(_raw(narrative))
    _assert_invalid(
        lambda: decode_shadow_calibration_profile_source(
            _raw(shadow), narrative=narrative_source
        )
    )


def test_shadow_recomputes_ordered_unique_corpus_set() -> None:
    narrative, shadow, _run = _profiles()
    corpus = shadow["calibration_corpus"]
    assert isinstance(corpus, dict)
    members = corpus["members"]
    assert isinstance(members, list)
    members.reverse()
    corpus["corpus_set_sha256"] = canonical_json_hash(members)
    narrative_source = decode_stage1_narrative_profile_source(_raw(narrative))
    _assert_invalid(
        lambda: decode_shadow_calibration_profile_source(
            _raw(shadow), narrative=narrative_source
        )
    )


def test_shadow_rejects_narrative_reference_substitution() -> None:
    narrative, shadow, _run = _profiles()
    narrative_source = decode_stage1_narrative_profile_source(_raw(narrative))
    reference = shadow["stage1_narrative_profile"]
    assert isinstance(reference, dict)
    reference["stage1_command_policy_sha256"] = _hash("substituted-command-policy")
    _assert_invalid(
        lambda: decode_shadow_calibration_profile_source(
            _raw(shadow), narrative=narrative_source
        )
    )


def test_local_run_rejects_shadow_identity_clock_and_policy_drift() -> None:
    narrative, shadow, run = _profiles()
    narrative_source, shadow_source = _decoded_dependencies(narrative, shadow)
    timing = run["timing_policies"]
    assert isinstance(timing, dict)
    timing["word_gap_ms"] = 181
    _assert_invalid(
        lambda: decode_local_run_profile_source(
            _raw(run), narrative=narrative_source, shadow=shadow_source
        )
    )


def test_local_run_requires_complete_same_validation_set_refs() -> None:
    narrative, shadow, run = _profiles()
    narrative_source, shadow_source = _decoded_dependencies(narrative, shadow)
    calibration = run["calibration"]
    assert isinstance(calibration, dict)
    receipt = calibration["validation_receipt_ref"]
    assert isinstance(receipt, dict)
    receipt["artifact_set_id"] = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    _assert_invalid(
        lambda: decode_local_run_profile_source(
            _raw(run), narrative=narrative_source, shadow=shadow_source
        )
    )


@pytest.mark.parametrize("mutation", ["same_child", "zero_bound", "projection_drift"])
def test_local_run_closes_child_records_bounds_and_registry_projection(mutation: str) -> None:
    narrative, shadow, run = _profiles()
    narrative_source, shadow_source = _decoded_dependencies(narrative, shadow)
    calibration = run["calibration"]
    native = run["native_timed_speech"]
    registry = run["timed_speech_registry_entry"]
    assert isinstance(calibration, dict)
    assert isinstance(native, dict)
    assert isinstance(registry, dict)
    producers = native["producers"]
    assert isinstance(producers, list) and isinstance(producers[0], dict)
    if mutation == "same_child":
        calibration["vad_producer_record_sha256"] = calibration["asr_producer_record_sha256"]
    elif mutation == "zero_bound":
        calibration["asr_timing_error_bound_tick"] = 0
        producers[0]["timing_error_bound_tick"] = 0
    else:
        requirement = registry["transcript_requirement"]
        assert isinstance(requirement, dict)
        requirement["adapter_sha256"] = _hash("substituted-adapter")
    _assert_invalid(
        lambda: decode_local_run_profile_source(
            _raw(run), narrative=narrative_source, shadow=shadow_source
        )
    )


def test_aggregate_record_hash_cannot_fill_both_producer_requirements() -> None:
    narrative, shadow, run = _profiles()
    narrative_source, shadow_source = _decoded_dependencies(narrative, shadow)
    calibration = run["calibration"]
    registry = run["timed_speech_registry_entry"]
    assert isinstance(calibration, dict) and isinstance(registry, dict)
    record_ref = calibration["record_ref"]
    transcript = registry["transcript_requirement"]
    vad = registry["vad_requirement"]
    assert isinstance(record_ref, dict) and isinstance(transcript, dict) and isinstance(vad, dict)
    aggregate_hash = record_ref["content_hash"]
    transcript["calibration_record_sha256"] = aggregate_hash
    vad["calibration_record_sha256"] = aggregate_hash
    _assert_invalid(
        lambda: decode_local_run_profile_source(
            _raw(run), narrative=narrative_source, shadow=shadow_source
        )
    )


def test_local_run_cannot_enable_publication_or_runtime_selection() -> None:
    narrative, shadow, run = _profiles()
    narrative_source, shadow_source = _decoded_dependencies(narrative, shadow)
    capabilities = run["capabilities"]
    assert isinstance(capabilities, dict)
    capabilities["external_publication"] = True
    _assert_invalid(
        lambda: decode_local_run_profile_source(
            _raw(run), narrative=narrative_source, shadow=shadow_source
        )
    )
