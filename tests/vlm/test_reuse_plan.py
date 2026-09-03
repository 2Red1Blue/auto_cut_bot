"""Pure, deterministic cross-run VLM reuse-plan coverage."""

from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path

import pytest
from autocut_kernel.vlm.reuse_identity import VlmReuseIdentityV1
from autocut_kernel.vlm.reuse_plan import (
    VLM_REUSE_PLAN_SCHEMA_VERSION,
    VlmReuseOriginClosureReference,
    VlmReusePlan,
    VlmReusePlanEpisode,
    VlmTargetCensusReference,
)

from .test_reuse_identity import _context_pack, _identity, _request


def _origin(identity_hash: str) -> VlmReuseOriginClosureReference:
    return VlmReuseOriginClosureReference(
        origin_job_key="base-run",
        origin_profile="pc_cuda",
        origin_child_idempotency_key="vlm-child-0",
        origin_attempt_id="attempt-1",
        origin_reuse_identity_sha256=identity_hash,
        origin_receipt_sha256="sha256:" + "1" * 64,
        origin_artifact_set_sha256="sha256:" + "2" * 64,
        origin_request_payload_sha256="sha256:" + "3" * 64,
        origin_response_payload_sha256="sha256:" + "4" * 64,
        origin_semantic_pack_sha256="sha256:" + "5" * 64,
    )


def _identity_for_episode(
    episode_index: int,
    *,
    context: str | None = None,
    model_id: str | None = None,
    prompt_suffix: str = "",
) -> VlmReuseIdentityV1:
    request = _request(
        context_pack=None if context is None else _context_pack(context)
    )
    request = replace(
        request,
        episode_index=episode_index,
        prompt_template=request.prompt_template + prompt_suffix,
        **({"model_id": model_id} if model_id is not None else {}),
    )
    return _identity(request)


def _episode(
    episode_index: int,
    *,
    base: VlmReuseIdentityV1 | None = None,
    target: VlmReuseIdentityV1 | None = None,
    origin: bool = True,
) -> VlmReusePlanEpisode:
    target_identity = _identity_for_episode(episode_index) if target is None else target
    base_identity = target_identity if base is None else base
    return VlmReusePlanEpisode(
        episode_index=episode_index,
        target_identity=target_identity,
        base_identity=base_identity,
        origin=_origin(base_identity.canonical_hash) if origin and base_identity is not None else None,
    )


def _target_census(
    episodes: tuple[VlmReusePlanEpisode, ...],
) -> VlmTargetCensusReference:
    target = episodes[0].target_identity
    return VlmTargetCensusReference(
        declared_episode_count=len(episodes),
        target_source_manifest_sha256=target.source_manifest_sha256,
        target_source_provenance_sha256=target.source_provenance_sha256,
    )


def test_plan_is_stable_and_retains_closed_census_and_origin_references() -> None:
    episodes = (_episode(0), _episode(1))
    first = VlmReusePlan.build(
        target_census=_target_census(episodes),
        source_episode_census=episodes,
        selected_episode_indexes=(0, 1),
    )
    second = VlmReusePlan.build(
        target_census=_target_census(episodes),
        source_episode_census=episodes,
        selected_episode_indexes=(0, 1),
    )

    assert first == second
    assert first.canonical_hash == second.canonical_hash
    assert first.result_scope == "complete_batch"
    assert [node.decision for node in first.nodes] == ["reuse", "reuse"]
    assert first.nodes[0].origin is not None
    assert first.nodes[0].origin.origin_job_key == "base-run"
    assert first.to_mapping()["schema_version"] == VLM_REUSE_PLAN_SCHEMA_VERSION
    assert len(first.to_mapping()["source_episode_census"]) == 2


def test_policy_context_and_input_changes_have_distinct_execute_reasons() -> None:
    exact = _identity_for_episode(0)
    policy_changed = _identity_for_episode(0, model_id="doubao-seed-2-1-pro-next")
    context_base = _identity_for_episode(1, context="Ivy arrives.")
    context_target = _identity_for_episode(1, context="Ivy leaves.")
    input_changed = _identity_for_episode(2)
    different_prompt = _identity_for_episode(2, prompt_suffix="Different window note.")

    plan = VlmReusePlan.build(
        target_census=_target_census(
            (
                _episode(0, base=policy_changed, target=exact),
                _episode(1, base=context_base, target=context_target),
                _episode(2, base=input_changed, target=different_prompt),
            )
        ),
        source_episode_census=(
            _episode(0, base=policy_changed, target=exact),
            _episode(1, base=context_base, target=context_target),
            _episode(2, base=input_changed, target=different_prompt),
        ),
        selected_episode_indexes=(0, 1, 2),
    )

    assert [node.decision for node in plan.nodes] == ["execute", "execute", "execute"]
    assert [node.reason for node in plan.nodes] == [
        "semantic_policy_mismatch",
        "context_pack_mismatch",
        "input_identity_mismatch",
    ]
    assert all(node.origin is None for node in plan.nodes)


