from __future__ import annotations

import pytest
from autocut_kernel.vlm.observation_aliases import ObservationAliasMap
from autocut_kernel.vlm.observation_prompt import build_observation_prompt


def test_prompt_encourages_rich_observation_interpretation_and_unknown_without_targets() -> None:
    aliases = ObservationAliasMap("request", {"a": "private-long-target-001", "b": "private-long-target-002"})
    prompt = build_observation_prompt(
        task="描述门口人物和房间里的互动", audio_capability="未验证音轨，不描述对白",
        alias_map=aliases, alias_descriptions={"a": "红色外套、短发", "b": "灰色外套、戴眼镜"},
    )
    for expected in ("充分描述", "interpretations", "uncertainties", "a: 红色外套", "selected_id", "无法判断"):
        assert expected in prompt
    for forbidden in ("private-long-target", "evidence", "candidate", "confidence", "proof"):
        assert forbidden not in prompt


def test_prompt_requires_exactly_the_safe_alias_descriptor_projection() -> None:
    aliases = ObservationAliasMap("request", {"a": "private-target"})
    with pytest.raises(ValueError, match="exactly cover"):
        build_observation_prompt(task="观察", audio_capability="无音轨", alias_map=aliases, alias_descriptions={})
    with pytest.raises(ValueError, match="require an alias_map"):
        build_observation_prompt(task="观察", audio_capability="无音轨", alias_descriptions={"a": "x"})
