"""Pure, fail-closed contract for rich VLM observations.

This module intentionally has no dependency on V4 semantic packs, provider
transport, persistence, authority, or physical-media claims.  A decoded report
is a model statement, not evidence or a verified fact.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import cast

from ..media.types import canonical_sha256
from .observation_aliases import ObservationAliasMap

OBSERVATION_DECODER_IMPLEMENTATION = "vlm-observation-decoder-v1"


def _decoder_implementation_identity_from_sources(sources: Mapping[str, bytes]) -> dict[str, object]:
    """Build the registered identity from an explicit, closed source inventory."""
    source_names = ("observation_contract.py", "observation_aliases.py")
    if set(sources) != set(source_names):
        raise _error("decoder implementation source inventory is not closed")
    return {
        "kind": "VlmObservationDecoderImplementation/v1",
        "implementation": OBSERVATION_DECODER_IMPLEMENTATION,
        "sources": [
            {"path": name, "sha256": f"sha256:{sha256(sources[name]).hexdigest()}"}
            for name in source_names
        ],
    }


def observation_decoder_implementation_identity() -> dict[str, object]:
    """Hash the isolated decoder sources, independently of all V4 identities."""
    directory = Path(__file__).resolve().parent
    return _decoder_implementation_identity_from_sources({
        name: (directory / name).read_bytes()
        for name in ("observation_contract.py", "observation_aliases.py")
    })


OBSERVATION_DECODER_IMPLEMENTATION_SHA256 = canonical_sha256(observation_decoder_implementation_identity())


class ObservationContractError(ValueError):
    """Raw output cannot be accepted as this observation contract."""


def _error(detail: str) -> ObservationContractError:
    return ObservationContractError(f"VLM_OBSERVATION_INVALID: {detail}")


def _text(value: object, field: str, *, allow_empty: bool = False) -> str:
    if type(value) is not str:  # noqa: E721
        raise _error(f"{field} must be a string")
    if not allow_empty and not value.strip():
        raise _error(f"{field} must be non-empty text")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise _error(f"{field} contains an invalid Unicode surrogate")
    return value


@dataclass(frozen=True, slots=True)
class ObservationLimits:
    """Explicit local resource budget; it is not a model-visible business schema."""

    max_response_bytes: int
    max_total_text_chars: int
    max_text_chars: int
    max_moments: int
    max_interpretations: int
    max_uncertainties: int
    max_json_depth: int = 16

    def __post_init__(self) -> None:
        for field in (
            "max_response_bytes", "max_total_text_chars", "max_text_chars", "max_moments",
            "max_interpretations", "max_uncertainties", "max_json_depth",
        ):
            value = getattr(self, field)
            if type(value) is not int or value <= 0:  # noqa: E721
                raise _error(f"limits.{field} must be a positive integer")
        if self.max_text_chars > self.max_total_text_chars:
            raise _error("limits.max_text_chars must not exceed max_total_text_chars")

    def to_mapping(self) -> dict[str, object]:
        return {
            "max_response_bytes": self.max_response_bytes,
            "max_total_text_chars": self.max_total_text_chars,
            "max_text_chars": self.max_text_chars,
            "max_moments": self.max_moments,
            "max_interpretations": self.max_interpretations,
            "max_uncertainties": self.max_uncertainties,
            "max_json_depth": self.max_json_depth,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class ObservationMoment:
    description: str
    time_hint: str | None
    raw_time_hint: object = None
    time_hint_issue: str | None = None

    def __post_init__(self) -> None:
        _text(self.description, "moment.description")
        if self.time_hint is not None:
            _text(self.time_hint, "moment.time_hint", allow_empty=True)
        if self.time_hint_issue is not None:
            _text(self.time_hint_issue, "moment.time_hint_issue")
        object.__setattr__(self, "raw_time_hint", _freeze_json(self.raw_time_hint))

    def to_mapping(self) -> dict[str, object]:
        return {"description": self.description, "time_hint": self.time_hint}

    def to_diagnostic_mapping(self) -> dict[str, object]:
        return {
            **self.to_mapping(),
            "raw_time_hint": _thaw_json(self.raw_time_hint),
            "time_hint_issue": self.time_hint_issue,
        }


@dataclass(frozen=True, slots=True)
class ObservationSelection:
    """Result of an optional selection without promoting it to evidence."""

    status: str
    raw_selected_id: object | None
    resolved_target: str | None
    reason: str | None

    def __post_init__(self) -> None:
        if self.status not in {"selected", "no_selection", "unresolved"}:
            raise _error("selection.status is invalid")
        object.__setattr__(self, "raw_selected_id", _freeze_json(self.raw_selected_id))
        if self.resolved_target is not None:
            _text(self.resolved_target, "selection.resolved_target")
        if self.reason is not None:
            _text(self.reason, "selection.reason", allow_empty=True)
        if self.status == "selected" and (
            type(self.raw_selected_id) is not str or self.resolved_target is None  # noqa: E721
        ):
            raise _error("selected selection must retain its original resolved alias")
        if self.status == "no_selection" and (self.raw_selected_id is not None or self.resolved_target is not None):
            raise _error("no_selection must not resolve a target")
        if self.status == "unresolved" and (self.raw_selected_id is None or self.resolved_target is not None):
            raise _error("unresolved selection must retain only its raw alias")

    def to_mapping(self) -> dict[str, object]:
        """Provider-safe result: target identity intentionally remains private."""
        return {
            "status": self.status,
            "raw_selected_id": _thaw_json(self.raw_selected_id),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ObservationReport:
    description: str
    moments: tuple[ObservationMoment, ...]
    interpretations: tuple[str, ...]
    uncertainties: tuple[str, ...]
    selection: ObservationSelection | None = None

    def __post_init__(self) -> None:
        _text(self.description, "description")
        if type(self.moments) is not tuple:  # noqa: E721
            raise _error("moments must be an immutable tuple")
        if type(self.interpretations) is not tuple:  # noqa: E721
            raise _error("interpretations must be an immutable tuple")
        if type(self.uncertainties) is not tuple:  # noqa: E721
            raise _error("uncertainties must be an immutable tuple")
        if any(type(item) is not ObservationMoment for item in self.moments):  # noqa: E721
            raise _error("moments must contain exact ObservationMoment values")
        for field in ("interpretations", "uncertainties"):
            values = getattr(self, field)
            for index, item in enumerate(values):
                _text(item, f"{field}[{index}]")
        if self.selection is not None and type(self.selection) is not ObservationSelection:  # noqa: E721
            raise _error("selection must be an exact ObservationSelection")

    def to_mapping(self) -> dict[str, object]:
        """The base model report has exactly four fields; selection is separate state."""
        return {
            "description": self.description,
            "moments": [moment.to_mapping() for moment in self.moments],
            "interpretations": list(self.interpretations),
            "uncertainties": list(self.uncertainties),
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    def to_diagnostic_mapping(self) -> dict[str, object]:
        """Private normalized diagnostics; raw response bytes remain authoritative."""
        result = self.to_mapping()
        result["moments"] = [moment.to_diagnostic_mapping() for moment in self.moments]
        if self.selection is not None:
            result["selection"] = self.selection.to_mapping()
        return result


def observation_response_schema(alias_map: ObservationAliasMap | None = None) -> dict[str, object]:
    """Return the provider-visible shallow response schema.

    The dynamic enum is solely membership validation for a request-local,
    program-assigned alias.  It does not encode a business category.
    """
    properties: dict[str, object] = {
        "description": {"type": "string", "minLength": 1},
        "moments": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["description", "time_hint"],
                "properties": {
                    "description": {"type": "string", "minLength": 1},
                    "time_hint": {"type": ["string", "null"]},
                },
            },
        },
        "interpretations": {"type": "array", "items": {"type": "string"}},
        "uncertainties": {"type": "array", "items": {"type": "string"}},
    }
    required = ["description", "moments", "interpretations", "uncertainties"]
    if alias_map is not None:
        properties["selected_id"] = {"enum": [*sorted(alias_map.aliases), None]}
        properties["selection_reason"] = {"type": ["string", "null"]}
        required.extend(("selected_id", "selection_reason"))
    return {"type": "object", "additionalProperties": False, "required": required, "properties": properties}


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _error(f"JSON contains duplicate key {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise _error(f"JSON non-finite number {value!r} is forbidden")


def _validate_json_tree(value: object, limits: ObservationLimits, depth: int = 1) -> None:
    if depth > limits.max_json_depth:
        raise _error("JSON exceeds maximum nesting depth")
    if type(value) is str:  # noqa: E721
        _text(value, "JSON string", allow_empty=True)
    elif type(value) in (int, float):
        if type(value) is float and not math.isfinite(value):  # noqa: E721
            raise _error("JSON contains non-finite number")
    elif type(value) is list:
        for child in cast(list[object], value):
            _validate_json_tree(child, limits, depth + 1)
    elif type(value) is dict:
        for key, child in cast(dict[str, object], value).items():
            _text(key, "JSON object key", allow_empty=True)
            _validate_json_tree(child, limits, depth + 1)


def _freeze_json(value: object) -> object:
    """Keep decoder diagnostics immutable without interpreting their content."""
    if type(value) is list:  # noqa: E721
        return tuple(_freeze_json(item) for item in cast(list[object], value))
    if type(value) is dict:  # noqa: E721
        return MappingProxyType({key: _freeze_json(item) for key, item in cast(dict[str, object], value).items()})
    return value


def _thaw_json(value: object) -> object:
    """Return a JSON-serializable copy of immutable diagnostic raw values."""
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in cast(Mapping[str, object], value).items()}
    if type(value) is tuple:  # noqa: E721
        return [_thaw_json(item) for item in cast(tuple[object, ...], value)]
    return value


def _closed_object(value: object, fields: set[str], field: str) -> dict[str, object]:
    if type(value) is not dict or set(cast(dict[str, object], value)) != fields:  # noqa: E721
        raise _error(f"{field} must be a closed object with the required fields")
    return cast(dict[str, object], value)


def _texts(value: object, field: str, maximum: int, limits: ObservationLimits) -> tuple[str, ...]:
    if type(value) is not list:  # noqa: E721
        raise _error(f"{field} must be an array")
    items = cast(list[object], value)
    if len(items) > maximum:
        raise _error(f"{field} exceeds its resource limit")
    return tuple(_text(item, f"{field}[{index}]") for index, item in enumerate(items))


def _selection(mapping: dict[str, object], alias_map: ObservationAliasMap | None) -> ObservationSelection | None:
    if alias_map is None:
        return None
    selected = mapping["selected_id"]
    reason_value = mapping["selection_reason"]
    reason = reason_value if type(reason_value) is str else None  # noqa: E721
    if selected is None:
        return ObservationSelection("no_selection", None, None, reason)
    if type(selected) is not str:  # noqa: E721
        return ObservationSelection("unresolved", selected, None, reason)
    raw = selected
    target = alias_map.resolve(raw)
    if target is None:
        return ObservationSelection("unresolved", raw, None, reason)
    return ObservationSelection("selected", raw, target, reason)


def decode_observation_report(
    raw: bytes, limits: ObservationLimits, alias_map: ObservationAliasMap | None = None,
) -> ObservationReport:
    """Decode one complete raw response without repairing its semantic content.

    A malformed optional ``time_hint`` becomes ``None`` only after its moment's
    description and the report's required structure have passed validation.
    Unknown aliases similarly retain a useful report with an unresolved
    selection; no current catalog is consulted.
    """
    if type(raw) is not bytes:  # noqa: E721
        raise _error("raw response must be bytes")
    if type(limits) is not ObservationLimits:  # noqa: E721
        raise _error("limits must be an exact ObservationLimits")
    if alias_map is not None and type(alias_map) is not ObservationAliasMap:  # noqa: E721
        raise _error("alias_map must be an exact ObservationAliasMap")
    if len(raw) > limits.max_response_bytes:
        raise _error("response exceeds byte budget")
    try:
        parsed: object = json.loads(
            raw.decode("utf-8", "strict"), object_pairs_hook=_reject_duplicates, parse_constant=_reject_constant,
        )
    except ObservationContractError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise _error("response must be complete strict UTF-8 JSON") from error
    _validate_json_tree(parsed, limits)
    fields = {"description", "moments", "interpretations", "uncertainties"}
    if alias_map is not None:
        fields.update(("selected_id", "selection_reason"))
    root = _closed_object(parsed, fields, "root")
    description = _text(root["description"], "description")
    moments_value = root["moments"]
    if type(moments_value) is not list:  # noqa: E721
        raise _error("moments must be an array")
    moment_items = cast(list[object], moments_value)
    if len(moment_items) > limits.max_moments:
        raise _error("moments exceeds its resource limit")
    moments: list[ObservationMoment] = []
    for index, item in enumerate(moment_items):
        moment = _closed_object(item, {"description", "time_hint"}, f"moments[{index}]")
        hint = moment["time_hint"]
        moments.append(ObservationMoment(
            _text(moment["description"], f"moments[{index}].description"),
            hint if type(hint) is str else None,
            raw_time_hint=hint,
            time_hint_issue=None if hint is None or type(hint) is str else "optional_time_hint_not_string",
        ))
    interpretations = _texts(root["interpretations"], "interpretations", limits.max_interpretations, limits)
    uncertainties = _texts(root["uncertainties"], "uncertainties", limits.max_uncertainties, limits)
    all_text = [description, *(moment.description for moment in moments), *(moment.time_hint or "" for moment in moments), *interpretations, *uncertainties]
    if alias_map is not None and root["selection_reason"] is not None:
        all_text.append(_text(root["selection_reason"], "selection_reason", allow_empty=True))
    if any(len(text) > limits.max_text_chars for text in all_text):
        raise _error("a text field exceeds its resource limit")
    if sum(len(text) for text in all_text) > limits.max_total_text_chars:
        raise _error("report exceeds its total text resource limit")
    return ObservationReport(description, tuple(moments), interpretations, uncertainties, _selection(root, alias_map))


__all__ = [
    "OBSERVATION_DECODER_IMPLEMENTATION", "OBSERVATION_DECODER_IMPLEMENTATION_SHA256",
    "ObservationContractError", "ObservationLimits", "ObservationMoment", "ObservationReport", "ObservationSelection",
    "decode_observation_report", "observation_decoder_implementation_identity", "observation_response_schema",
]
