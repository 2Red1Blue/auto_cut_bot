from __future__ import annotations

import pytest
from autocut_kernel.semantic_chain.observation_inputs import (
    ObservationInputError,
    build_consumer_prompt,
    build_observation_consumer_inputs,
    consumer_schema,
)
from autocut_kernel.store.observation import PersistedObservationReport
from autocut_kernel.vlm.observation_contract import ObservationMoment, ObservationReport


def _report(description: str) -> PersistedObservationReport:
    value = object.__new__(PersistedObservationReport)
    object.__setattr__(value, "report", ObservationReport(description, (ObservationMoment("她转身离开", "中段"),), ("也许是误会",), ("身份不明",)))
    return value


def test_short_alias_directory_preserves_raw_unverified_observations() -> None:
    inputs = build_observation_consumer_inputs((_report("真实编号 SECRET-42，人物表情紧张"), _report("门口的人身份未知")))
    assert inputs.aliases == {"a": 0, "b": 1}
    assert inputs.report_for("a").report.interpretations == ("也许是误会",)
    assert inputs.report_for("a").report.uncertainties == ("身份不明",)
    prompt = build_consumer_prompt("narrative", inputs)
    assert '"id": "a"' in prompt
    assert "未验证" in prompt
    assert "她转身离开" in prompt
    assert "也许是误会" in prompt
    assert "身份不明" in prompt


def test_reordering_is_an_explicit_directory_identity_change() -> None:
    first = build_observation_consumer_inputs((_report("first"), _report("second")))
    second = build_observation_consumer_inputs((_report("second"), _report("first")))
    assert first.aliases == second.aliases
    assert first.descriptions != second.descriptions


def test_schema_rejects_unknown_references_and_never_contains_v4_fields() -> None:
    schema = consumer_schema("narrative", build_observation_consumer_inputs((_report("x"),)))
    item = schema["properties"]["units"]["items"]
    assert item["properties"]["report_refs"]["items"]["enum"] == ["a"]
    assert "facts" not in str(schema)
    assert "support" not in str(schema)
    assert "candidate" not in str(schema)


def test_non_persisted_or_unknown_stage_is_rejected() -> None:
    with pytest.raises(ObservationInputError):
        build_observation_consumer_inputs((object(),))
    with pytest.raises(ObservationInputError):
        consumer_schema("v4", build_observation_consumer_inputs((_report("x"),)))
    inputs = build_observation_consumer_inputs((_report("x"),))
    with pytest.raises(ObservationInputError):
        consumer_schema("story", inputs)
    with pytest.raises(TypeError):
        inputs.aliases["z"] = 0


def test_overbudget_report_is_rejected_without_truncation() -> None:
    with pytest.raises(ObservationInputError, match="prompt budget"):
        build_observation_consumer_inputs((_report("x" * 100),), max_prompt_chars=20)