def test_selected_only_multi_episode_plan_stays_inspection_without_expanding_selection() -> None:
    episodes = (_episode(0), _episode(1), _episode(2))
    plan = VlmReusePlan.build(
        target_census=_target_census(episodes),
        source_episode_census=episodes,
        selected_episode_indexes=(1,),
    )

    assert plan.result_scope == "inspection"
    assert plan.selected_episode_indexes == (1,)
    assert [node.episode_index for node in plan.nodes] == [1]
    assert len(plan.source_episode_census) == 3


def test_sliced_census_cannot_claim_complete_batch_under_three_episode_target() -> None:
    target = _identity_for_episode(0)
    bound_three_episode_target = VlmTargetCensusReference(
        declared_episode_count=3,
        target_source_manifest_sha256=target.source_manifest_sha256,
        target_source_provenance_sha256=target.source_provenance_sha256,
    )

    with pytest.raises(ValueError, match="declared episode count"):
        VlmReusePlan.build(
            target_census=bound_three_episode_target,
            source_episode_census=(VlmReusePlanEpisode(0, target),),
            selected_episode_indexes=(0,),
        )


def test_target_census_identity_must_match_every_target_episode() -> None:
    episode = _episode(0)
    with pytest.raises(ValueError, match="must match target_census"):
        VlmReusePlan.build(
            target_census=VlmTargetCensusReference(
                1,
                "sha256:" + "d" * 64,
                episode.target_identity.source_provenance_sha256,
            ),
            source_episode_census=(episode,),
            selected_episode_indexes=(0,),
        )


def test_missing_origin_or_identity_mismatch_never_produces_reuse() -> None:
    target = _identity_for_episode(0)
    base = _identity_for_episode(1)
    mismatched_origin = _origin("sha256:" + "f" * 64)
    plan = VlmReusePlan.build(
        target_census=_target_census(
            (
                _episode(0, base=target, target=target, origin=False),
                VlmReusePlanEpisode(1, base, base, mismatched_origin),
            )
        ),
        source_episode_census=(
            _episode(0, base=target, target=target, origin=False),
            VlmReusePlanEpisode(1, base, base, mismatched_origin),
        ),
        selected_episode_indexes=(0, 1),
    )

    assert [node.reason for node in plan.nodes] == [
        "missing_origin_closure",
        "origin_identity_mismatch",
    ]


def test_missing_base_identity_is_an_explicit_execute_decision() -> None:
    target = _identity_for_episode(0)
    plan = VlmReusePlan.build(
        target_census=VlmTargetCensusReference(
            1, target.source_manifest_sha256, target.source_provenance_sha256
        ),
        source_episode_census=(VlmReusePlanEpisode(0, target),),
        selected_episode_indexes=(0,),
    )

    assert plan.result_scope == "complete_batch"
    assert plan.nodes[0].decision == "execute"
    assert plan.nodes[0].reason == "missing_base_identity"
    assert plan.nodes[0].base_identity_sha256 is None


@pytest.mark.parametrize(
    ("census", "selection", "message"),
    [
        ((), (0,), "must not be empty"),
        ((_episode(0), _episode(2)), (0,), "ordered contiguous"),
        ((_episode(0),), (), "must not be empty"),
        ((_episode(0), _episode(1)), (1, 0), "sorted and unique"),
        ((_episode(0),), (1,), "must be a subset"),
    ],
)
def test_invalid_plan_shapes_fail_closed(
    census: tuple[VlmReusePlanEpisode, ...], selection: tuple[int, ...], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        VlmReusePlan.build(
            target_census=(
                _target_census(census)
                if census
                else VlmTargetCensusReference(1, "sha256:" + "a" * 64, "sha256:" + "b" * 64)
            ),
            source_episode_census=census,
            selected_episode_indexes=selection,
        )


def test_episode_and_origin_shapes_are_closed() -> None:
    identity = _identity_for_episode(0)
    with pytest.raises(ValueError, match="base_identity"):
        VlmReusePlanEpisode(0, identity, None, _origin(identity.canonical_hash))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="origin_reuse_identity"):
        VlmReuseOriginClosureReference(
            "base-run", "pc_cuda", "child", "attempt", "sha256:" + "z" * 64,
            "sha256:" + "1" * 64, "sha256:" + "2" * 64, "sha256:" + "3" * 64,
            "sha256:" + "4" * 64, "sha256:" + "5" * 64,
        )


def test_plan_module_has_no_store_http_or_provider_imports() -> None:
    import autocut_kernel.vlm.reuse_plan as reuse_plan

    tree = ast.parse(Path(reuse_plan.__file__).read_text(encoding="utf-8"))
    imported_modules = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert not any(
        module.startswith(("autocut_kernel.store", "auto_cut_bot", "http", "requests"))
        for module in imported_modules
    )
