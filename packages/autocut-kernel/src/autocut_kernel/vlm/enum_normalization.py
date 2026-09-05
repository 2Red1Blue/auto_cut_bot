"""Bounded, auditable normalization of three explicitly unordered V4 sets."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from importlib import resources
from typing import cast

from .models import VlmCandidateTag, VlmEditingMode, VlmNarrativeFunction, VlmParsePolicy
from .parser import VlmResponseIndeterminate, VlmResponseRejected, _constant, _pairs_object
from .semantic_parser_v4 import _validate_json_value

VLM_ENUM_NORMALIZER = "vlm-enum-set-order-v1"
_SETS = {
    "editing_modes": tuple(item.value for item in VlmEditingMode),
    "narrative_functions": tuple(item.value for item in VlmNarrativeFunction),
    "tags": tuple(item.value for item in VlmCandidateTag),
}


def _hash(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def normalizer_contract_sha256() -> str:
    with resources.files("autocut_kernel").joinpath("vlm/enum_normalization.py").open("rb") as stream:
        raw = stream.read(4 * 1024 * 1024 + 1)
    if not 0 < len(raw) <= 4 * 1024 * 1024:
        raise ValueError("normalizer implementation source size is invalid")
    return _hash(raw)


@dataclass(frozen=True, slots=True)
class EnumSetTransformation:
    path: str
    before: tuple[str, ...]
    after: tuple[str, ...]

    def to_mapping(self) -> dict[str, object]:
        return {"path": self.path, "before": list(self.before), "after": list(self.after)}


@dataclass(frozen=True, slots=True)
class NormalizedVlmResponse:
    raw_response_sha256: str
    normalized_response: bytes
    transformations: tuple[EnumSetTransformation, ...]

    def to_mapping(self) -> dict[str, object]:
        return {
            "strategy_version": VLM_ENUM_NORMALIZER,
            "implementation_sha256": normalizer_contract_sha256(),
            "raw_response_sha256": self.raw_response_sha256,
            "normalized_response_sha256": _hash(self.normalized_response),
            "normalized_response": json.loads(self.normalized_response),
            "transformations": [item.to_mapping() for item in self.transformations],
        }


def normalize_vlm_enum_sets(raw: bytes, policy: VlmParsePolicy) -> NormalizedVlmResponse:
    """Never deduplicate, repair references, truncate text, or mutate provider bytes."""
    if type(raw) is not bytes or type(policy) is not VlmParsePolicy:
        raise VlmResponseRejected("INVALID_RAW_RESPONSE", "normalizer requires exact bytes and policy")
    if len(raw) > policy.max_response_bytes:
        raise VlmResponseIndeterminate("RESPONSE_BUDGET_EXCEEDED", "raw response exceeds frozen budget")
    try:
        payload = json.loads(raw.decode("utf-8", "strict"), object_pairs_hook=_pairs_object,
                             parse_float=Decimal, parse_constant=_constant)
        _validate_json_value(payload)
    except VlmResponseRejected:
        raise
    except (ValueError, UnicodeError, RecursionError) as error:
        raise VlmResponseRejected("INVALID_JSON", "normalizer requires bounded strict integer JSON") from error
    if type(payload) is not dict or type(payload.get("schema_version")) is not int or payload["schema_version"] != 4:
        raise VlmResponseRejected("UNSUPPORTED_SCHEMA_VERSION", "normalizer requires schema_version 4")
    candidates = payload.get("candidate_hypotheses")
    if type(candidates) is not list:
        raise VlmResponseRejected("INVALID_RESPONSE_SCHEMA", "candidate_hypotheses must be an array")
    if len(candidates) > policy.max_candidate_hypotheses:
        raise VlmResponseIndeterminate("OBJECT_BUDGET_EXCEEDED", "candidate_hypotheses exceeds frozen budget")
    transformations: list[EnumSetTransformation] = []
    for index, candidate in enumerate(candidates):
        if type(candidate) is not dict:
            raise VlmResponseRejected("INVALID_RESPONSE_SCHEMA", f"candidate_hypotheses[{index}] must be an object")
        for field, allowed in _SETS.items():
            path = f"$.candidate_hypotheses[{index}].{field}"
            values = candidate.get(field)
            if type(values) is not list or not values:
                raise VlmResponseRejected("NONCANONICAL_ENUM_SET", f"{path} must be a non-empty array")
            if any(type(value) is not str or value not in allowed for value in values):
                raise VlmResponseRejected("UNKNOWN_ENUM_VALUE", f"{path} contains an unregistered value")
            before = tuple(cast(list[str], values))
            if len(set(before)) != len(before):
                raise VlmResponseRejected("NONCANONICAL_ENUM_SET", f"{path} must contain unique values")
            after = tuple(value for value in allowed if value in before)
            if before != after:
                transformations.append(EnumSetTransformation(path, before, after))
                candidate[field] = list(after)
    normalized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(normalized) > policy.max_response_bytes:
        raise VlmResponseIndeterminate("RESPONSE_BUDGET_EXCEEDED", "normalized response exceeds frozen budget")
    return NormalizedVlmResponse(_hash(raw), normalized, tuple(transformations))
