"""Explicit parser dispatch; an invalid response never selects another parser."""

from __future__ import annotations

import hashlib
from importlib import resources
from typing import TypeAlias

from ..media.types import canonical_sha256
from .models import VlmParsePolicy, VlmRequestIdentity, VlmSemanticPack
from .parser import parse_vlm_response
from .parser_contract import VlmParserContractError, vlm_parser_contract_sha256
from .semantic_pack_v4 import VlmSemanticPackV4
from .window import WindowManifest, WindowManifestSet

VLM_PARSER_V3 = "strict-semantic-pack-v3"
VLM_PARSER_V4 = "strict-semantic-pack-v4"
REGISTERED_VLM_PARSERS = frozenset({VLM_PARSER_V3, VLM_PARSER_V4})
SemanticPackValue: TypeAlias = VlmSemanticPack | VlmSemanticPackV4
_V4_SOURCE_PATHS = (
    "vlm/semantic_support_v4.py", "vlm/semantic_pack_v4.py",
    "vlm/semantic_parser_v4.py", "vlm/semantic_contracts.py",
)
_MAX_SOURCE_BYTES = 4 * 1024 * 1024


def parser_contract_sha256_for(strategy_version: str) -> str:
    """Keep V3's exact existing identity; V4 binds its added source bundle."""
    if strategy_version == VLM_PARSER_V3:
        return vlm_parser_contract_sha256()
    if strategy_version != VLM_PARSER_V4:
        raise ValueError("VLM parser strategy is not registered")
    sources: list[dict[str, str]] = []
    try:
        root = resources.files("autocut_kernel")
        for path in _V4_SOURCE_PATHS:
            with root.joinpath(*path.split("/")).open("rb") as stream:
                raw = stream.read(_MAX_SOURCE_BYTES + 1)
            if not 0 < len(raw) <= _MAX_SOURCE_BYTES:
                raise VlmParserContractError("installed V4 parser source size is invalid")
            sources.append({"path": path, "sha256": "sha256:" + hashlib.sha256(raw).hexdigest()})
    except (OSError, ModuleNotFoundError):
        raise VlmParserContractError("installed V4 parser sources are unavailable") from None
    return canonical_sha256({
        "schema_version": "vlm-parser-implementation-contract-v2",
        "parser_strategy_version": VLM_PARSER_V4,
        "shared_v3_bundle_sha256": vlm_parser_contract_sha256(),
        "sources": sources,
    })


def parse_registered_vlm_response(
    raw_response: bytes,
    *,
    parser_strategy_version: str,
    manifest: WindowManifest,
    manifest_set: WindowManifestSet,
    request_identity: VlmRequestIdentity,
    policy: VlmParsePolicy,
    parser_contract_sha256: str | None = None,
) -> SemanticPackValue:
    require_parser_contract(parser_strategy_version, parser_contract_sha256)
    if parser_strategy_version == VLM_PARSER_V3:
        return parse_vlm_response(
            raw_response, manifest=manifest, manifest_set=manifest_set,
            request_identity=request_identity, policy=policy,
        )
    if parser_strategy_version == VLM_PARSER_V4:
        from .semantic_parser_v4 import parse_vlm_response_v4

        return parse_vlm_response_v4(
            raw_response, manifest=manifest, manifest_set=manifest_set,
            request_identity=request_identity, policy=policy,
        )
    raise ValueError("VLM parser strategy is not registered")


def require_parser_contract(strategy_version: str, frozen_sha256: str | None) -> None:
    """Verify V4's original executable identity, never replace it with current code."""
    if strategy_version == VLM_PARSER_V3:
        if frozen_sha256 is not None:
            raise ValueError("V3 does not accept a new parser_contract_sha256 field")
        return
    if strategy_version != VLM_PARSER_V4:
        raise ValueError("VLM parser strategy is not registered")
    if type(frozen_sha256) is not str or frozen_sha256 != parser_contract_sha256_for(strategy_version):  # noqa: E721
        raise ValueError("V4 frozen parser_contract_sha256 differs from the registered implementation")
