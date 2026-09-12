"""Private, request-local aliases for an optional VLM object selection.

The target identities deliberately never appear in provider-visible schemas or
prompts.  An alias map is immutable so replay always resolves against the map
that belonged to the original request, rather than a later catalog order.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, cast

from ..media.types import canonical_sha256

_ALIAS = re.compile(r"[a-z]{1,2}\Z")


class ObservationAliasError(ValueError):
    """A program-owned selection directory is not safe to expose."""


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value.strip():  # noqa: E721
        raise ObservationAliasError(f"{field} must be non-empty text")
    try:
        value.encode("utf-8", "strict")
    except UnicodeEncodeError as error:
        raise ObservationAliasError(f"{field} must be valid UTF-8") from error
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ObservationAliasError(f"{field} must not contain surrogate code points")
    return value


@dataclass(frozen=True, slots=True)
class ObservationAliasMap:
    """Frozen one-to-one alias-to-existing-target mapping for one request scope."""

    scope: str
    aliases: Mapping[str, str]

    def __post_init__(self) -> None:
        scope = _text(self.scope, "scope")
        if not isinstance(cast(object, self.aliases), Mapping):
            raise ObservationAliasError("aliases must be a mapping")
        frozen: dict[str, str] = {}
        for alias, target in cast(Mapping[object, object], self.aliases).items():
            if type(alias) is not str or _ALIAS.fullmatch(alias) is None:  # noqa: E721
                raise ObservationAliasError("aliases must use one or two lowercase letters")
            if alias in frozen:
                raise ObservationAliasError("aliases must not repeat an alias")
            frozen[alias] = _text(target, f"aliases[{alias!r}]")
        if not frozen:
            raise ObservationAliasError("aliases must not be empty")
        if len(set(frozen.values())) != len(frozen):
            raise ObservationAliasError("aliases must be a one-to-one mapping")
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "aliases", MappingProxyType(frozen))

    def resolve(self, alias: str) -> str | None:
        """Resolve only a member of this frozen map; unknown aliases stay unknown."""
        return self.aliases.get(alias)

    def to_mapping(self) -> dict[str, object]:
        """Private envelope representation; callers must not send it to providers."""
        return {"scope": self.scope, "aliases": dict(sorted(self.aliases.items()))}

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())
