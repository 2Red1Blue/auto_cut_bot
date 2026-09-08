"""Opt-in PostgreSQL transaction proof for the bounded SpanVariantSet writer."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from autocut_kernel.pipeline import build_span_variant_set_command as command_module
from autocut_kernel.pipeline.build_span_variant_set_command import (
    BUILD_SPAN_VARIANT_SET_COMMAND,
    build_span_variant_set_artifact,
    rebuild_span_variant_set,
    resolve_build_span_variant_set_request,
)
from autocut_kernel.store import CommandClaim, CommandSuccess, PostgresRuntimeStore
from autocut_kernel.store.models import artifact_set_hash, canonical_payload_hash

from tests.pipeline.test_build_span_variant_set_command import _prepared_case

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("AUTOCUT_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(
    not DSN,
    reason="set AUTOCUT_TEST_POSTGRES_DSN to run disposable PostgreSQL tests",
)
MIGRATIONS = Path("packages/autocut-kernel/migrations")


@pytest.fixture(autouse=True)
def migrated_database() -> None:
    assert DSN is not None
    with psycopg.connect(DSN, autocommit=True) as connection:
        if connection.info.dbname != "ac_autocut_verify":
            pytest.fail("AUTOCUT_TEST_POSTGRES_DSN must name disposable ac_autocut_verify")
        with connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA IF EXISTS storage CASCADE")
            cursor.execute("DROP SCHEMA IF EXISTS runtime CASCADE")
            for name in (
                "0001_runtime_core.sql",
                "0002_runtime_core_constraints.sql",
                "0003_vlm_generation_and_run_finalization.sql",
                "0004_provider_media_objects.sql",
                "0006_ark_provider_recovery.sql",
                "0009_vlm_bounded_retry.sql",
                "0011_generation_retry_schedule.sql",
                "0018_command_execution_kind.sql",
            ):
                cursor.execute((MIGRATIONS / name).read_text())


def test_postgres_writer_commits_and_replays_exact_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert DSN is not None
    fixture_store, request, resolver, limits, _parent = _prepared_case(tmp_path, monkeypatch)
    resolved = resolve_build_span_variant_set_request(
        fixture_store,
        request,
        authority_profile_resolver=resolver,
        limits=limits,
    )
    value = rebuild_span_variant_set(resolved)
    artifact = build_span_variant_set_artifact(request, value)
    success_artifacts = (artifact,)
    success_hash = artifact_set_hash(success_artifacts)

    monkeypatch.setattr(
        command_module,
        "resolve_build_span_variant_set_request",
        lambda *_args, **_kwargs: resolved,
    )
    monkeypatch.setattr(
        command_module,
        "rebuild_span_variant_set",
        lambda actual: value if actual is resolved else None,
    )
    durable = PostgresRuntimeStore(lambda: psycopg.connect(DSN))
    claimed = durable.claim_command(
        CommandClaim(
            request.job,
            request.idempotency_key,
            BUILD_SPAN_VARIANT_SET_COMMAND,
            resolved.request_hash,
            execution_kind="deterministic",
        )
    )
    success = CommandSuccess(claimed.command_slot_id, success_hash, success_artifacts)

    first = durable.commit_span_variant_set_success(
        request,
        success,
        authority_profile_resolver=resolver,
        limits=limits,
    )
    replay = durable.commit_span_variant_set_success(
        request,
        success,
        authority_profile_resolver=resolver,
        limits=limits,
    )

    assert first.state == "succeeded"
    assert replay.receipt_id == first.receipt_id
    assert replay.artifact_set_id == first.artifact_set_id
    record = durable.read_committed_artifact_set(
        request.job,
        command_slot_id=claimed.command_slot_id,
        receipt_id=first.receipt_id,
        artifact_set_id=first.artifact_set_id,
        expected_request_hash=resolved.request_hash,
        expected_command_name=BUILD_SPAN_VARIANT_SET_COMMAND,
        expected_execution_kind="deterministic",
    )
    assert record.set_hash == success_hash
    assert canonical_payload_hash(record.members[0].payload_json) == artifact.content_hash
    assert json.loads(record.members[0].payload_json) == json.loads(artifact.payload_json)
