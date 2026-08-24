"""Canonical local detector identities shared by source prep and media preflight."""

from __future__ import annotations

from collections.abc import Sequence

from autocut_kernel.media.types import canonical_sha256, sha256_prefixed


def local_detector_identity_sha256(
    *,
    producer_kind: str,
    producer_generation_policy_sha256: str,
    tools: Sequence[tuple[str, str, str]],
    model_sha256: str | None = None,
) -> str:
    """Bind one producer to exact executable bytes/version evidence and optional model."""

    if not producer_kind or producer_kind != producer_kind.strip():
        raise ValueError("producer_kind must be canonical text")
    sha256_prefixed(
        producer_generation_policy_sha256,
        "producer_generation_policy_sha256",
    )
    normalized: list[dict[str, str]] = []
    for name, executable_sha256, version_evidence_sha256 in tools:
        if not name or name != name.strip():
            raise ValueError("detector tool name must be canonical text")
        sha256_prefixed(executable_sha256, "executable_sha256")
        sha256_prefixed(version_evidence_sha256, "version_evidence_sha256")
        normalized.append(
            {
                "executable_sha256": executable_sha256,
                "name": name,
                "version_evidence_sha256": version_evidence_sha256,
            }
        )
    if not normalized:
        raise ValueError("detector identity requires at least one tool")
    payload: dict[str, object] = {
        "producer_generation_policy_sha256": producer_generation_policy_sha256,
        "producer_kind": producer_kind,
        "schema_version": "local-media-detector-identity-v1",
        "tools": normalized,
    }
    if model_sha256 is not None:
        sha256_prefixed(model_sha256, "model_sha256")
        payload["model_sha256"] = model_sha256
    return canonical_sha256(payload)


__all__ = ["local_detector_identity_sha256"]
