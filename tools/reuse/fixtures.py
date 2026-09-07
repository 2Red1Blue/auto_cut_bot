"""Fixture manifest loading and verification (v1 compat read + FixtureManifest/v2).

v1 manifests are the existing YAML files under docs/llm-stage-contracts/fixtures/;
v2 is the closed JSON contract from doc 13 §9.2. Private media roots are mapped
via --media-root and never enter content identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools.reuse.models import (
    ErrorCode,
    ExperimentError,
    file_sha256,
    load_json_strict,
)

# label_kind values from doc 13 §10; only video_verified labels feed primary scores.
LABEL_KINDS = ("video_verified", "external_script_reference", "api_reference", "pipeline_baseline")
ALIGNMENT_STATUSES = ("verified", "unverified", "not_applicable")

_V2_KEYS = {
    "schema",
    "set_id",
    "frozen",
    "label_kind",
    "alignment_status",
    "inputs",
    "labels_ref",
    "allowed_context_refs",
}
_V2_INPUT_KEYS = {"path", "sha256", "size_bytes", "episode"}


@dataclass(frozen=True)
class FixtureInput:
    path: str  # repo/fixture-set relative private path; resolved via media root only
    sha256: str
    size_bytes: int | None
    episode: int | None


@dataclass(frozen=True)
class FixtureManifest:
    set_id: str
    frozen: bool
    label_kind: str
    alignment_status: str
    inputs: tuple[FixtureInput, ...]
    labels_ref: str | None
    allowed_context_refs: tuple[str, ...]
    source_schema: str  # "v1-compat" | "FixtureManifest/v2"
    raw_ref: dict[str, Any]  # untouched original manifest document

    @property
    def path(self) -> str | None:
        return None


def _load_v1(obj: dict[str, Any]) -> FixtureManifest:
    if obj.get("frozen") is not True:
        pass  # frozen=false stays representable; verification refuses it
    inputs: list[FixtureInput] = []
    for video in obj.get("videos") or []:
        inputs.append(
            FixtureInput(
                path=str(video.get("file", "")),
                sha256=str(video.get("sha256", "")),
                size_bytes=video.get("size_bytes"),
                episode=video.get("episode"),
            )
        )
    return FixtureManifest(
        set_id=str(obj.get("set_id", "")),
        frozen=bool(obj.get("frozen", False)),
        label_kind="external_script_reference",  # v1 provenance: source_script first-class
        alignment_status="unverified",  # v1 scene times use fixed steps; not video-verified
        inputs=tuple(inputs),
        labels_ref=None,
        allowed_context_refs=(),
        source_schema="v1-compat",
        raw_ref=obj,
    )


def _load_v2(obj: dict[str, Any]) -> FixtureManifest:
    unknown = sorted(set(obj) - _V2_KEYS)
    if unknown:
        raise ExperimentError(ErrorCode.FIXTURE_UNVERIFIED, f"manifest: unknown keys {unknown}")
    label_kind = obj.get("label_kind")
    if label_kind not in LABEL_KINDS:
        raise ExperimentError(ErrorCode.FIXTURE_UNVERIFIED, f"manifest.label_kind: {label_kind!r}")
    alignment = obj.get("alignment_status")
    if alignment not in ALIGNMENT_STATUSES:
        raise ExperimentError(
            ErrorCode.FIXTURE_UNVERIFIED, f"manifest.alignment_status: {alignment!r}"
        )
    inputs: list[FixtureInput] = []
    for i, raw in enumerate(obj.get("inputs") or []):
        if not isinstance(raw, dict):
            raise ExperimentError(ErrorCode.FIXTURE_UNVERIFIED, f"manifest.inputs[{i}]: object")
        unknown_in = sorted(set(raw) - _V2_INPUT_KEYS)
        if unknown_in:
            raise ExperimentError(
                ErrorCode.FIXTURE_UNVERIFIED, f"manifest.inputs[{i}]: unknown keys {unknown_in}"
            )
        inputs.append(
            FixtureInput(
                path=str(raw.get("path", "")),
                sha256=str(raw.get("sha256", "")),
                size_bytes=raw.get("size_bytes"),
                episode=raw.get("episode"),
            )
        )
    ctx = obj.get("allowed_context_refs") or []
    if not isinstance(ctx, list) or not all(isinstance(x, str) for x in ctx):
        raise ExperimentError(ErrorCode.FIXTURE_UNVERIFIED, "manifest.allowed_context_refs: strings")
    return FixtureManifest(
        set_id=str(obj.get("set_id", "")),
        frozen=bool(obj.get("frozen", False)),
        label_kind=label_kind,
        alignment_status=alignment,
        inputs=tuple(inputs),
        labels_ref=obj.get("labels_ref"),
        allowed_context_refs=tuple(ctx),
        source_schema="FixtureManifest/v2",
        raw_ref=obj,
    )


def load_fixture_manifest(path: Path) -> FixtureManifest:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ExperimentError(ErrorCode.FIXTURE_UNVERIFIED, f"manifest unreadable: {exc}") from exc
    if path.suffix in (".yaml", ".yml"):
        import yaml

        try:
            obj = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ExperimentError(
                ErrorCode.FIXTURE_UNVERIFIED, f"manifest invalid YAML: {exc}"
            ) from exc
        if not isinstance(obj, dict):
            raise ExperimentError(ErrorCode.FIXTURE_UNVERIFIED, "manifest: expected mapping")
        return _load_v1(obj)
    obj = load_json_strict(text, context="fixture manifest")
    if not isinstance(obj, dict) or obj.get("schema") != "FixtureManifest/v2":
        raise ExperimentError(
            ErrorCode.FIXTURE_UNVERIFIED, "manifest: JSON manifest must be FixtureManifest/v2"
        )
    return _load_v2(obj)


def verify_fixture(
    spec_fixture_sha256: str,
    manifest_path: Path,
    manifest: FixtureManifest,
    media_root: Path,
) -> None:
    """Check manifest identity, frozen status and every input hash under media root.

    The media root only maps private relative paths; it never enters content
    identity, and resolved paths must stay inside it."""
    actual = file_sha256(manifest_path)
    if actual != spec_fixture_sha256:
        raise ExperimentError(
            ErrorCode.FIXTURE_UNVERIFIED,
            f"manifest sha256 {actual} != spec {spec_fixture_sha256}",
        )
    if not manifest.frozen:
        raise ExperimentError(
            ErrorCode.FIXTURE_UNVERIFIED, f"fixture set {manifest.set_id} is not frozen"
        )
    if not manifest.inputs:
        raise ExperimentError(ErrorCode.FIXTURE_UNVERIFIED, "manifest has no inputs")
    root = media_root.resolve()
    if not root.is_dir():
        raise ExperimentError(ErrorCode.FIXTURE_UNVERIFIED, f"media root missing: {root}")
    for item in manifest.inputs:
        if not item.path or item.sha256 == "":
            raise ExperimentError(ErrorCode.FIXTURE_UNVERIFIED, "manifest input missing path/sha256")
        resolved = (root / item.path).resolve()
        if resolved != root and root not in resolved.parents:
            raise ExperimentError(
                ErrorCode.PATH_ESCAPE, f"fixture path escapes media root: {item.path}"
            )
        if not resolved.is_file():
            raise ExperimentError(
                ErrorCode.FIXTURE_UNVERIFIED, f"fixture input missing: {item.path}"
            )
        if file_sha256(resolved) != item.sha256:
            raise ExperimentError(
                ErrorCode.FIXTURE_UNVERIFIED, f"fixture input hash mismatch: {item.path}"
            )
