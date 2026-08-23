"""Closed vocabulary for the F0 source-ledger only."""
# pyright: reportUnknownVariableType=none, reportUnknownArgumentType=none

from __future__ import annotations

import re
from pathlib import PurePosixPath

PIN_IDS = frozenset(
    {
        "A",
        "B",
        "C1",
        "C2",
        "C3",
        "C4",
        "C5",
        "D",
        "E",
        "authority",
        "errata.execution",
        "errata.recovery",
    }
)
KERNEL_PIN_OIDS = {
    "A": ("1fd66f6598b950b19349a44113569c04e840a84f", "168b71d9fa9d20e9c2dc1061f80073ac0a0078de"),
    "B": ("eb7e4181f63da308c405bad6d99fcd085cfdd98a", "50f78ea0b7f754eb8f91ef800924b86da25b3083"),
    "C1": ("a915bc8b3ce847c6a521efa8f0631408c294f022", "6aedd94c71bde172799bda9208f3dfc95810c37f"),
    "C2": ("20ade5b515ccd7bbf2a3ea0541992c86c1be456f", "944c0801155319d63558b45daede6b038087d654"),
    "C3": ("c647f4efabe83757918943092880d60c275dc984", "c2211113d9fda2baccb5655e39053f236f7f7e0e"),
    "C4": ("2d882e8cbdfbe8a3bd5462ee82f683abc9fd34b2", "e5cf323d605723c6ab1158447bb5d031b271395d"),
    "C5": ("31e3c1a461395a7f77cb9ed13e8dcd8111f8dac1", "5fe1de75e46d30405ec746f99344b4a537f7f381"),
}
ERRATA_IMPORT = "e2318652cfaed0f99f1ba355de7982f1d34df150"
ERRATA_ATTESTATION = "baf667f797ac7d4eb34e48caad8047fb07433c9c"
F0_PROFILE_ID = "authority_intake_v1"
CONTENT_PIN_COUNTS = {
    "A": 1,
    "B": 10,
    "C1": 1,
    "C2": 1,
    "C3": 1,
    "C4": 1,
    "C5": 1,
    "authority": 10,
    "errata.execution": 7,
    "errata.recovery": 7,
}
B_SOURCE_PATHS = frozenset(
    {
        "packages/autocut-kernel/src/autocut_kernel/contracts/source/2_1_3/common/"
        "contributions/common-system-direct-contribution.manifest.json",
        "packages/autocut-kernel/src/autocut_kernel/contracts/source/2_1_3/common/"
        "schemas/primitives/artifact-ref.schema.json",
        "packages/autocut-kernel/src/autocut_kernel/contracts/source/2_1_3/common/"
        "schemas/primitives/artifact-set-ref.schema.json",
        "packages/autocut-kernel/src/autocut_kernel/contracts/source/2_1_3/common/"
        "schemas/primitives/domain-ref.schema.json",
        "packages/autocut-kernel/src/autocut_kernel/contracts/source/2_1_3/common/"
        "schemas/primitives/immutable-blob-ref.schema.json",
        "packages/autocut-kernel/src/autocut_kernel/contracts/source/2_1_3/common/"
        "schemas/primitives/scope.schema.json",
        "packages/autocut-kernel/src/autocut_kernel/contracts/source/2_1_3/common/"
        "schemas/primitives/source-span-ref.schema.json",
        "packages/autocut-kernel/src/autocut_kernel/contracts/source/2_1_3/common/"
        "schemas/bootstrap/external-job-request.schema.json",
        "packages/autocut-kernel/src/autocut_kernel/contracts/source/2_1_3/common/"
        "schemas/bootstrap/job-start-slot.schema.json",
        "packages/autocut-kernel/src/autocut_kernel/contracts/source/2_1_3/common/"
        "schemas/run/run-manifest.schema.json",
    }
)
SLOT_IDS = frozenset(
    {
        "catalogue_envelope",
        "command_identity",
        "artifact_set",
        "receipt_dispatcher",
        "admission_rule_result",
        "diagnostic_degradation",
        "recovery_ledger",
        "store",
        "bootstrap",
        "components",
        "dual_runtime",
    }
)
SHA256 = re.compile(r"sha256:(?!0{64}$)[0-9a-f]{64}\Z")
OID = re.compile(r"[0-9a-f]{40}\Z")
FORBIDDEN_TEXT = re.compile(
    r"(?:\b(?:tbd|placeholder|guess(?:ed)?)\b|\b(?:cmd|profile|component|rule|strategy)_[a-z0-9_]+)",
    re.IGNORECASE,
)


class IntakeError(ValueError):
    """Raised for any F0 closure or source-integrity denial."""


def require_path(value: str) -> None:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise IntakeError("path must be a contained relative POSIX path")


def require_oid(value: object) -> str:
    if not isinstance(value, str) or not OID.fullmatch(value) or value == "0" * 40:
        raise IntakeError("Git commit OID must be a full 40-character lowercase SHA-1")
    return value


def require_hash(value: object) -> str:
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise IntakeError("raw hash must be a non-zero lower-case sha256 digest")
    return value


def reject_forbidden(value: object, *, key: str = "") -> None:
    if value is None:
        raise IntakeError("null is not permitted in F0")
    if isinstance(value, str) and FORBIDDEN_TEXT.search(value):
        raise IntakeError(f"forbidden non-source or identity-looking text in {key or 'value'}")
    if isinstance(value, dict):
        for child_key, child in value.items():
            if child_key in {
                "status",
                "readiness",
                "ready",
                "registry",
                "generated",
                "runtime",
                "component",
                "identity",
            }:
                raise IntakeError(f"forbidden F0 field {child_key}")
            reject_forbidden(child, key=child_key)
    elif isinstance(value, list):
        for child in value:
            reject_forbidden(child, key=key)
