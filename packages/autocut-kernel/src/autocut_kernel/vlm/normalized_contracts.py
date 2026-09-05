"""Additive parser registry; the legacy source bundles remain byte-for-byte intact."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from importlib import resources

from ..media.types import canonical_sha256
from . import semantic_contracts as legacy
from .enum_normalization import VLM_ENUM_NORMALIZER, normalize_vlm_enum_sets, normalizer_contract_sha256
from .models import VlmParsePolicy, VlmRequestIdentity
from .semantic_contracts import SemanticPackValue, VLM_PARSER_V3, VLM_PARSER_V4
from .semantic_parser_v4 import parse_vlm_response_v4
from .window import WindowManifest, WindowManifestSet

VLM_PARSER_NORMALIZED_V4 = "normalized-semantic-pack-v4-v1"
V4_PARSERS = frozenset({VLM_PARSER_V4, VLM_PARSER_NORMALIZED_V4})
REGISTERED_VLM_PARSERS = legacy.REGISTERED_VLM_PARSERS | {VLM_PARSER_NORMALIZED_V4}


class ParserImplementationUnavailableError(ValueError):
    code = "PARSER_IMPLEMENTATION_UNAVAILABLE"


def parser_contract_sha256_for(strategy_version: str) -> str:
    if strategy_version in legacy.REGISTERED_VLM_PARSERS:
        return legacy.parser_contract_sha256_for(strategy_version)
    if strategy_version != VLM_PARSER_NORMALIZED_V4:
        raise ParserImplementationUnavailableError("PARSER_IMPLEMENTATION_UNAVAILABLE: unknown parser strategy")
    try:
        with resources.files("autocut_kernel").joinpath("vlm/normalized_contracts.py").open("rb") as stream:
            raw = stream.read(4 * 1024 * 1024 + 1)
        if not 0 < len(raw) <= 4 * 1024 * 1024:
            raise ValueError("parser dispatch source size is invalid")
        return canonical_sha256({
            "strategy_version": strategy_version, "schema_version": 4,
            "normalizer_strategy": VLM_ENUM_NORMALIZER,
            "normalizer_sha256": normalizer_contract_sha256(),
            "decoder_projection_sha256": legacy.parser_contract_sha256_for(VLM_PARSER_V4),
            "dispatch_sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
        })
    except (OSError, ModuleNotFoundError, ValueError) as error:
        raise ParserImplementationUnavailableError("PARSER_IMPLEMENTATION_UNAVAILABLE: parser bundle is unavailable") from error


def require_parser_contract(strategy_version: str, frozen_sha256: str | None) -> None:
    if strategy_version == VLM_PARSER_V3:
        legacy.require_parser_contract(strategy_version, frozen_sha256)
        return
    if strategy_version not in V4_PARSERS or frozen_sha256 != parser_contract_sha256_for(strategy_version):
        raise ParserImplementationUnavailableError(
            "PARSER_IMPLEMENTATION_UNAVAILABLE: frozen parser implementation is not installed"
        )


def parse_registered_vlm_response(
    raw_response: bytes, *, parser_strategy_version: str,
    manifest: WindowManifest, manifest_set: WindowManifestSet,
    request_identity: VlmRequestIdentity, policy: VlmParsePolicy,
    parser_contract_sha256: str | None = None,
) -> SemanticPackValue:
    require_parser_contract(parser_strategy_version, parser_contract_sha256)
    if parser_strategy_version != VLM_PARSER_NORMALIZED_V4:
        return legacy.parse_registered_vlm_response(
            raw_response, parser_strategy_version=parser_strategy_version,
            manifest=manifest, manifest_set=manifest_set, request_identity=request_identity,
            policy=policy, parser_contract_sha256=parser_contract_sha256,
        )
    normalized = normalize_vlm_enum_sets(raw_response, policy)
    pack = parse_vlm_response_v4(
        normalized.normalized_response, manifest=manifest, manifest_set=manifest_set,
        request_identity=request_identity, policy=policy,
    )
    # Provenance always names the actual provider bytes, never the derived serialization.
    return replace(pack, raw_response_sha256=normalized.raw_response_sha256)
