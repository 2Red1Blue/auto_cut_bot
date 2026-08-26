"""The bounded local source compiler has no generic RegistrySet fallback."""

from __future__ import annotations

import pytest
from authority.errors import GateViolation
from authority.profile_sources import compile_locked_profile_sources
from authority.shadow_context import SHADOW_PROFILE_SCHEMA_PATH
from autocut_kernel.registry.installed_local_run import compute_local_profile_registry_sha256

from tests.authority.test_shadow_context import NARRATIVE_PATH, SHADOW_PATH, _sources


def _compile(tmp_path, *, profile_kind: str):
    fixture = _sources(tmp_path)
    compilation = compile_locked_profile_sources(
        **{key: value for key, value in fixture.options.items() if key != "shadow_path"},
        profile_path=SHADOW_PATH,
        schema_path=SHADOW_PROFILE_SCHEMA_PATH,
        profile_kind=profile_kind,
    )
    return fixture, compilation


def test_profile_compilation_binds_exact_three_blobs_and_frozen_kind(tmp_path) -> None:
    fixture, compilation = _compile(tmp_path, profile_kind="shadow_calibration_v1")
    assert compilation.registry_sha256 == compute_local_profile_registry_sha256(
        profile_kind="shadow_calibration_v1",
        narrative_raw=fixture.narrative_raw,
        profile_raw=fixture.shadow_raw,
        schema_raw=fixture.schema_raw,
    )
    assert compilation.registry_sha256 != compute_local_profile_registry_sha256(
        profile_kind="local_run_v1",
        narrative_raw=fixture.narrative_raw,
        profile_raw=fixture.shadow_raw,
        schema_raw=fixture.schema_raw,
    )
    assert not hasattr(compilation, "registry_set") and not hasattr(compilation, "ready")
    assert tuple(entry["path"] for entry in fixture.lock["entries"]) == (
        NARRATIVE_PATH,
        SHADOW_PATH,
        SHADOW_PROFILE_SCHEMA_PATH,
    )


def test_profile_compilation_rejects_an_unapproved_kind(tmp_path) -> None:
    fixture = _sources(tmp_path)
    with pytest.raises(GateViolation, match="AUTH-PROFILE-KIND"):
        compile_locked_profile_sources(
            **{key: value for key, value in fixture.options.items() if key != "shadow_path"},
            profile_path=SHADOW_PATH,
            schema_path=SHADOW_PROFILE_SCHEMA_PATH,
            profile_kind="generic_registry_v1",
        )
