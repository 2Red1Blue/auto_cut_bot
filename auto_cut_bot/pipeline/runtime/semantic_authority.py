"""Digest-bound authority for the independently executable semantic plan.

This resource deliberately authorizes only SourcePrep and Doubao VLM semantic
evidence.  It is not a local-run profile and cannot authorize timed speech,
editing, rendering, publication, or an authority bootstrap.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from importlib import resources
from typing import cast

from autocut_kernel.vlm import GenerationRetryPolicy, VlmParsePolicy
from autocut_kernel.vlm.parser_contract import vlm_parser_contract_sha256

from auto_cut_bot.pipeline.source_prep.command import identity_window_sampling_policy_sha256
from auto_cut_bot.pipeline.vlm.prompt import vlm_prompt_template_sha256
from auto_cut_bot.pipeline.vlm.request_factory import DoubaoVlmRequestPolicy


class SemanticRunAuthorityError(ValueError):
    """The installed semantic-only authority is absent or inconsistent."""


_SCHEMA_VERSION = "autocut-semantic-run-authority-v1"
_CONTRACT_VERSION = "2.1.3"
_DIGEST_NAME = "semantic-run.sha256"
_RESOURCE_NAME = "semantic-run.json"
_FIELDS = frozenset({"capabilities", "contract_version", "retry_policy", "schema_version", "vlm"})
_VLM_FIELDS = frozenset({
    "adapter_strategy_version", "model_id", "parse_policy", "parser_contract_sha256",
    "parser_strategy_version", "prompt_template_sha256", "prompt_version", "provider_id",
    "request_parameters", "response_schema_sha256", "stage_strategy_version",
    "window_sampling_policy_sha256",
})
_CAPABILITY_FIELDS = frozenset({
    "authority_bootstrap", "external_publication", "physical_edit", "render_qc",
    "source_prep", "stage1_compile", "stage2_compile", "stage3_compile",
    "timed_speech", "vlm_semantic_evidence",
})
_REQUEST_FIELDS = frozenset({"max_output_tokens", "temperature", "video_fps"})
_RETRY_FIELDS = frozenset({"backoff_seconds", "max_attempts", "strategy_version"})
_PARSE_POLICY_FIELDS = frozenset({
    "max_candidate_hypotheses", "max_entities", "max_events", "max_facts",
    "max_measurements", "max_response_bytes", "max_temporal_segments",
    "max_text_characters", "max_total_text_characters",
})


def _closed_object(value: object, expected: frozenset[str], label: str) -> dict[str, object]:
    if type(value) is not dict or frozenset(cast(dict[str, object], value)) != expected:  # noqa: E721
        raise SemanticRunAuthorityError(f"{label} does not match its closed schema")
    return cast(dict[str, object], value)


def _strict_json(raw: bytes) -> dict[str, object]:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicates)
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise SemanticRunAuthorityError("semantic authority is not strict JSON") from error
    return _closed_object(value, _FIELDS, "semantic authority")


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _text_sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _required_int(value: dict[str, object], field: str) -> int:
    item = value.get(field)
    if type(item) is not int:  # noqa: E721
        raise SemanticRunAuthorityError(f"semantic authority {field} must be an integer")
    return item


@dataclass(frozen=True, slots=True)
class SemanticRunAuthority:
    """Exact VLM/retry policies and an intentionally narrow capability set."""

    vlm_policy: DoubaoVlmRequestPolicy
    retry_policy: GenerationRetryPolicy

    def __post_init__(self) -> None:
        if type(self.vlm_policy) is not DoubaoVlmRequestPolicy:  # noqa: E721
            raise SemanticRunAuthorityError("semantic authority requires an exact VLM policy")
        if type(self.retry_policy) is not GenerationRetryPolicy:  # noqa: E721
            raise SemanticRunAuthorityError("semantic authority requires an exact retry policy")


def decode_semantic_run_authority(raw: bytes, *, expected_sha256: str) -> SemanticRunAuthority:
    """Decode and recompute every executable semantic-policy identity."""
    if type(raw) is not bytes or not raw:  # noqa: E721
        raise SemanticRunAuthorityError("semantic authority bytes are missing")
    actual_sha256 = "sha256:" + hashlib.sha256(raw).hexdigest()
    if type(expected_sha256) is not str or actual_sha256 != expected_sha256:  # noqa: E721
        raise SemanticRunAuthorityError("semantic authority digest mismatch")
    document = _strict_json(raw)
    if document["schema_version"] != _SCHEMA_VERSION or document["contract_version"] != _CONTRACT_VERSION:
        raise SemanticRunAuthorityError("semantic authority version is unsupported")
    capabilities = _closed_object(document["capabilities"], _CAPABILITY_FIELDS, "capabilities")
    expected_capabilities = {
        "authority_bootstrap": False, "external_publication": False, "physical_edit": False,
        "render_qc": False, "source_prep": True, "stage1_compile": False,
        "stage2_compile": False, "stage3_compile": False, "timed_speech": False,
        "vlm_semantic_evidence": True,
    }
    if capabilities != expected_capabilities:
        raise SemanticRunAuthorityError("semantic authority capabilities are widened or incomplete")
    vlm = _closed_object(document["vlm"], _VLM_FIELDS, "vlm")
    parameters = _closed_object(vlm["request_parameters"], _REQUEST_FIELDS, "request_parameters")
    parse_policy_raw = vlm["parse_policy"]
    if type(parse_policy_raw) is not dict:  # noqa: E721
        raise SemanticRunAuthorityError("VLM parse policy is invalid")
    parse_policy = cast(dict[str, object], parse_policy_raw)
    if frozenset(parse_policy) != _PARSE_POLICY_FIELDS:
        raise SemanticRunAuthorityError("VLM parse policy does not match its closed schema")
    try:
        policy = DoubaoVlmRequestPolicy(
            model_id=cast(str, vlm["model_id"]),
            provider_id=cast(str, vlm["provider_id"]),
            adapter_strategy_version=cast(str, vlm["adapter_strategy_version"]),
            prompt_version=cast(str, vlm["prompt_version"]),
            max_output_tokens=cast(int, parameters["max_output_tokens"]),
            temperature=cast(int | float, parameters["temperature"]),
            video_fps=cast(int | float, parameters["video_fps"]),
            parse_policy=VlmParsePolicy(
                max_response_bytes=_required_int(parse_policy, "max_response_bytes"),
                max_entities=_required_int(parse_policy, "max_entities"),
                max_facts=_required_int(parse_policy, "max_facts"),
                max_events=_required_int(parse_policy, "max_events"),
                max_candidate_hypotheses=_required_int(parse_policy, "max_candidate_hypotheses"),
                max_temporal_segments=_required_int(parse_policy, "max_temporal_segments"),
                max_measurements=_required_int(parse_policy, "max_measurements"),
                max_text_characters=_required_int(parse_policy, "max_text_characters"),
                max_total_text_characters=_required_int(parse_policy, "max_total_text_characters"),
            ),
            parser_strategy_version=cast(str, vlm["parser_strategy_version"]),
            stage_strategy_version=cast(str, vlm["stage_strategy_version"]),
        )
    except (TypeError, ValueError) as error:
        raise SemanticRunAuthorityError("semantic authority VLM policy is invalid") from error
    expected_hashes = {
        "prompt_template_sha256": vlm_prompt_template_sha256(),
        "response_schema_sha256": _text_sha256(policy.response_schema_json),
        "parser_contract_sha256": vlm_parser_contract_sha256(),
        "window_sampling_policy_sha256": identity_window_sampling_policy_sha256(),
    }
    if any(vlm[key] != value for key, value in expected_hashes.items()):
        raise SemanticRunAuthorityError("semantic authority VLM implementation binding differs")
    retry = _closed_object(document["retry_policy"], _RETRY_FIELDS, "retry_policy")
    backoff = retry["backoff_seconds"]
    if type(backoff) is not list:  # noqa: E721
        raise SemanticRunAuthorityError("semantic authority retry policy is invalid")
    backoff_items = cast(list[object], backoff)
    if any(type(value) is not int for value in backoff_items):  # noqa: E721
        raise SemanticRunAuthorityError("semantic authority retry policy is invalid")
    try:
        retry_policy = GenerationRetryPolicy(
            cast(str, retry["strategy_version"]), cast(int, retry["max_attempts"]),
            tuple(cast(int, value) for value in backoff_items),
        )
    except (TypeError, ValueError) as error:
        raise SemanticRunAuthorityError("semantic authority retry policy is invalid") from error
    return SemanticRunAuthority(policy, retry_policy)


def load_installed_semantic_run_authority() -> SemanticRunAuthority:
    """Load only the package-owned semantic resource; no environment override exists."""
    try:
        root = resources.files("auto_cut_bot.pipeline.runtime").joinpath("_authority")
        with root.joinpath(_DIGEST_NAME).open("rb") as stream:
            digest = stream.read(73)
        with root.joinpath(_RESOURCE_NAME).open("rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
    except (ModuleNotFoundError, OSError):
        raise SemanticRunAuthorityError("installed semantic authority is unavailable") from None
    if len(digest) != 72 or not digest.endswith(b"\n") or len(raw) > 1024 * 1024:
        raise SemanticRunAuthorityError("installed semantic authority framing is invalid")
    try:
        expected_sha256 = digest[:-1].decode("ascii")
    except UnicodeError:
        raise SemanticRunAuthorityError("installed semantic authority digest is invalid") from None
    return decode_semantic_run_authority(raw, expected_sha256=expected_sha256)


__all__ = (
    "SemanticRunAuthority", "SemanticRunAuthorityError", "decode_semantic_run_authority",
    "load_installed_semantic_run_authority",
)
