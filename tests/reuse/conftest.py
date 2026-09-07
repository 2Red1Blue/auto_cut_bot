"""Shared fixtures for the reuse runner tests.

All integration tests run against a hermetic tmp repo layout: projects.json is
copied from the real registry, the fixture manifest and media files are
synthesized, and run.resolve_repo_root is monkeypatched to the tmp root.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.reuse.models import (  # noqa: E402
    ExperimentSpec,
    file_sha256,
    json_sha256,
)


@pytest.fixture()
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture()
def tmp_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Hermetic repo root: tools/reuse/projects.json copied, artifacts writable."""
    (tmp_path / "tools" / "reuse").mkdir(parents=True)
    shutil.copy(REPO_ROOT / "tools" / "reuse" / "projects.json", tmp_path / "tools" / "reuse" / "projects.json")
    (tmp_path / "tools" / "reuse" / "adapters").mkdir()
    (tmp_path / "tools" / "reuse" / "adapters" / "__init__.py").write_text("")
    shutil.copy(
        REPO_ROOT / "tools" / "reuse" / "adapters" / "fake.py",
        tmp_path / "tools" / "reuse" / "adapters" / "fake.py",
    )
    return tmp_path


@pytest.fixture()
def fake_fixture(tmp_repo: Path) -> dict[str, str]:
    """A frozen v2 fixture manifest with two tiny media inputs; returns paths."""
    media_root = tmp_repo / "private-media"
    media_root.mkdir()
    inputs = []
    for episode, name in ((1, "ep01.mp4"), (2, "ep02.mp4")):
        path = media_root / name
        path.write_bytes(f"fake-video-{episode}".encode())
        inputs.append(
            {"path": name, "sha256": file_sha256(path), "size_bytes": path.stat().st_size, "episode": episode}
        )
    manifest = {
        "schema": "FixtureManifest/v2",
        "set_id": "fake-set",
        "frozen": True,
        "label_kind": "video_verified",
        "alignment_status": "verified",
        "inputs": inputs,
        "labels_ref": None,
        "allowed_context_refs": [],
    }
    manifest_path = tmp_repo / "fixtures" / "fake-set" / "manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return {
        "manifest": str(manifest_path),
        "manifest_sha256": file_sha256(manifest_path),
        "media_root": str(media_root),
    }


def make_spec(fixture: dict[str, str], **overrides: object) -> dict[str, object]:
    spec: dict[str, object] = {
        "schema": "ExperimentSpec/v1",
        "experiment_id": "fake-smoke",
        "producer_id": "fake",
        "producer_commit": "builtin:fake",
        "adapter_version": "fake-1",
        "fixture_ref": str(Path(fixture["manifest"]).resolve()),
        "fixture_sha256": fixture["manifest_sha256"],
        "variant": "baseline",
        "mode": "replay",
        "model_request": None,
        "budget": None,
        "metric_policy": {"policy_version": "v1", "revision": "fake-smoke-r1"},
        "seed": None,
    }
    spec.update(overrides)
    return spec


def write_spec(tmp_repo: Path, spec_dict: dict[str, object]) -> tuple[Path, str]:
    path = tmp_repo / "specs" / "spec.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(spec_dict, indent=2, ensure_ascii=False)
    path.write_text(text, encoding="utf-8")
    return path, json_sha256(json.loads(text))


def live_spec(fixture: dict[str, str], **overrides: object) -> dict[str, object]:
    return make_spec(
        fixture,
        mode="live",
        model_request={
            "provider": "fake",
            "model": "fake-model",
            "prompt_id": "fake-prompt-1",
            "schema_id": "fake-schema-1",
        },
        budget={"max_calls": 10, "max_input_tokens": 10_000, "max_output_tokens": 10_000, "max_concurrency": 1},
        **overrides,
    )


__all__ = ["ExperimentSpec", "make_spec", "live_spec", "write_spec"]
