from __future__ import annotations

import json
from pathlib import Path

import pytest
from autocut_kernel.vlm.observation_aliases import ObservationAliasMap
from autocut_kernel.vlm.observation_contract import (
    OBSERVATION_DECODER_IMPLEMENTATION_SHA256,
    ObservationContractError,
    ObservationLimits,
    ObservationReport,
    _decoder_implementation_identity_from_sources,
    decode_observation_report,
    observation_decoder_implementation_identity,
    observation_response_schema,
)


@pytest.fixture
def limits() -> ObservationLimits:
    return ObservationLimits(4096, 1000, 400, 4, 4, 4)


def _raw(**overrides: object) -> bytes:
    report: dict[str, object] = {
        "description": "红衣女子进入房间。工牌写着 A01，墙上字幕有 p001。",
        "moments": [{"description": "她把信封放到桌上。", "time_hint": "开头不久"}],
        "interpretations": ["我觉得她可能很紧张，但无法确认原因。"],
        "uncertainties": ["无法确定字幕对应谁说话。"],
    }
    report.update(overrides)
    return json.dumps(report, ensure_ascii=False).encode()


def test_base_schema_and_decoder_keep_rich_observation_without_machine_duties(limits: ObservationLimits) -> None:
    assert OBSERVATION_DECODER_IMPLEMENTATION_SHA256.startswith("sha256:")
    identity = observation_decoder_implementation_identity()
    assert [source["path"] for source in identity["sources"]] == [
        "observation_contract.py", "observation_aliases.py",
    ]
    assert all(source["sha256"].startswith("sha256:") for source in identity["sources"])
    source_dir = Path(__file__).parents[2] / "packages/autocut-kernel/src/autocut_kernel/vlm"
    sources = {
        name: (source_dir / name).read_bytes()
        for name in ("observation_contract.py", "observation_aliases.py")
    }
    changed = dict(sources)
    changed["observation_aliases.py"] += b"\n# implementation change\n"
    assert _decoder_implementation_identity_from_sources(sources) != _decoder_implementation_identity_from_sources(changed)
    schema = observation_response_schema()
    assert set(schema) == {"type", "additionalProperties", "required", "properties"}
    assert set(schema["properties"]) == {"description", "moments", "interpretations", "uncertainties"}
    encoded = json.dumps(schema, ensure_ascii=False)
    for forbidden in ("evidence", "candidate", "confidence", "proof", "enum", "id"):
        assert forbidden not in encoded

    report = decode_observation_report(_raw(), limits)
    assert report.description.endswith("p001。")
    assert report.to_mapping()["moments"] == [{"description": "她把信封放到桌上。", "time_hint": "开头不久"}]
    assert report.selection is None


@pytest.mark.parametrize(
    "raw",
    [
        b'{"description":"x","description":"y","moments":[],"interpretations":[],"uncertainties":[]}',
        b'{"description":"\\ud800","moments":[],"interpretations":[],"uncertainties":[]}',
        b'{"description":"x","moments":[],"interpretations":[],"uncertainties":[],"x":1}',
        b'{"description":"x","moments":[],"interpretations":[],"uncertainties":[],"n":NaN}',
    ],
)
def test_decoder_rejects_non_closed_or_non_strict_roots(raw: bytes, limits: ObservationLimits) -> None:
    with pytest.raises(ObservationContractError):
        decode_observation_report(raw, limits)


def test_decoder_enforces_byte_and_depth_budgets(limits: ObservationLimits) -> None:
    with pytest.raises(ObservationContractError, match="byte budget"):
        decode_observation_report(_raw(description="x" * 4096), limits)
    shallow_limits = ObservationLimits(4096, 1000, 400, 4, 4, 4, max_json_depth=2)
    with pytest.raises(ObservationContractError, match="nesting depth"):
        decode_observation_report(_raw(), shallow_limits)


def test_malformed_optional_time_hint_keeps_moment_and_diagnostic(limits: ObservationLimits) -> None:
    report = decode_observation_report(_raw(moments=[{"description": "手指收紧。", "time_hint": {"rough": 123}}]), limits)
    moment = report.moments[0]
    assert moment.description == "手指收紧。"
    assert moment.time_hint is None
    assert dict(moment.raw_time_hint) == {"rough": 123}
    assert moment.time_hint_issue == "optional_time_hint_not_string"
    assert json.dumps(report.to_diagnostic_mapping(), ensure_ascii=False)


def test_selection_schema_is_request_local_and_unknown_alias_stays_unresolved(limits: ObservationLimits) -> None:
    aliases = ObservationAliasMap("request-1", {"a": "existing-person-long-001", "b": "existing-person-long-002"})
    schema = observation_response_schema(aliases)
    assert schema["properties"]["selected_id"] == {"enum": ["a", "b", None]}
    assert "existing-person-long-001" not in json.dumps(schema)

    selected = decode_observation_report(_raw(selected_id="b", selection_reason="衣服更接近 b"), limits, aliases)
    assert selected.selection is not None
    assert selected.selection.status == "selected"
    assert selected.selection.resolved_target == "existing-person-long-002"

    unknown = decode_observation_report(_raw(selected_id="zz", selection_reason="无法对应"), limits, aliases)
    assert unknown.to_mapping() == selected.to_mapping()
    assert unknown.selection is not None
    assert unknown.selection.status == "unresolved"
    assert unknown.selection.raw_selected_id == "zz"
    assert unknown.selection.resolved_target is None

    malformed = decode_observation_report(_raw(selected_id={"bad": 17}, selection_reason=None), limits, aliases)
    assert malformed.description == unknown.description
    assert malformed.selection is not None
    assert malformed.selection.status == "unresolved"
    assert dict(malformed.selection.raw_selected_id) == {"bad": 17}
    assert json.dumps(malformed.to_diagnostic_mapping(), ensure_ascii=False)


def test_report_rejects_mutable_collections_even_when_dataclass_is_frozen() -> None:
    with pytest.raises(ObservationContractError, match="moments must be an immutable tuple"):
        ObservationReport("x", [], (), ())


def test_alias_map_replay_identity_is_order_independent_and_scope_bound() -> None:
    first = ObservationAliasMap("attempt-7", {"a": "target-one", "b": "target-two"})
    reordered = ObservationAliasMap("attempt-7", {"b": "target-two", "a": "target-one"})
    different_scope = ObservationAliasMap("attempt-8", {"a": "target-one", "b": "target-two"})
    assert first.canonical_hash == reordered.canonical_hash
    assert first.canonical_hash != different_scope.canonical_hash
    assert first.resolve("a") == "target-one"
    assert first.resolve("missing") is None
    with pytest.raises(ValueError, match="one-to-one"):
        ObservationAliasMap("attempt-7", {"a": "same", "b": "same"})
