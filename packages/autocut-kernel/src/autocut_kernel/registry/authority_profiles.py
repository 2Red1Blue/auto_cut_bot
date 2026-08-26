"""Closed, grammar-only decoder for locked authority profile source documents.

The decoder accepts bytes supplied by the authority-lock verifier.  It never
reads a checkout, environment variable, credential, or runtime selector.
Its result is explicitly unresolved: Phase 2 must resolve predecessor lock,
RegistrySet, CalibrationRecord, and independent validation-receipt references
before any profile can become runtime or publication authority.

Both source identities are retained deliberately. ``source_sha256`` hashes the
exact Git blob bytes, while ``canonical_sha256`` hashes semantic JSON. Whitespace
or key-order changes therefore require a new A/B/C source identity even when the
canonical semantic hash is unchanged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast
from uuid import UUID

from ..contracts.compiler.canonical import (
    canonical_json_hash,
    load_canonical_json_bytes,
    sha256_bytes,
)
from ..contracts.compiler.errors import CanonicalizationError, ContractCompilerError
from ..media.stage4_predecessor import (
    TimedSpeechProfileRegistryEntry,
    decode_timed_speech_profile_registry_entry,
)
from ..media.types import TimeBase
from ..store.models import ArtifactScope, CommittedArtifactMemberReference

if TYPE_CHECKING:
    from ..semantic_chain.editorial_command_policy import Stage3CommandPolicy
    from ..semantic_chain.stage1_command_policy import Stage1CommandPolicy
    from ..semantic_chain.story_design_command_policy import Stage2CommandPolicy

AUTHORITY_PROFILE_SOURCE_INVALID = "AUTHORITY_PROFILE_SOURCE_INVALID"
CONTRACT_VERSION = "2.1.3"
STAGE1_NARRATIVE_SCHEMA_VERSION = "autocut-stage1-narrative-profile-v2"
SHADOW_CALIBRATION_SCHEMA_VERSION = "autocut-shadow-calibration-profile-v2"
LOCAL_RUN_SCHEMA_VERSION = "autocut-local-run-profile-v4"

_ZERO_HASH = "sha256:" + "0" * 64
_PROFILE_VERSION = re.compile(r"^[1-9][0-9]*$")
_CANONICAL_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_SENSITIVE_CONTENT = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(rb"\bgh[pousr]_[A-Za-z0-9_]{24,}\b"),
    re.compile(rb"\bsk-[A-Za-z0-9_-]{24,}\b"),
    re.compile(
        rb"(?i)\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|password)\b"
        rb"\s*[:=]\s*([\"'])[A-Za-z0-9_./+\-=]{20,}\1"
    ),
    re.compile(rb"(?i)authorization\s*:\s*bearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(rb"(?i)(?:^|\n)cookie\s*:\s*[^\n]{16,}"),
    re.compile(
        rb"(?i)\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|password|"
        rb"authorization|bearer|cookie|database[_-]?url|dsn)\b"
    ),
    re.compile(rb"(?i)(?:https?|file|postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://"),
)


class AuthorityProfileSourceError(ContractCompilerError):
    """Terminal rejection of one authority profile source set."""

    code = AUTHORITY_PROFILE_SOURCE_INVALID


def _invalid(detail: str) -> AuthorityProfileSourceError:
    return AuthorityProfileSourceError(f"{AUTHORITY_PROFILE_SOURCE_INVALID}: {detail}")


def _mapping(value: object, fields: frozenset[str], field_name: str) -> dict[str, object]:
    if type(value) is not dict or frozenset(cast(dict[str, object], value)) != fields:  # noqa: E721
        raise _invalid(f"{field_name} does not match its closed schema")
    return cast(dict[str, object], value)


def _array(value: object, field_name: str, *, non_empty: bool = False) -> list[object]:
    if type(value) is not list or (non_empty and not value):  # noqa: E721
        raise _invalid(f"{field_name} must be a closed array")
    return cast(list[object], value)


def _text(value: object, field_name: str, *, canonical_id: bool = False) -> str:
    if type(value) is not str or not value or value != value.strip():  # noqa: E721
        raise _invalid(f"{field_name} must be canonical non-empty text")
    if canonical_id and _CANONICAL_ID.fullmatch(value) is None:
        raise _invalid(f"{field_name} must be a canonical identifier")
    return value


def _integer(
    value: object, field_name: str, *, minimum: int | None = None, maximum: int | None = None
) -> int:
    if type(value) is not int:  # noqa: E721 - booleans are deliberately rejected.
        raise _invalid(f"{field_name} must be an integer")
    if minimum is not None and value < minimum:
        raise _invalid(f"{field_name} is below its closed minimum")
    if maximum is not None and value > maximum:
        raise _invalid(f"{field_name} is above its closed maximum")
    return value


def _boolean(value: object, field_name: str) -> bool:
    if type(value) is not bool:  # noqa: E721
        raise _invalid(f"{field_name} must be a boolean")
    return value


def _sha(value: object, field_name: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:  # noqa: E721
        raise _invalid(f"{field_name} must be a lowercase SHA-256 identity")
    if value == _ZERO_HASH:
        raise _invalid(f"{field_name} must be non-zero")
    return value


def _profile_version(value: object, field_name: str) -> str:
    version = _text(value, field_name)
    if _PROFILE_VERSION.fullmatch(version) is None:
        raise _invalid(f"{field_name} must be a canonical positive decimal string")
    return version


def _const(value: object, expected: object, field_name: str) -> None:
    if type(value) is not type(expected) or value != expected:
        raise _invalid(f"{field_name} is not the locked value")


def _source_object(raw: bytes, label: str) -> tuple[dict[str, object], str, str]:
    if type(raw) is not bytes:  # noqa: E721
        raise _invalid(f"{label} bytes are invalid")
    if any(pattern.search(raw) for pattern in _SENSITIVE_CONTENT):
        raise _invalid(f"{label} contains forbidden sensitive material")
    try:
        value, _canonical = load_canonical_json_bytes(raw, origin="authority profile source")
    except (CanonicalizationError, ValueError) as error:
        raise _invalid(f"{label} is not strict canonical-subset JSON") from error
    if type(value) is not dict:  # noqa: E721
        raise _invalid(f"{label} root must be an object")
    mapping = cast(dict[str, object], value)
    _validate_all_hash_fields(mapping, label)
    return mapping, sha256_bytes(raw), canonical_json_hash(mapping)


def _validate_all_hash_fields(value: object, field_name: str) -> None:
    if type(value) is dict:  # noqa: E721
        for key, child in cast(dict[str, object], value).items():
            child_name = f"{field_name}.{key}"
            if key.endswith("_sha256") or key == "content_hash":
                _sha(child, child_name)
            _validate_all_hash_fields(child, child_name)
    elif type(value) is list:  # noqa: E721
        for index, child in enumerate(cast(list[object], value)):
            _validate_all_hash_fields(child, f"{field_name}[{index}]")
    elif type(value) is str and (  # noqa: E721
        value.startswith(("/", "~/", "env:"))
        or "://" in value
        or "\\" in value
        or "${" in value
    ):
        raise _invalid(f"{field_name} contains a forbidden locator or runtime selector")


@dataclass(frozen=True, slots=True)
class Stage1NarrativeProfileReference:
    profile_version: str
    source_sha256: str
    prompt_template_sha256: str
    response_schema_sha256: str
    parser_contract_sha256: str
    request_parameters_sha256: str
    parse_policy_sha256: str
    retry_policy_sha256: str
    window_sampling_policy_sha256: str
    stage1_command_policy_sha256: str

    def to_mapping(self) -> dict[str, object]:
        return {
            "profile_id": "stage1_narrative",
            "profile_version": self.profile_version,
            "source_sha256": self.source_sha256,
            "provider_id": "doubao-ark-responses-stream",
            "adapter_strategy_version": "doubao-ark-files-responses-stream-v2",
            "model_id": "doubao-seed-2-1-pro-260628",
            "prompt_version": "vlm-semantic-pack-v3",
            "prompt_template_sha256": self.prompt_template_sha256,
            "response_schema_sha256": self.response_schema_sha256,
            "parser_strategy_version": "strict-semantic-pack-v3",
            "parser_contract_sha256": self.parser_contract_sha256,
            "request_parameters_sha256": self.request_parameters_sha256,
            "parse_policy_sha256": self.parse_policy_sha256,
            "retry_policy_sha256": self.retry_policy_sha256,
            "window_sampling_policy_sha256": self.window_sampling_policy_sha256,
            "stage1_command_policy_sha256": self.stage1_command_policy_sha256,
        }


@dataclass(frozen=True, slots=True)
class Stage1NarrativeProfileSource:
    profile_version: str
    source_sha256: str
    canonical_sha256: str
    reference: Stage1NarrativeProfileReference
    command_policy: Stage1CommandPolicy

    def to_mapping(self) -> dict[str, object]:
        ref = self.reference
        return {
            "schema_version": STAGE1_NARRATIVE_SCHEMA_VERSION,
            "contract_version": CONTRACT_VERSION,
            "profile_id": "stage1_narrative",
            "profile_version": self.profile_version,
            "profile_state": "stage1_narrative_v1",
            "provider": {
                "provider_id": "doubao-ark-responses-stream",
                "adapter_strategy_version": "doubao-ark-files-responses-stream-v2",
                "transport": "ark_responses_streaming",
            },
            "model": {"model_id": "doubao-seed-2-1-pro-260628"},
            "prompt": {
                "version": "vlm-semantic-pack-v3",
                "template_sha256": ref.prompt_template_sha256,
            },
            "response_schema": {"schema_sha256": ref.response_schema_sha256},
            "parser": {
                "strategy_version": "strict-semantic-pack-v3",
                "contract_sha256": ref.parser_contract_sha256,
            },
            "policies": {
                "request_parameters_sha256": ref.request_parameters_sha256,
                "parse_policy_sha256": ref.parse_policy_sha256,
                "retry_policy_sha256": ref.retry_policy_sha256,
                "window_sampling_policy_sha256": ref.window_sampling_policy_sha256,
                "stage1_command_policy_sha256": ref.stage1_command_policy_sha256,
            },
            "stage1_command_policy": self.command_policy.to_mapping(),
            "capabilities": {
                "narrative_evidence_generation": True,
                "stage1_compile": True,
                "external_publication": False,
            },
        }


@dataclass(frozen=True, slots=True)
class NativeTimedSpeechProducer:
    producer_kind: str
    producer_id: str
    producer_version: str
    generation_policy_sha256: str
    detector_sha256: str
    calibration_policy_sha256: str
    model_id: str
    model_revision: str
    model_sha256: str
    inference_kind: str
    service_sha256: str
    producer_record_sha256: str | None = None
    timing_error_bound_tick: int | None = None

    def common_mapping(self) -> dict[str, object]:
        return {
            "producer_kind": self.producer_kind,
            "producer_id": self.producer_id,
            "producer_version": self.producer_version,
            "generation_policy_sha256": self.generation_policy_sha256,
            "detector_sha256": self.detector_sha256,
            "calibration_policy_sha256": self.calibration_policy_sha256,
            "model_id": self.model_id,
            "model_revision": self.model_revision,
            "model_sha256": self.model_sha256,
            "inference_kind": self.inference_kind,
            "service_sha256": self.service_sha256,
        }

    def to_mapping(self) -> dict[str, object]:
        mapping = self.common_mapping()
        if self.producer_record_sha256 is not None:
            mapping["producer_record_sha256"] = self.producer_record_sha256
            mapping["timing_error_bound_tick"] = self.timing_error_bound_tick
        return mapping


@dataclass(frozen=True, slots=True)
class NativeTimedSpeechProfile:
    service_sha256: str
    funasr_version: str
    torch_version: str
    max_request_bytes: int
    native_port_identity_sha256: str
    producers: tuple[NativeTimedSpeechProducer, NativeTimedSpeechProducer]

    def to_mapping(self) -> dict[str, object]:
        return {
            "provider_id": "funasr-http-v1",
            "provider_version": "1.0.0",
            "service_sha256": self.service_sha256,
            "funasr_version": self.funasr_version,
            "torch_version": self.torch_version,
            "device": "cpu",
            "word_timing_capability": "required",
            "max_request_bytes": self.max_request_bytes,
            "native_port_identity_sha256": self.native_port_identity_sha256,
            "producers": [producer.to_mapping() for producer in self.producers],
        }


@dataclass(frozen=True, slots=True)
class SourceClockPolicy:
    policy_sha256: str
    clock_id: str
    time_base: TimeBase

    def to_mapping(self) -> dict[str, object]:
        return {
            "policy_sha256": self.policy_sha256,
            "clock_id": self.clock_id,
            "time_base": {
                "numerator": self.time_base.numerator,
                "denominator": self.time_base.denominator,
            },
            "origin_rule": "declared_source_audio_origin_tick",
            "range_rule": "complete_source_audio_range_only",
            "millisecond_conversion_rule": "floor_start_ceil_end_integer_v1",
        }


@dataclass(frozen=True, slots=True)
class TimingPolicies:
    timed_speech_policy_sha256: str
    word_gap_policy_sha256: str
    vad_merge_policy_sha256: str
    alignment_policy_sha256: str
    acceptance_policy_sha256: str
    word_gap_ms: int
    vad_merge_gap_ms: int

    def to_mapping(self) -> dict[str, object]:
        return {
            "timed_speech_policy_sha256": self.timed_speech_policy_sha256,
            "word_gap_policy_sha256": self.word_gap_policy_sha256,
            "vad_merge_policy_sha256": self.vad_merge_policy_sha256,
            "alignment_policy_sha256": self.alignment_policy_sha256,
            "acceptance_policy_sha256": self.acceptance_policy_sha256,
            "word_gap_ms": self.word_gap_ms,
            "vad_merge_gap_ms": self.vad_merge_gap_ms,
        }


@dataclass(frozen=True, slots=True)
class AuthorityProfileCapabilities:
    shadow_measurement: bool
    authority_registry_compile: bool
    authority_bootstrap: bool
    http_media_preflight: bool
    local_pipeline_run: bool
    local_render_qc: bool
    semantic_highlight_read: bool
    external_publication: bool
    runtime_profile_selection: bool

    def to_mapping(self) -> dict[str, object]:
        return {
            "shadow_measurement": self.shadow_measurement,
            "authority_registry_compile": self.authority_registry_compile,
            "authority_bootstrap": self.authority_bootstrap,
            "http_media_preflight": self.http_media_preflight,
            "local_pipeline_run": self.local_pipeline_run,
            "local_render_qc": self.local_render_qc,
            "semantic_highlight_read": self.semantic_highlight_read,
            "external_publication": self.external_publication,
            "runtime_profile_selection": self.runtime_profile_selection,
        }


@dataclass(frozen=True, slots=True)
class CalibrationCorpusMember:
    member_id: str
    corpus_member_reference_sha256: str
    source_id: str
    source_sha256: str
    source_blob_reference_sha256: str
    expected_anchor_reference_sha256: str

    def to_mapping(self) -> dict[str, object]:
        return {
            "member_id": self.member_id,
            "corpus_member_reference_sha256": self.corpus_member_reference_sha256,
            "source_id": self.source_id,
            "source_sha256": self.source_sha256,
            "source_blob_reference_sha256": self.source_blob_reference_sha256,
            "expected_anchor_reference_sha256": self.expected_anchor_reference_sha256,
        }


@dataclass(frozen=True, slots=True)
class CalibrationCorpus:
    corpus_set_sha256: str
    members: tuple[CalibrationCorpusMember, ...]

    def to_mapping(self) -> dict[str, object]:
        return {
            "corpus_set_sha256": self.corpus_set_sha256,
            "members": [member.to_mapping() for member in self.members],
        }


@dataclass(frozen=True, slots=True)
class CalibrationAcceptance:
    max_successor_attempts: int

    def to_mapping(self) -> dict[str, object]:
        return {
            "aggregation_strategy": "member-bound-calibration-statistics-v1",
            "alignment_strategy": "complete-ordered-one-to-one-v1",
            "require_complete_member_set": True,
            "require_zero_invalid_members": True,
            "require_positive_asr_bound": True,
            "require_positive_vad_bound": True,
            "max_successor_attempts": self.max_successor_attempts,
        }


@dataclass(frozen=True, slots=True)
class ShadowCalibrationProfileSource:
    profile_version: str
    profile_contract_sha256: str
    source_sha256: str
    canonical_sha256: str
    stage1_narrative_profile: Stage1NarrativeProfileReference
    native_timed_speech: NativeTimedSpeechProfile
    source_clock_policy: SourceClockPolicy
    calibration_corpus: CalibrationCorpus
    timing_policies: TimingPolicies
    capabilities: AuthorityProfileCapabilities
    calibration_acceptance: CalibrationAcceptance

    @property
    def profile_key(self) -> str:
        return f"shadow_calibration@{self.profile_version}"

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": SHADOW_CALIBRATION_SCHEMA_VERSION,
            "contract_version": CONTRACT_VERSION,
            "profile_id": "shadow_calibration",
            "profile_version": self.profile_version,
            "profile_state": "shadow_calibration_v1",
            "profile_contract_sha256": self.profile_contract_sha256,
            "stage1_narrative_profile": self.stage1_narrative_profile.to_mapping(),
            "native_timed_speech": self.native_timed_speech.to_mapping(),
            "source_clock_policy": self.source_clock_policy.to_mapping(),
            "calibration_corpus": self.calibration_corpus.to_mapping(),
            "timing_policies": self.timing_policies.to_mapping(),
            "capabilities": self.capabilities.to_mapping(),
            "calibration_acceptance": self.calibration_acceptance.to_mapping(),
        }


@dataclass(frozen=True, slots=True)
class ShadowProfileReference:
    profile_version: str
    source_sha256: str
    registry_set_sha256: str
    authority_lock_sha256: str

    def to_mapping(self) -> dict[str, object]:
        return {
            "profile_id": "shadow_calibration",
            "profile_version": self.profile_version,
            "source_sha256": self.source_sha256,
            "registry_set_sha256": self.registry_set_sha256,
            "authority_lock_sha256": self.authority_lock_sha256,
        }


@dataclass(frozen=True, slots=True)
class LocalRunCalibration:
    record_ref: CommittedArtifactMemberReference
    validation_receipt_ref: CommittedArtifactMemberReference
    asr_producer_record_sha256: str
    vad_producer_record_sha256: str
    asr_timing_error_bound_tick: int
    vad_timing_error_bound_tick: int

    def to_mapping(self) -> dict[str, object]:
        return {
            "record_ref": self.record_ref.to_mapping(),
            "validation_receipt_ref": self.validation_receipt_ref.to_mapping(),
            "asr_producer_record_sha256": self.asr_producer_record_sha256,
            "vad_producer_record_sha256": self.vad_producer_record_sha256,
            "asr_timing_error_bound_tick": self.asr_timing_error_bound_tick,
            "vad_timing_error_bound_tick": self.vad_timing_error_bound_tick,
        }


@dataclass(frozen=True, slots=True)
class LocalRunProfileSource:
    profile_version: str
    profile_contract_sha256: str
    source_sha256: str
    canonical_sha256: str
    predecessor_shadow_profile: ShadowProfileReference
    stage1_narrative_profile: Stage1NarrativeProfileReference
    native_timed_speech: NativeTimedSpeechProfile
    source_clock_policy: SourceClockPolicy
    timing_policies: TimingPolicies
    capabilities: AuthorityProfileCapabilities
    calibration: LocalRunCalibration
    timed_speech_registry_entry: TimedSpeechProfileRegistryEntry
    stage2_command_policy: Stage2CommandPolicy
    stage3_command_policy: Stage3CommandPolicy

    @property
    def stage2_command_policy_sha256(self) -> str:
        return self.stage2_command_policy.canonical_hash

    @property
    def stage3_command_policy_sha256(self) -> str:
        return self.stage3_command_policy.canonical_hash

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": LOCAL_RUN_SCHEMA_VERSION,
            "contract_version": CONTRACT_VERSION,
            "profile_id": "local_run",
            "profile_version": self.profile_version,
            "profile_state": "local_run_v1",
            "profile_contract_sha256": self.profile_contract_sha256,
            "predecessor_shadow_profile": self.predecessor_shadow_profile.to_mapping(),
            "stage1_narrative_profile": self.stage1_narrative_profile.to_mapping(),
            "native_timed_speech": self.native_timed_speech.to_mapping(),
            "source_clock_policy": self.source_clock_policy.to_mapping(),
            "timing_policies": self.timing_policies.to_mapping(),
            "capabilities": self.capabilities.to_mapping(),
            "calibration": self.calibration.to_mapping(),
            "timed_speech_registry_entry": self.timed_speech_registry_entry.to_mapping(),
            "stage2_command_policy": self.stage2_command_policy.to_mapping(),
            "stage2_command_policy_sha256": self.stage2_command_policy_sha256,
            "stage3_command_policy": self.stage3_command_policy.to_mapping(),
            "stage3_command_policy_sha256": self.stage3_command_policy_sha256,
        }


@dataclass(frozen=True, slots=True)
class UnresolvedAuthorityProfileSourceSet:
    """Grammar-closed sources whose provenance/member refs are not yet resolved."""

    narrative: Stage1NarrativeProfileSource
    shadow: ShadowCalibrationProfileSource
    local_run: LocalRunProfileSource | None
    resolution_state: Literal["grammar_only_unresolved"] = "grammar_only_unresolved"


def _decode_narrative_reference(value: object, field_name: str) -> Stage1NarrativeProfileReference:
    fields = frozenset(
        {
            "profile_id", "profile_version", "source_sha256", "provider_id",
            "adapter_strategy_version", "model_id", "prompt_version",
            "prompt_template_sha256", "response_schema_sha256", "parser_strategy_version",
            "parser_contract_sha256", "request_parameters_sha256", "parse_policy_sha256",
            "retry_policy_sha256", "window_sampling_policy_sha256", "stage1_command_policy_sha256",
        }
    )
    mapping = _mapping(value, fields, field_name)
    for name, expected in (
        ("profile_id", "stage1_narrative"),
        ("provider_id", "doubao-ark-responses-stream"),
        ("adapter_strategy_version", "doubao-ark-files-responses-stream-v2"),
        ("model_id", "doubao-seed-2-1-pro-260628"),
        ("prompt_version", "vlm-semantic-pack-v3"),
        ("parser_strategy_version", "strict-semantic-pack-v3"),
    ):
        _const(mapping[name], expected, f"{field_name}.{name}")
    return Stage1NarrativeProfileReference(
        _profile_version(mapping["profile_version"], f"{field_name}.profile_version"),
        _sha(mapping["source_sha256"], f"{field_name}.source_sha256"),
        *(
            _sha(mapping[name], f"{field_name}.{name}")
            for name in (
                "prompt_template_sha256", "response_schema_sha256", "parser_contract_sha256",
                "request_parameters_sha256", "parse_policy_sha256", "retry_policy_sha256",
                "window_sampling_policy_sha256", "stage1_command_policy_sha256",
            )
        ),
    )


def decode_stage1_narrative_profile_source(raw: bytes) -> Stage1NarrativeProfileSource:
    """Decode one locked Stage-1 narrative source without accepting defaults."""
    # Pure policy decoding must not import pipeline facades or DB adapters.
    from ..semantic_chain.stage1_command_policy import Stage1CommandPolicy

    value, source_sha256, semantic_sha256 = _source_object(raw, "stage1 narrative source")
    mapping = _mapping(
        value,
        frozenset(
            {
                "schema_version", "contract_version", "profile_id", "profile_version",
                "profile_state", "provider", "model", "prompt", "response_schema", "parser",
                "policies", "stage1_command_policy", "capabilities",
            }
        ),
        "stage1 narrative source",
    )
    for name, expected in (
        ("schema_version", STAGE1_NARRATIVE_SCHEMA_VERSION),
        ("contract_version", CONTRACT_VERSION),
        ("profile_id", "stage1_narrative"),
        ("profile_state", "stage1_narrative_v1"),
    ):
        _const(mapping[name], expected, f"stage1 narrative source.{name}")
    version = _profile_version(mapping["profile_version"], "stage1 narrative source.profile_version")
    provider = _mapping(
        mapping["provider"],
        frozenset({"provider_id", "adapter_strategy_version", "transport"}),
        "stage1 narrative source.provider",
    )
    for name, expected in (
        ("provider_id", "doubao-ark-responses-stream"),
        ("adapter_strategy_version", "doubao-ark-files-responses-stream-v2"),
        ("transport", "ark_responses_streaming"),
    ):
        _const(provider[name], expected, f"stage1 narrative source.provider.{name}")
    model = _mapping(mapping["model"], frozenset({"model_id"}), "stage1 narrative source.model")
    _const(model["model_id"], "doubao-seed-2-1-pro-260628", "stage1 narrative source.model.model_id")
    prompt = _mapping(
        mapping["prompt"], frozenset({"version", "template_sha256"}), "stage1 narrative source.prompt"
    )
    _const(prompt["version"], "vlm-semantic-pack-v3", "stage1 narrative source.prompt.version")
    response_schema = _mapping(
        mapping["response_schema"], frozenset({"schema_sha256"}), "stage1 narrative source.response_schema"
    )
    parser = _mapping(
        mapping["parser"], frozenset({"strategy_version", "contract_sha256"}), "stage1 narrative source.parser"
    )
    _const(
        parser["strategy_version"],
        "strict-semantic-pack-v3",
        "stage1 narrative source.parser.strategy_version",
    )
    policy_names = (
        "request_parameters_sha256", "parse_policy_sha256", "retry_policy_sha256",
        "window_sampling_policy_sha256", "stage1_command_policy_sha256",
    )
    policies = _mapping(mapping["policies"], frozenset(policy_names), "stage1 narrative source.policies")
    try:
        command_policy = Stage1CommandPolicy.from_mapping(mapping["stage1_command_policy"])
    except (ValueError, TypeError) as error:
        raise _invalid("stage1 narrative source command policy is invalid") from error
    _const(
        command_policy.generation.model_id,
        "doubao-seed-2-1-pro-260628",
        "stage1 narrative source command policy model",
    )
    if (
        canonical_json_hash(mapping["stage1_command_policy"]) != command_policy.canonical_hash
        or policies["stage1_command_policy_sha256"] != command_policy.canonical_hash
    ):
        raise _invalid("stage1 narrative source command policy hash does not close")
    capabilities = _mapping(
        mapping["capabilities"],
        frozenset({"narrative_evidence_generation", "stage1_compile", "external_publication"}),
        "stage1 narrative source.capabilities",
    )
    for name, expected in (
        ("narrative_evidence_generation", True),
        ("stage1_compile", True),
        ("external_publication", False),
    ):
        _const(_boolean(capabilities[name], f"stage1 narrative source.capabilities.{name}"), expected, name)
    reference = Stage1NarrativeProfileReference(
        version,
        source_sha256,
        _sha(prompt["template_sha256"], "stage1 narrative source.prompt.template_sha256"),
        _sha(response_schema["schema_sha256"], "stage1 narrative source.response_schema.schema_sha256"),
        _sha(parser["contract_sha256"], "stage1 narrative source.parser.contract_sha256"),
        *(_sha(policies[name], f"stage1 narrative source.policies.{name}") for name in policy_names),
    )
    return Stage1NarrativeProfileSource(version, source_sha256, semantic_sha256, reference, command_policy)


def _decode_native_timed_speech(value: object, field_name: str, *, local_run: bool) -> NativeTimedSpeechProfile:
    mapping = _mapping(
        value,
        frozenset(
            {
                "provider_id", "provider_version", "service_sha256", "funasr_version",
                "torch_version", "device", "word_timing_capability", "max_request_bytes",
                "native_port_identity_sha256", "producers",
            }
        ),
        field_name,
    )
    for name, expected in (
        ("provider_id", "funasr-http-v1"),
        ("provider_version", "1.0.0"),
        ("device", "cpu"),
        ("word_timing_capability", "required"),
    ):
        _const(mapping[name], expected, f"{field_name}.{name}")
    service_sha256 = _sha(mapping["service_sha256"], f"{field_name}.service_sha256")
    producer_fields = {
        "producer_kind", "producer_id", "producer_version", "generation_policy_sha256",
        "detector_sha256", "calibration_policy_sha256", "model_id", "model_revision",
        "model_sha256", "inference_kind", "service_sha256",
    }
    if local_run:
        producer_fields.update({"producer_record_sha256", "timing_error_bound_tick"})
    raw_producers = _array(mapping["producers"], f"{field_name}.producers")
    if len(raw_producers) != 2:
        raise _invalid(f"{field_name}.producers must contain ordered ASR then VAD")
    producers: list[NativeTimedSpeechProducer] = []
    expected_matrix = (
        ("asr", "SenseVoiceSmall", "sensevoice-word-timestamp"),
        ("vad", "fsmn-vad", "fsmn-vad-direct"),
    )
    for index, (raw_producer, (kind, model_id, inference_kind)) in enumerate(
        zip(raw_producers, expected_matrix, strict=True)
    ):
        name = f"{field_name}.producers[{index}]"
        producer = _mapping(raw_producer, frozenset(producer_fields), name)
        for key, expected in (
            ("producer_kind", kind), ("model_id", model_id), ("inference_kind", inference_kind)
        ):
            _const(producer[key], expected, f"{name}.{key}")
        producer_service = _sha(producer["service_sha256"], f"{name}.service_sha256")
        if producer_service != service_sha256:
            raise _invalid(f"{name}.service_sha256 does not close to the parent identity")
        producers.append(
            NativeTimedSpeechProducer(
                kind,
                _text(producer["producer_id"], f"{name}.producer_id", canonical_id=True),
                _text(producer["producer_version"], f"{name}.producer_version"),
                _sha(producer["generation_policy_sha256"], f"{name}.generation_policy_sha256"),
                _sha(producer["detector_sha256"], f"{name}.detector_sha256"),
                _sha(producer["calibration_policy_sha256"], f"{name}.calibration_policy_sha256"),
                model_id,
                _text(producer["model_revision"], f"{name}.model_revision"),
                _sha(producer["model_sha256"], f"{name}.model_sha256"),
                inference_kind,
                producer_service,
                _sha(producer["producer_record_sha256"], f"{name}.producer_record_sha256")
                if local_run
                else None,
                _integer(producer["timing_error_bound_tick"], f"{name}.timing_error_bound_tick", minimum=1)
                if local_run
                else None,
            )
        )
    asr, vad = producers
    if (
        asr.producer_id == vad.producer_id
        or asr.detector_sha256 == vad.detector_sha256
        or asr.model_sha256 == vad.model_sha256
    ):
        raise _invalid(f"{field_name}.producers require distinct ASR and VAD identities")
    if local_run and asr.producer_record_sha256 == vad.producer_record_sha256:
        raise _invalid(f"{field_name}.producers require distinct child calibration records")
    return NativeTimedSpeechProfile(
        service_sha256,
        _text(mapping["funasr_version"], f"{field_name}.funasr_version"),
        _text(mapping["torch_version"], f"{field_name}.torch_version"),
        _integer(mapping["max_request_bytes"], f"{field_name}.max_request_bytes", minimum=1),
        _sha(mapping["native_port_identity_sha256"], f"{field_name}.native_port_identity_sha256"),
        (asr, vad),
    )


def _decode_source_clock(value: object, field_name: str) -> SourceClockPolicy:
    mapping = _mapping(
        value,
        frozenset(
            {
                "policy_sha256", "clock_id", "time_base", "origin_rule", "range_rule",
                "millisecond_conversion_rule",
            }
        ),
        field_name,
    )
    for name, expected in (
        ("origin_rule", "declared_source_audio_origin_tick"),
        ("range_rule", "complete_source_audio_range_only"),
        ("millisecond_conversion_rule", "floor_start_ceil_end_integer_v1"),
    ):
        _const(mapping[name], expected, f"{field_name}.{name}")
    raw_base = _mapping(mapping["time_base"], frozenset({"numerator", "denominator"}), f"{field_name}.time_base")
    try:
        time_base = TimeBase(
            _integer(raw_base["numerator"], f"{field_name}.time_base.numerator", minimum=1),
            _integer(raw_base["denominator"], f"{field_name}.time_base.denominator", minimum=1),
        )
    except ValueError as error:
        raise _invalid(f"{field_name}.time_base is not a reduced positive rational") from error
    return SourceClockPolicy(
        _sha(mapping["policy_sha256"], f"{field_name}.policy_sha256"),
        _text(mapping["clock_id"], f"{field_name}.clock_id", canonical_id=True),
        time_base,
    )


def _decode_timing_policies(value: object, field_name: str) -> TimingPolicies:
    names = (
        "timed_speech_policy_sha256", "word_gap_policy_sha256", "vad_merge_policy_sha256",
        "alignment_policy_sha256", "acceptance_policy_sha256",
    )
    mapping = _mapping(value, frozenset((*names, "word_gap_ms", "vad_merge_gap_ms")), field_name)
    return TimingPolicies(
        timed_speech_policy_sha256=_sha(
            mapping["timed_speech_policy_sha256"],
            f"{field_name}.timed_speech_policy_sha256",
        ),
        word_gap_policy_sha256=_sha(
            mapping["word_gap_policy_sha256"], f"{field_name}.word_gap_policy_sha256"
        ),
        vad_merge_policy_sha256=_sha(
            mapping["vad_merge_policy_sha256"], f"{field_name}.vad_merge_policy_sha256"
        ),
        alignment_policy_sha256=_sha(
            mapping["alignment_policy_sha256"], f"{field_name}.alignment_policy_sha256"
        ),
        acceptance_policy_sha256=_sha(
            mapping["acceptance_policy_sha256"], f"{field_name}.acceptance_policy_sha256"
        ),
        word_gap_ms=_integer(
            mapping["word_gap_ms"], f"{field_name}.word_gap_ms", minimum=0
        ),
        vad_merge_gap_ms=_integer(
            mapping["vad_merge_gap_ms"], f"{field_name}.vad_merge_gap_ms", minimum=0
        ),
    )


def _decode_capabilities(value: object, field_name: str, expected: tuple[bool, ...]) -> AuthorityProfileCapabilities:
    names = (
        "shadow_measurement", "authority_registry_compile", "authority_bootstrap",
        "http_media_preflight", "local_pipeline_run", "local_render_qc",
        "semantic_highlight_read", "external_publication", "runtime_profile_selection",
    )
    mapping = _mapping(value, frozenset(names), field_name)
    actual = tuple(_boolean(mapping[name], f"{field_name}.{name}") for name in names)
    if actual != expected:
        raise _invalid(f"{field_name} is not the locked capability matrix")
    return AuthorityProfileCapabilities(*actual)


def _decode_corpus(value: object, field_name: str) -> CalibrationCorpus:
    mapping = _mapping(value, frozenset({"corpus_set_sha256", "members"}), field_name)
    raw_members = _array(mapping["members"], f"{field_name}.members", non_empty=True)
    fields = frozenset(
        {
            "member_id", "corpus_member_reference_sha256", "source_id", "source_sha256",
            "source_blob_reference_sha256", "expected_anchor_reference_sha256",
        }
    )
    members: list[CalibrationCorpusMember] = []
    for index, raw_member in enumerate(raw_members):
        name = f"{field_name}.members[{index}]"
        member = _mapping(raw_member, fields, name)
        members.append(
            CalibrationCorpusMember(
                _text(member["member_id"], f"{name}.member_id", canonical_id=True),
                _sha(member["corpus_member_reference_sha256"], f"{name}.corpus_member_reference_sha256"),
                _text(member["source_id"], f"{name}.source_id", canonical_id=True),
                _sha(member["source_sha256"], f"{name}.source_sha256"),
                _sha(member["source_blob_reference_sha256"], f"{name}.source_blob_reference_sha256"),
                _sha(member["expected_anchor_reference_sha256"], f"{name}.expected_anchor_reference_sha256"),
            )
        )
    if tuple(member.member_id for member in members) != tuple(sorted(member.member_id for member in members)):
        raise _invalid(f"{field_name}.members must be in canonical member_id order")
    unique_columns = (
        (member.member_id for member in members),
        (member.corpus_member_reference_sha256 for member in members),
        (member.source_id for member in members),
        (member.source_sha256 for member in members),
        (member.source_blob_reference_sha256 for member in members),
        (member.expected_anchor_reference_sha256 for member in members),
    )
    if any(len(set(column)) != len(members) for column in unique_columns):
        raise _invalid(f"{field_name}.members contain a duplicate identity")
    corpus_set_sha256 = _sha(mapping["corpus_set_sha256"], f"{field_name}.corpus_set_sha256")
    if corpus_set_sha256 != canonical_json_hash([member.to_mapping() for member in members]):
        raise _invalid(f"{field_name}.corpus_set_sha256 does not match the canonical member array")
    return CalibrationCorpus(corpus_set_sha256, tuple(members))


def _decode_acceptance(value: object, field_name: str) -> CalibrationAcceptance:
    mapping = _mapping(
        value,
        frozenset(
            {
                "aggregation_strategy", "alignment_strategy", "require_complete_member_set",
                "require_zero_invalid_members", "require_positive_asr_bound",
                "require_positive_vad_bound", "max_successor_attempts",
            }
        ),
        field_name,
    )
    for name, expected in (
        ("aggregation_strategy", "member-bound-calibration-statistics-v1"),
        ("alignment_strategy", "complete-ordered-one-to-one-v1"),
        ("require_complete_member_set", True),
        ("require_zero_invalid_members", True),
        ("require_positive_asr_bound", True),
        ("require_positive_vad_bound", True),
    ):
        if type(expected) is bool:
            _const(_boolean(mapping[name], f"{field_name}.{name}"), expected, f"{field_name}.{name}")
        else:
            _const(mapping[name], expected, f"{field_name}.{name}")
    return CalibrationAcceptance(
        _integer(mapping["max_successor_attempts"], f"{field_name}.max_successor_attempts", minimum=0, maximum=1)
    )


def decode_shadow_calibration_profile_source(
    raw: bytes,
    *,
    narrative: Stage1NarrativeProfileSource,
    expected_profile_contract_sha256: str,
) -> ShadowCalibrationProfileSource:
    """Decode shadow grammar and bind its contract claim to an independent hash."""
    if type(narrative) is not Stage1NarrativeProfileSource:  # noqa: E721
        raise _invalid("shadow narrative dependency must be an exact decoded source")
    value, source_sha256, semantic_sha256 = _source_object(raw, "shadow calibration source")
    mapping = _mapping(
        value,
        frozenset(
            {
                "schema_version", "contract_version", "profile_id", "profile_version",
                "profile_state", "profile_contract_sha256", "stage1_narrative_profile",
                "native_timed_speech", "source_clock_policy", "calibration_corpus",
                "timing_policies", "capabilities", "calibration_acceptance",
            }
        ),
        "shadow calibration source",
    )
    for name, expected in (
        ("schema_version", SHADOW_CALIBRATION_SCHEMA_VERSION),
        ("contract_version", CONTRACT_VERSION),
        ("profile_id", "shadow_calibration"),
        ("profile_state", "shadow_calibration_v1"),
    ):
        _const(mapping[name], expected, f"shadow calibration source.{name}")
    expected_contract = _sha(
        expected_profile_contract_sha256,
        "expected shadow profile contract SHA-256",
    )
    claimed_contract = _sha(
        mapping["profile_contract_sha256"],
        "shadow calibration source.profile_contract_sha256",
    )
    if claimed_contract != expected_contract:
        raise _invalid("shadow calibration source profile contract does not match locked input")
    narrative_ref = _decode_narrative_reference(
        mapping["stage1_narrative_profile"], "shadow calibration source.stage1_narrative_profile"
    )
    if narrative_ref != narrative.reference:
        raise _invalid("shadow calibration source narrative reference does not resolve exactly")
    return ShadowCalibrationProfileSource(
        _profile_version(mapping["profile_version"], "shadow calibration source.profile_version"),
        claimed_contract,
        source_sha256,
        semantic_sha256,
        narrative_ref,
        _decode_native_timed_speech(
            mapping["native_timed_speech"], "shadow calibration source.native_timed_speech", local_run=False
        ),
        _decode_source_clock(mapping["source_clock_policy"], "shadow calibration source.source_clock_policy"),
        _decode_corpus(mapping["calibration_corpus"], "shadow calibration source.calibration_corpus"),
        _decode_timing_policies(mapping["timing_policies"], "shadow calibration source.timing_policies"),
        _decode_capabilities(
            mapping["capabilities"],
            "shadow calibration source.capabilities",
            (True, False, False, False, False, False, False, False, False),
        ),
        _decode_acceptance(mapping["calibration_acceptance"], "shadow calibration source.calibration_acceptance"),
    )


def _decode_shadow_reference(value: object, field_name: str) -> ShadowProfileReference:
    mapping = _mapping(
        value,
        frozenset(
            {"profile_id", "profile_version", "source_sha256", "registry_set_sha256", "authority_lock_sha256"}
        ),
        field_name,
    )
    _const(mapping["profile_id"], "shadow_calibration", f"{field_name}.profile_id")
    return ShadowProfileReference(
        _profile_version(mapping["profile_version"], f"{field_name}.profile_version"),
        _sha(mapping["source_sha256"], f"{field_name}.source_sha256"),
        _sha(mapping["registry_set_sha256"], f"{field_name}.registry_set_sha256"),
        _sha(mapping["authority_lock_sha256"], f"{field_name}.authority_lock_sha256"),
    )


def _decode_member_reference(
    value: object, field_name: str, *, artifact_type: str, shadow_profile_key: str
) -> CommittedArtifactMemberReference:
    mapping = _mapping(
        value,
        frozenset(
            {
                "artifact_set_id", "artifact_type", "content_hash", "logical_id",
                "member_ordinal", "receipt_id", "revision", "scope",
            }
        ),
        field_name,
    )
    _const(mapping["artifact_type"], artifact_type, f"{field_name}.artifact_type")
    scope = _mapping(mapping["scope"], frozenset({"namespace", "kind", "key"}), f"{field_name}.scope")
    _const(scope["namespace"], "autocut_authority", f"{field_name}.scope.namespace")
    _const(scope["kind"], "calibration", f"{field_name}.scope.kind")
    _const(scope["key"], shadow_profile_key, f"{field_name}.scope.key")

    def canonical_uuid(raw_uuid: object, name: str) -> UUID:
        text = _text(raw_uuid, name)
        try:
            parsed = UUID(text)
        except ValueError as error:
            raise _invalid(f"{name} must be a canonical UUID") from error
        if str(parsed) != text:
            raise _invalid(f"{name} must be a canonical UUID")
        return parsed

    try:
        return CommittedArtifactMemberReference(
            canonical_uuid(mapping["receipt_id"], f"{field_name}.receipt_id"),
            canonical_uuid(mapping["artifact_set_id"], f"{field_name}.artifact_set_id"),
            _integer(mapping["member_ordinal"], f"{field_name}.member_ordinal", minimum=0),
            ArtifactScope(
                cast(str, scope["namespace"]), cast(str, scope["kind"]), cast(str, scope["key"])
            ),
            artifact_type,
            _text(mapping["logical_id"], f"{field_name}.logical_id"),
            _integer(mapping["revision"], f"{field_name}.revision", minimum=1),
            _sha(mapping["content_hash"], f"{field_name}.content_hash"),
        )
    except ValueError as error:
        if isinstance(error, AuthorityProfileSourceError):
            raise
        raise _invalid(f"{field_name} is invalid") from error


def _decode_calibration(value: object, field_name: str, shadow: ShadowCalibrationProfileSource) -> LocalRunCalibration:
    mapping = _mapping(
        value,
        frozenset(
            {
                "record_ref", "validation_receipt_ref", "asr_producer_record_sha256",
                "vad_producer_record_sha256", "asr_timing_error_bound_tick",
                "vad_timing_error_bound_tick",
            }
        ),
        field_name,
    )
    record_ref = _decode_member_reference(
        mapping["record_ref"], f"{field_name}.record_ref", artifact_type="calibration_record",
        shadow_profile_key=shadow.profile_key,
    )
    receipt_ref = _decode_member_reference(
        mapping["validation_receipt_ref"], f"{field_name}.validation_receipt_ref",
        artifact_type="calibration_validation_receipt", shadow_profile_key=shadow.profile_key,
    )
    if (
        record_ref.receipt_id != receipt_ref.receipt_id
        or record_ref.artifact_set_id != receipt_ref.artifact_set_id
        or record_ref.member_ordinal == receipt_ref.member_ordinal
    ):
        raise _invalid(f"{field_name} refs must name distinct members of one committed validation set")
    asr_record = _sha(mapping["asr_producer_record_sha256"], f"{field_name}.asr_producer_record_sha256")
    vad_record = _sha(mapping["vad_producer_record_sha256"], f"{field_name}.vad_producer_record_sha256")
    if asr_record == vad_record:
        raise _invalid(f"{field_name} requires distinct ASR and VAD child records")
    if record_ref.content_hash in {asr_record, vad_record}:
        raise _invalid(f"{field_name} aggregate record must differ from both child records")
    return LocalRunCalibration(
        record_ref,
        receipt_ref,
        asr_record,
        vad_record,
        _integer(mapping["asr_timing_error_bound_tick"], f"{field_name}.asr_timing_error_bound_tick", minimum=1),
        _integer(mapping["vad_timing_error_bound_tick"], f"{field_name}.vad_timing_error_bound_tick", minimum=1),
    )


def _milliseconds_to_ticks(milliseconds: int, time_base: TimeBase) -> int:
    denominator = 1_000 * time_base.numerator
    return (milliseconds * time_base.denominator + denominator - 1) // denominator


def _validate_registry_projection(
    entry: TimedSpeechProfileRegistryEntry,
    *,
    profile_version: str,
    native: NativeTimedSpeechProfile,
    clock: SourceClockPolicy,
    timing: TimingPolicies,
    calibration: LocalRunCalibration,
) -> None:
    asr, vad = native.producers
    if entry.profile_id != "local_run" or entry.profile_version != profile_version:
        raise _invalid("timed speech registry entry does not identify the local-run source")
    requirements = (entry.transcript_requirement, entry.vad_requirement)
    for requirement, producer, record_hash in zip(
        requirements,
        (asr, vad),
        (calibration.asr_producer_record_sha256, calibration.vad_producer_record_sha256),
        strict=True,
    ):
        if (
            requirement.producer_id != producer.producer_id
            or requirement.generation_policy_sha256 != producer.generation_policy_sha256
            or requirement.model_sha256 != producer.detector_sha256
            or requirement.adapter_sha256 != native.native_port_identity_sha256
            or requirement.calibration_record_sha256 != record_hash
            or requirement.clock_id != clock.clock_id
            or requirement.time_base != clock.time_base
            or requirement.producer_kind != producer.producer_kind
            or requirement.inference_kind != producer.inference_kind
        ):
            raise _invalid("timed speech registry producer projection does not close")
    policy = entry.guard_policy
    if (
        policy.policy_sha256 != timing.timed_speech_policy_sha256
        or policy.source_audio_clock_id != clock.clock_id
        or policy.source_audio_time_base != clock.time_base
        or policy.word_gap_tick != _milliseconds_to_ticks(timing.word_gap_ms, clock.time_base)
        or policy.vad_merge_gap_tick != _milliseconds_to_ticks(timing.vad_merge_gap_ms, clock.time_base)
        or policy.pre_roll_tick != 0
        or policy.post_roll_tick != 0
    ):
        raise _invalid("timed speech registry guard policy projection does not close")


def decode_local_run_profile_source(
    raw: bytes,
    *,
    narrative: Stage1NarrativeProfileSource,
    shadow: ShadowCalibrationProfileSource,
    expected_profile_contract_sha256: str,
) -> LocalRunProfileSource:
    """Decode unresolved local-run grammar and close predecessor projections."""
    from ..semantic_chain.editorial_command_policy import Stage3CommandPolicy
    from ..semantic_chain.story_design_command_policy import Stage2CommandPolicy

    if type(narrative) is not Stage1NarrativeProfileSource or type(shadow) is not ShadowCalibrationProfileSource:  # noqa: E721
        raise _invalid("local-run dependencies must be exact decoded sources")
    value, source_sha256, semantic_sha256 = _source_object(raw, "local-run source")
    mapping = _mapping(
        value,
        frozenset(
            {
                "schema_version", "contract_version", "profile_id", "profile_version",
                "profile_state", "profile_contract_sha256", "predecessor_shadow_profile",
                "stage1_narrative_profile", "native_timed_speech", "source_clock_policy",
                "timing_policies", "capabilities", "calibration", "timed_speech_registry_entry",
                "stage2_command_policy", "stage2_command_policy_sha256",
                "stage3_command_policy", "stage3_command_policy_sha256",
            }
        ),
        "local-run source",
    )
    for name, expected in (
        ("schema_version", LOCAL_RUN_SCHEMA_VERSION),
        ("contract_version", CONTRACT_VERSION),
        ("profile_id", "local_run"),
        ("profile_state", "local_run_v1"),
    ):
        _const(mapping[name], expected, f"local-run source.{name}")
    expected_contract = _sha(
        expected_profile_contract_sha256,
        "expected local-run profile contract SHA-256",
    )
    claimed_contract = _sha(
        mapping["profile_contract_sha256"],
        "local-run source.profile_contract_sha256",
    )
    if claimed_contract != expected_contract:
        raise _invalid("local-run source profile contract does not match locked input")
    version = _profile_version(mapping["profile_version"], "local-run source.profile_version")
    try:
        stage2_policy = Stage2CommandPolicy.from_mapping(mapping["stage2_command_policy"])
    except (ValueError, TypeError) as error:
        raise _invalid("local-run source Stage 2 command policy is invalid") from error
    _const(
        stage2_policy.generation.model_id,
        "doubao-seed-2-1-pro-260628",
        "local-run source Stage 2 command policy model",
    )
    if (
        canonical_json_hash(mapping["stage2_command_policy"]) != stage2_policy.canonical_hash
        or mapping["stage2_command_policy_sha256"] != stage2_policy.canonical_hash
    ):
        raise _invalid("local-run source Stage 2 command policy hash does not close")
    try:
        stage3_policy = Stage3CommandPolicy.from_mapping(mapping["stage3_command_policy"])
    except (ValueError, TypeError) as error:
        raise _invalid("local-run source Stage 3 command policy is invalid") from error
    _const(
        stage3_policy.generation.model_id,
        "doubao-seed-2-1-pro-260628",
        "local-run source Stage 3 command policy model",
    )
    if (
        canonical_json_hash(mapping["stage3_command_policy"]) != stage3_policy.canonical_hash
        or mapping["stage3_command_policy_sha256"] != stage3_policy.canonical_hash
    ):
        raise _invalid("local-run source Stage 3 command policy hash does not close")
    predecessor = _decode_shadow_reference(
        mapping["predecessor_shadow_profile"], "local-run source.predecessor_shadow_profile"
    )
    if predecessor.profile_version != shadow.profile_version or predecessor.source_sha256 != shadow.source_sha256:
        raise _invalid("local-run predecessor does not resolve to the decoded shadow source")
    narrative_ref = _decode_narrative_reference(
        mapping["stage1_narrative_profile"], "local-run source.stage1_narrative_profile"
    )
    if narrative_ref != narrative.reference or narrative_ref != shadow.stage1_narrative_profile:
        raise _invalid("local-run narrative reference does not resolve across the source set")
    native = _decode_native_timed_speech(
        mapping["native_timed_speech"], "local-run source.native_timed_speech", local_run=True
    )
    if tuple(item.common_mapping() for item in native.producers) != tuple(
        item.common_mapping() for item in shadow.native_timed_speech.producers
    ) or (
        native.service_sha256,
        native.funasr_version,
        native.torch_version,
        native.max_request_bytes,
        native.native_port_identity_sha256,
    ) != (
        shadow.native_timed_speech.service_sha256,
        shadow.native_timed_speech.funasr_version,
        shadow.native_timed_speech.torch_version,
        shadow.native_timed_speech.max_request_bytes,
        shadow.native_timed_speech.native_port_identity_sha256,
    ):
        raise _invalid("local-run native timed-speech identity does not inherit the shadow source")
    clock = _decode_source_clock(mapping["source_clock_policy"], "local-run source.source_clock_policy")
    timing = _decode_timing_policies(mapping["timing_policies"], "local-run source.timing_policies")
    if clock != shadow.source_clock_policy or timing != shadow.timing_policies:
        raise _invalid("local-run clock or timing policy does not inherit the shadow source")
    calibration = _decode_calibration(mapping["calibration"], "local-run source.calibration", shadow)
    asr, vad = native.producers
    if (
        asr.producer_record_sha256 != calibration.asr_producer_record_sha256
        or vad.producer_record_sha256 != calibration.vad_producer_record_sha256
        or asr.timing_error_bound_tick != calibration.asr_timing_error_bound_tick
        or vad.timing_error_bound_tick != calibration.vad_timing_error_bound_tick
    ):
        raise _invalid("local-run producer calibration does not close to the aggregate projection")
    try:
        entry = decode_timed_speech_profile_registry_entry(mapping["timed_speech_registry_entry"])
    except ValueError as error:
        raise _invalid("timed speech registry entry is invalid") from error
    _validate_registry_projection(
        entry,
        profile_version=version,
        native=native,
        clock=clock,
        timing=timing,
        calibration=calibration,
    )
    return LocalRunProfileSource(
        version,
        claimed_contract,
        source_sha256,
        semantic_sha256,
        predecessor,
        narrative_ref,
        native,
        clock,
        timing,
        _decode_capabilities(
            mapping["capabilities"],
            "local-run source.capabilities",
            (False, True, True, True, True, True, True, False, False),
        ),
        calibration,
        entry,
        stage2_policy,
        stage3_policy,
    )


def decode_authority_profile_source_grammar(
    *,
    narrative_raw: bytes,
    shadow_raw: bytes,
    expected_shadow_profile_contract_sha256: str,
    local_run_raw: bytes | None = None,
    expected_local_run_profile_contract_sha256: str | None = None,
) -> UnresolvedAuthorityProfileSourceSet:
    """Decode grammar only; returned references remain unresolved and non-authoritative."""
    if (local_run_raw is None) != (expected_local_run_profile_contract_sha256 is None):
        raise _invalid(
            "local-run source bytes and expected profile contract must be supplied together"
        )
    narrative = decode_stage1_narrative_profile_source(narrative_raw)
    shadow = decode_shadow_calibration_profile_source(
        shadow_raw,
        narrative=narrative,
        expected_profile_contract_sha256=expected_shadow_profile_contract_sha256,
    )
    local_run = (
        decode_local_run_profile_source(
            local_run_raw,
            narrative=narrative,
            shadow=shadow,
            expected_profile_contract_sha256=expected_local_run_profile_contract_sha256,
        )
        if local_run_raw is not None and expected_local_run_profile_contract_sha256 is not None
        else None
    )
    return UnresolvedAuthorityProfileSourceSet(narrative, shadow, local_run)


__all__ = [
    "AUTHORITY_PROFILE_SOURCE_INVALID",
    "AuthorityProfileSourceError",
    "LocalRunProfileSource",
    "ShadowCalibrationProfileSource",
    "Stage1NarrativeProfileSource",
    "UnresolvedAuthorityProfileSourceSet",
    "decode_authority_profile_source_grammar",
    "decode_local_run_profile_source",
    "decode_shadow_calibration_profile_source",
    "decode_stage1_narrative_profile_source",
]
