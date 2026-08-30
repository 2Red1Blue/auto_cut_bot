from __future__ import annotations

import hashlib
import json
from uuid import uuid4

from autocut_kernel.context_pack import (
    ContextSelectionPolicy,
    ExternalContextSnapshot,
    OwnerEpisodeMap,
    OwnerEpisodeMapSet,
)
from autocut_kernel.store import BlobRef, CommandClaim, CommandOutcome, CommandSuccess
from autocut_kernel.store.models import canonical_recipe_scope

from auto_cut_bot.pipeline.context_prepare import (
    ExternalNarrativeApiConfig,
    ExternalNarrativeApiError,
    FetchedExternalNarrativeContext,
    PrepareWindowContextCommand,
    PrepareWindowContextRequest,
)
from tests.pipeline.test_pipeline_vlm_stage import _bundle


def _sha(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


class _Store:
    def __init__(self) -> None:
        self.claims: dict[str, tuple[CommandClaim, CommandOutcome]] = {}
        self.blobs: list[BlobRef] = []
        self.successes: list[CommandSuccess] = []

    def read_outcome(self, _job, key: str) -> CommandOutcome | None:
        current = self.claims.get(key)
        return None if current is None else current[1]

    def claim_command(self, claim: CommandClaim) -> CommandOutcome:
        current = self.claims.get(claim.idempotency_key)
        if current is not None:
            assert current[0] == claim
            return current[1]
        outcome = CommandOutcome(uuid4(), "running", job_id=uuid4(), is_fresh_claim=True)
        self.claims[claim.idempotency_key] = (claim, outcome)
        return outcome

    def put_immutable_blob(self, _job, *, content: bytes, content_hash: str, media_type: str) -> BlobRef:
        assert content_hash == _sha(content)
        ref = BlobRef(uuid4(), content_hash, len(content), media_type)
        self.blobs.append(ref)
        return ref

    def commit_command_success(self, success: CommandSuccess) -> CommandOutcome:
        self.successes.append(success)
        result = CommandOutcome(
            success.command_slot_id, "succeeded", receipt_id=uuid4(), artifact_set_id=uuid4(), job_id=uuid4()
        )
        for key, (claim, current) in self.claims.items():
            if current.command_slot_id == success.command_slot_id:
                self.claims[key] = (claim, result)
                return result
        raise AssertionError("unknown claim")

    def commit_command_rejection(self, _rejection):
        raise AssertionError("context preparation deliberately emits a video-only PackSet")


class _Client:
    def __init__(self) -> None:
        self.config = ExternalNarrativeApiConfig("https://metadata.example", "not-persisted")
        self.calls = 0

    def fetch(self, series_external_id: str) -> FetchedExternalNarrativeContext:
        self.calls += 1
        assets = {"data": {series_external_id: {
            "bookId": series_external_id,
            "bookName": "Safe title",
            "overallSynopsis": "future ending must never enter the pack",
            "stablePremise": "A student starts a new school.",
            "characters": [{"characterId": "c-1", "name": "Alice"}],
        }}}
        episodes = {"data": {series_external_id: {
            "bookId": series_external_id,
            "episodes": [{
                "episodeId": "ep-1", "chapterId": "ch-1", "episodeOrdinal": 1,
                "title": "First day", "summary": "Alice arrives.", "characters": ["c-1"],
                "subtitles": [{"text": "must never enter the pack"}],
            }],
        }}}
        raw = json.dumps(
            {"asset_response": assets, "episode_response": episodes},
            ensure_ascii=False, separators=(",", ":"), sort_keys=True,
        ).encode()
        return FetchedExternalNarrativeContext(
            ExternalContextSnapshot(
                "snapshot:fixture", series_external_id, ("/assets", "/episodes"),
                "https://metadata.example", "test", _sha(raw),
            ), raw, assets, episodes,
        )


def test_context_command_snapshots_then_commits_only_bounded_pack() -> None:
    bundle, _blobs = _bundle()
    maps = OwnerEpisodeMapSet("book-1", (
        OwnerEpisodeMap(
            "episode-000.mp4", 0, "book-1", "ep-1", "ch-1", 1,
        ),
    ))
    request = PrepareWindowContextRequest(
        job=bundle.source_job,
        idempotency_key="context-fixture",
        artifact_scope=canonical_recipe_scope(bundle.source_job),
        artifact_revision=1,
        source_bundle=bundle,
        owner_maps=maps,
        selection_policy=ContextSelectionPolicy(),
    )
    store, client = _Store(), _Client()
    result = PrepareWindowContextCommand(store, client).execute(request)

    assert result.outcome.state == "succeeded"
    assert client.calls == 1
    assert len(store.blobs) == 1
    assert result.prepared is not None
    pack = result.prepared.pack_for_episode(0)
    assert pack.mode == "api_assisted"
    assert "future ending" not in pack.rendered_context
    assert "must never enter" not in pack.rendered_context
    assert store.successes[0].artifacts[0].artifact_type == "window_context_pack_set"
    assert str(store.successes[0].artifacts[0].payload_json).find("not-persisted") < 0


def test_context_command_degrades_to_video_only_when_api_fetch_fails() -> None:
    bundle, _blobs = _bundle()
    maps = OwnerEpisodeMapSet("book-1", (
        OwnerEpisodeMap("episode-000.mp4", 0, "book-1", "ep-1", "ch-1", 1),
    ))
    request = PrepareWindowContextRequest(
        bundle.source_job, "context-failure", canonical_recipe_scope(bundle.source_job), 1,
        bundle, maps, ContextSelectionPolicy(),
    )

    class FailingClient:
        def fetch(self, _series: str):
            raise ExternalNarrativeApiError("network unavailable")

    result = PrepareWindowContextCommand(_Store(), FailingClient()).execute(request)
    assert result.outcome.state == "succeeded"
    assert result.prepared is not None
    assert result.prepared.pack_for_episode(0).video_only_reason_code == "EXTERNAL_CONTEXT_UNAVAILABLE"


def test_context_command_commits_video_only_pack_without_api_configuration() -> None:
    """A local semantic run remains executable when external narrative data is absent."""
    bundle, _blobs = _bundle()
    request = PrepareWindowContextRequest(
        bundle.source_job,
        "context-video-only",
        canonical_recipe_scope(bundle.source_job),
        1,
        bundle,
        None,
        ContextSelectionPolicy(),
    )
    store = _Store()
    result = PrepareWindowContextCommand(store, None).execute(request)

    assert result.outcome.state == "succeeded"
    assert result.prepared is not None
    assert result.prepared.pack_for_episode(0).mode == "video_only"
    assert result.prepared.pack_for_episode(0).video_only_reason_code == "EXTERNAL_CONTEXT_NOT_CONFIGURED"
    assert result.prepared.snapshot is None
    assert store.blobs == []
