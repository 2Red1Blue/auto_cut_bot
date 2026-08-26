"""Identity of the fixed installed Semantic Pack v3 parser implementation bundle.

The ordered members are media/root_evidence.py, media/types.py, vlm/models.py,
vlm/parser.py and vlm/window.py. They cover the parser's local implementation
dependencies, including interval mapping and frame-index validation. Provider,
retry and persisted-pack decoding are separate contracts, not this parser path.

Every raw source-byte change, including comments or formatting, changes this
identity. The controlled installed wheel is the trust root: this is neither a
formal semantic proof nor whole-wheel authentication, and does not attest to
stdlib, package initialization, bytecode substitution or runtime monkeypatches.
No source discovery, imports of discovered modules or cached identity are used.
"""

from __future__ import annotations

import hashlib
from importlib import resources
from typing import Final

from ..media.types import canonical_sha256

_SOURCE_PATHS: Final = (
    "media/root_evidence.py",
    "media/types.py",
    "vlm/models.py",
    "vlm/parser.py",
    "vlm/window.py",
)
_MAX_SOURCE_BYTES: Final = 4 * 1024 * 1024


class VlmParserContractError(ValueError):
    """The installed parser bundle is absent, empty, unreadable or oversized."""


def vlm_parser_contract_sha256() -> str:
    """Hash exact bounded installed sources, without caller paths or overrides."""
    sources: list[dict[str, str]] = []
    try:
        root = resources.files("autocut_kernel")
        for path in _SOURCE_PATHS:
            with root.joinpath(*path.split("/")).open("rb") as stream:
                raw = stream.read(_MAX_SOURCE_BYTES + 1)
            if not 0 < len(raw) <= _MAX_SOURCE_BYTES:
                raise VlmParserContractError("installed parser source is empty or exceeds its byte bound")
            sources.append({"path": path, "sha256": "sha256:" + hashlib.sha256(raw).hexdigest()})
    except (OSError, ModuleNotFoundError):
        raise VlmParserContractError("installed parser implementation bundle is unavailable or unreadable") from None
    return canonical_sha256({
        "schema_version": "vlm-parser-implementation-contract-v1",
        "parser_strategy_version": "strict-semantic-pack-v3",
        "sources": sources,
    })
