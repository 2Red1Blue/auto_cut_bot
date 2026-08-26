"""Identity of the supported timed-speech wire-schema closure, not authority.

Only plain local ``#/$defs/<name>`` references are supported. This conservative
projection is not a general JSON Schema resolver or a semantic decoder hash.
Names match ``[A-Za-z_][A-Za-z0-9_-]*`` exactly; JSON Pointer escapes are excluded.
"""

from __future__ import annotations

import re
from typing import cast

from ..contracts.compiler.canonical import canonical_json_hash, load_canonical_json_bytes
from ..contracts.compiler.errors import CanonicalizationError

_DIALECT = "https://json-schema.org/draft/2020-12/schema"
_ROOT_NAME = "timed_speech_registry_entry"
_ROOT_POINTER = "#/$defs/" + _ROOT_NAME
_REFERENCE = re.compile(r"#/\$defs/([A-Za-z_][A-Za-z0-9_-]*)\Z")
_REFERENCE_MECHANISMS = frozenset({
    "$anchor", "$dynamicAnchor", "$dynamicRef", "$recursiveAnchor", "$recursiveRef",
})


class TimedSpeechContractError(ValueError):
    """The schema cannot be represented by the fixed contract projection."""


def _references(schema: object) -> set[str]:
    references: set[str] = set()
    pending = [schema]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            mapping = cast(dict[str, object], value)
            if mapping.keys() & (_REFERENCE_MECHANISMS | {"$id", "$schema"}):
                raise TimedSpeechContractError("reachable schema changes reference scope or uses unsupported anchors")
            if "$ref" in mapping:
                reference = mapping["$ref"]
                match = _REFERENCE.fullmatch(reference) if isinstance(reference, str) else None
                if match is None:
                    raise TimedSpeechContractError("only exact #/$defs/<plain-name> references are supported")
                references.add(match.group(1))
            pending.extend(mapping[key] for key in sorted(mapping, reverse=True))
        elif isinstance(value, list):
            pending.extend(reversed(cast(list[object], value)))
    return references


def timed_speech_registry_contract_sha256(raw: bytes) -> str:
    """Hash the exact root definition and its transitively reachable definitions.

    Source provenance belongs to the caller's locked-source boundary. Formatting,
    key order and unrelated definitions are excluded; reachable content is not.
    """
    if type(raw) is not bytes:  # noqa: E721
        raise TimedSpeechContractError("schema source must be exact bytes")
    try:
        value, _ = load_canonical_json_bytes(raw, origin="timed-speech contract schema")
    except (CanonicalizationError, RecursionError) as error:
        raise TimedSpeechContractError("schema source must be strict canonical-compatible JSON") from error
    if not isinstance(value, dict):
        raise TimedSpeechContractError("schema root must be an object")
    source = cast(dict[str, object], value)
    if source.get("$schema") != _DIALECT or source.keys() & _REFERENCE_MECHANISMS:
        raise TimedSpeechContractError("schema must use Draft 2020-12 without unsupported reference mechanisms")
    properties, definitions = source.get("properties"), source.get("$defs")
    if not isinstance(properties, dict) or not isinstance(definitions, dict):
        raise TimedSpeechContractError("schema requires properties and $defs objects")
    if cast(dict[str, object], properties).get(_ROOT_NAME) != {"$ref": _ROOT_POINTER}:
        raise TimedSpeechContractError("timed-speech entry must have the exact root property reference")
    available = cast(dict[str, object], definitions)
    selected: dict[str, object] = {}
    pending = [_ROOT_NAME]
    while pending:
        name = pending.pop()
        if name in selected:
            continue
        if name not in available:
            raise TimedSpeechContractError(f"missing referenced definition: {name}")
        schema = available[name]
        if not isinstance(schema, dict) and type(schema) is not bool:  # noqa: E721
            raise TimedSpeechContractError(f"definition must be a schema object or boolean: {name}")
        schema = cast(dict[str, object] | bool, schema)
        selected[name] = schema
        pending.extend(sorted(_references(schema), reverse=True))
    return canonical_json_hash({
        "schema_version": "timed-speech-registry-contract-projection-v1",
        "schema_dialect": _DIALECT,
        "root_pointer": _ROOT_POINTER,
        "definitions": selected,
    })
