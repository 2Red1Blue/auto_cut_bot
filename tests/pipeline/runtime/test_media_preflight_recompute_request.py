"""Focused tests for the closed media-preflight recompute request."""

from __future__ import annotations

import pytest

from auto_cut_bot.pipeline.runtime.errors import PipelineRunValidationError
from auto_cut_bot.pipeline.runtime.models import MediaPreflightRecomputeRequest

RUN_ID = "pipeline_run_" + "a" * 32


def _mapping(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "base_run_id": RUN_ID,
        "expected_version": 4,
        "stage": "media_preflight",
        "completion_scope": "selected_only",
        "episode_numbers": [3],
        "retry_budget": 2,
    }
    value.update(overrides)
    return value


def test_media_preflight_recompute_request_is_closed_and_canonical() -> None:
    request = MediaPreflightRecomputeRequest.from_mapping(_mapping())

    assert request.selected_episode_index == 2
    assert request.to_mapping() == _mapping()
    assert MediaPreflightRecomputeRequest.from_mapping(request.to_mapping()) == request
    assert request.request_hash == MediaPreflightRecomputeRequest.from_mapping(
        {**_mapping(), "retry_budget": 2}
    ).request_hash
    assert request.request_hash == "sha256:3d79161d8c87b05b7930ddb874932c95c6eca4269e0b69d88dda0322b59bb3cd"
    assert request.request_hash != MediaPreflightRecomputeRequest.from_mapping(
        {**_mapping(), "retry_budget": 3}
    ).request_hash
    for field, value in (
        ("base_run_id", "pipeline_run_" + "b" * 32),
        ("expected_version", 5),
        ("episode_numbers", [4]),
    ):
        assert request.request_hash != MediaPreflightRecomputeRequest.from_mapping(
            _mapping(**{field: value})
        ).request_hash


@pytest.mark.parametrize("retry_budget", (0, 3))
def test_media_preflight_recompute_request_accepts_retry_budget_edges(retry_budget: int) -> None:
    request = MediaPreflightRecomputeRequest.from_mapping(_mapping(retry_budget=retry_budget))
    assert request.retry_budget == retry_budget


def test_media_preflight_recompute_request_maps_episode_one_to_zero() -> None:
    request = MediaPreflightRecomputeRequest.from_mapping(_mapping(episode_numbers=[1]))
    assert request.selected_episode_index == 0


@pytest.mark.parametrize(
    "overrides",
    (
        {"unexpected": True},
        {"retry_budget": None},
    ),
)
def test_media_preflight_recompute_request_rejects_non_closed_mappings(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(PipelineRunValidationError):
        MediaPreflightRecomputeRequest.from_mapping(_mapping(**overrides))

    missing = _mapping()
    del missing["retry_budget"]
    with pytest.raises(PipelineRunValidationError):
        MediaPreflightRecomputeRequest.from_mapping(missing)


@pytest.mark.parametrize(
    "field,value",
    (
        ("stage", "vlm"),
        ("completion_scope", "full_stage"),
        ("base_run_id", None),
        ("base_run_id", "not-a-run-id"),
        ("base_run_id", 123),
        ("expected_version", -1),
        ("expected_version", True),
        ("expected_version", 1.5),
        ("expected_version", "4"),
        ("expected_version", None),
        ("episode_numbers", []),
        ("episode_numbers", [2, 2]),
        ("episode_numbers", [2, 1]),
        ("episode_numbers", [0]),
        ("episode_numbers", [-1]),
        ("episode_numbers", [True]),
        ("retry_budget", -1),
        ("retry_budget", 4),
        ("retry_budget", True),
        ("retry_budget", 1.5),
    ),
)
def test_media_preflight_recompute_request_rejects_invalid_values(
    field: str,
    value: object,
) -> None:
    with pytest.raises(PipelineRunValidationError):
        MediaPreflightRecomputeRequest.from_mapping(_mapping(**{field: value}))


def test_media_preflight_recompute_request_requires_json_episode_array() -> None:
    with pytest.raises(PipelineRunValidationError, match="JSON array"):
        MediaPreflightRecomputeRequest.from_mapping(_mapping(episode_numbers=(3,)))


def test_media_preflight_recompute_request_rejects_non_object_json() -> None:
    with pytest.raises(PipelineRunValidationError):
        MediaPreflightRecomputeRequest.from_mapping(  # type: ignore[arg-type]
            ["base_run_id", "completion_scope", "episode_numbers", "expected_version", "retry_budget", "stage"]
        )
