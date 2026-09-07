"""Unit tests for NarrativeDutyPolicy/v1 and the candidate wire decoder.

Mirrors the kernel compact tests: synthetic inputs only, no provider, no DB.
The baseline decode path is exercised through the same fixtures the kernel
uses, so the shadow decoder is verified against the real strict rules.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from autocut_kernel.semantic_chain.story_design_compact import (
    story_design_compact_response_schema,
)
from autocut_kernel.semantic_chain.story_design_draft import StoryDesignDraftPolicy

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "packages" / "autocut-kernel" / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "packages" / "autocut-kernel" / "src"))

from tests.semantic_chain.test_material_support import material_case  # noqa: E402
from tests.semantic_chain.test_story_design_draft import POLICY as DRAFT_POLICY  # noqa: E402
from tools.reuse.narrative_strategy import context_io  # noqa: E402
from tools.reuse.narrative_strategy.policy import (  # noqa: E402
    NARRATIVE_WIRE_SCHEMA_VERSION,
    candidate_prompt_section,
    extend_response_schema,
)
from tools.reuse.narrative_strategy.wire import (  # noqa: E402
    DUTY_DUPLICATE,
    DUTY_FIELD_INVALID,
    DUTY_REFERENCE_NOT_FOUND,
    DUTY_REFERENCE_TYPE_MISMATCH,
    DUTY_SETUP_PAYOFF_IDENTICAL,
    DUTY_TARGET_MISMATCH,
    DUTY_UNKNOWN_KIND,
    NarrativeDutyError,
    decode_narrative_story_draft,
)


def _build_context():
    case = material_case()
    from autocut_kernel.semantic_chain.story_design_compact import (
        build_story_design_compact_context,
    )

    context = build_story_design_compact_context(
        case["inputs"], case["stage1"], case["projection"],
        **{key: case[key] for key in ("job_policy", "story_policy", "candidate_policy")},
    )
    return case, context


@pytest.fixture()
def ctx():
    return _build_context()


def _wire(ctx, duties_by_proposal: dict[int, list[dict]] | None = None) -> str:
    """A valid compact wire (via migration of the kernel fixture draft), with
    optional duties attached."""
    from autocut_kernel.semantic_chain.story_design_compact_migration import (
        migrate_story_design_v1_to_compact,
    )

    case, context = ctx
    raw = json.dumps(case["draft"].to_mapping()).encode("utf-8")
    migration = migrate_story_design_v1_to_compact(raw, context=context, policy=_draft_policy())
    payload = json.loads(migration.wire_bytes)
    payload["schema_version"] = NARRATIVE_WIRE_SCHEMA_VERSION
    if duties_by_proposal:
        for index, duties in duties_by_proposal.items():
            payload["proposals"][index]["narrative_duties"] = duties
    return json.dumps(payload, ensure_ascii=False)


def _draft_policy() -> StoryDesignDraftPolicy:
    return DRAFT_POLICY


class TestPolicy:
    def test_prompt_section_has_no_subtitle_timing_or_narration_rules(self) -> None:
        section = candidate_prompt_section()
        # absorbed duties only; forbidden methods must not appear as rules
        assert "hook" in section and "payoff" in section
        assert "不要根据字幕时间选择片段" in section
        assert "不要引入旁白" in section

    def test_extend_schema_keeps_closed_fields(self, ctx) -> None:
        _, context = ctx
        base = story_design_compact_response_schema(_draft_policy())
        extended = extend_response_schema(base)
        proposal_schema = extended["properties"]["proposals"]["items"]
        assert proposal_schema["properties"]["narrative_duties"]["items"]["properties"][
            "duty_kind"]["enum"][0] == "hook"
        assert proposal_schema["additionalProperties"] is False
        # duties themselves are closed too
        assert proposal_schema["properties"]["narrative_duties"]["items"][
            "additionalProperties"] is False


class TestDecode:
    def test_baseline_wire_without_duties_decodes(self, ctx) -> None:
        case, context = ctx
        raw = _wire(ctx)
        narrative = decode_narrative_story_draft(raw, context=context, policy=_draft_policy())
        assert narrative.duty_count == 0
        assert len(narrative.base.proposals) == len(case["draft"].proposals)

    def test_duties_decoded_and_bound_to_proposals(self, ctx) -> None:
        _, context = ctx
        raw = _wire(ctx, {0: [
            {"duty_kind": "hook", "target_ref": "proposal-0", "reason": "开场即冲突"},
            {"duty_kind": "setup", "target_ref": "proposal-0", "reason": "铺垫身份"},
        ]})
        narrative = decode_narrative_story_draft(raw, context=context, policy=_draft_policy())
        assert narrative.duty_count == 2
        proposal_id, duties = narrative.duties[0]
        assert proposal_id == narrative.base.proposals[0].proposal_id
        assert [d.duty_kind for d in duties] == ["hook", "setup"]

    def test_paired_duty_with_context_refs_passes(self, ctx) -> None:
        _, context = ctx
        ref_aliases = sorted(a for a, _ in context.aliases if a[0] in ("e", "f"))
        assert len(ref_aliases) >= 2, "fixture must contain two event/fact aliases"
        raw = _wire(ctx, {0: [{
            "duty_kind": "payoff", "target_ref": "proposal-0",
            "reason": "回收铺垫", "setup_ref": ref_aliases[0], "payoff_ref": ref_aliases[-1],
        }]})
        narrative = decode_narrative_story_draft(raw, context=context, policy=_draft_policy())
        assert narrative.duty_count == 1

    def test_unknown_duty_kind_rejected(self, ctx) -> None:
        _, context = ctx
        raw = _wire(ctx, {0: [{"duty_kind": "plot_twist", "target_ref": "proposal-0", "reason": "x"}]})
        with pytest.raises(NarrativeDutyError) as exc:
            decode_narrative_story_draft(raw, context=context, policy=_draft_policy())
        assert exc.value.code == DUTY_UNKNOWN_KIND

    def test_wrong_target_ref_rejected(self, ctx) -> None:
        _, context = ctx
        raw = _wire(ctx, {1: [{"duty_kind": "hook", "target_ref": "proposal-0", "reason": "x"}]})
        with pytest.raises(NarrativeDutyError) as exc:
            decode_narrative_story_draft(raw, context=context, policy=_draft_policy())
        assert exc.value.code == DUTY_TARGET_MISMATCH

    def test_paired_duty_missing_refs_rejected(self, ctx) -> None:
        _, context = ctx
        raw = _wire(ctx, {0: [{"duty_kind": "payoff", "target_ref": "proposal-0", "reason": "x"}]})
        with pytest.raises(NarrativeDutyError) as exc:
            decode_narrative_story_draft(raw, context=context, policy=_draft_policy())
        assert exc.value.code == DUTY_FIELD_INVALID

    def test_unknown_reference_rejected(self, ctx) -> None:
        _, context = ctx
        raw = _wire(ctx, {0: [{
            "duty_kind": "reveal", "target_ref": "proposal-0", "reason": "x",
            "setup_ref": "e999", "payoff_ref": "e1",
        }]})
        with pytest.raises(NarrativeDutyError) as exc:
            decode_narrative_story_draft(raw, context=context, policy=_draft_policy())
        assert exc.value.code == DUTY_REFERENCE_NOT_FOUND

    def test_non_event_reference_type_rejected(self, ctx) -> None:
        _, context = ctx
        subject_alias = next(a for a, _ in context.aliases if a.startswith("p"))
        raw = _wire(ctx, {0: [{
            "duty_kind": "reveal", "target_ref": "proposal-0", "reason": "x",
            "setup_ref": subject_alias, "payoff_ref": subject_alias,
        }]})
        with pytest.raises(NarrativeDutyError) as exc:
            decode_narrative_story_draft(raw, context=context, policy=_draft_policy())
        assert exc.value.code == DUTY_REFERENCE_TYPE_MISMATCH

    def test_identical_setup_payoff_rejected(self, ctx) -> None:
        _, context = ctx
        event_alias = next(a for a, _ in context.aliases if a.startswith("e"))
        raw = _wire(ctx, {0: [{
            "duty_kind": "cliffhanger", "target_ref": "proposal-0", "reason": "x",
            "setup_ref": event_alias, "payoff_ref": event_alias,
        }]})
        with pytest.raises(NarrativeDutyError) as exc:
            decode_narrative_story_draft(raw, context=context, policy=_draft_policy())
        assert exc.value.code == DUTY_SETUP_PAYOFF_IDENTICAL

    def test_duplicate_duty_rejected(self, ctx) -> None:
        _, context = ctx
        duty = {"duty_kind": "hook", "target_ref": "proposal-0", "reason": "x"}
        raw = _wire(ctx, {0: [duty, dict(duty, reason="y")]})
        with pytest.raises(NarrativeDutyError) as exc:
            decode_narrative_story_draft(raw, context=context, policy=_draft_policy())
        assert exc.value.code == DUTY_DUPLICATE

    def test_wrong_schema_version_rejected(self, ctx) -> None:
        _, context = ctx
        raw = _wire(ctx)
        payload = json.loads(raw)
        payload["schema_version"] = "stage2-story-design-compact-v2"
        with pytest.raises(NarrativeDutyError) as exc:
            decode_narrative_story_draft(
                json.dumps(payload, ensure_ascii=False), context=context, policy=_draft_policy())
        assert exc.value.code == DUTY_FIELD_INVALID

    def test_kernel_decode_still_enforces_closed_fields(self, ctx) -> None:
        """Unknown proposal fields (beyond narrative_duties) must fail in the
        kernel decoder even after duty stripping."""
        _, context = ctx
        raw = _wire(ctx)
        payload = json.loads(raw)
        payload["proposals"][0]["sneaky"] = True
        with pytest.raises(Exception):  # kernel CompactDraftError
            decode_narrative_story_draft(
                json.dumps(payload, ensure_ascii=False), context=context, policy=_draft_policy())


class TestContextIo:
    def test_payload_roundtrip_preserves_context(self, ctx) -> None:
        _, context = ctx
        payload = context_io.export_shadow_payload(context, _draft_policy())
        rebuilt = context_io.rebuild_context(payload)
        assert rebuilt == context
        assert rebuilt.canonical_hash == context.canonical_hash
