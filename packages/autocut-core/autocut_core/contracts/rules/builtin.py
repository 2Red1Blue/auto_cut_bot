"""内置合同规则表 — SKILL.md 固定合同的声明式落地。

每条规则对应 SKILL.md "固定合同" 章节的一条 (或多条) 条款, 只挑
**可在纯数据结构上判定** 的核心规则先行落地 (共 15 条);
其余条款的落地清单见 skills/ac_shared_contracts/references/contract-rule-matrix.md。

规则 payload 约定 (artifacts 字典的 key):
  - ``story_script``:  Story Script JSON dict (beats / teaser_contract / ...);
  - ``series_bible``:  Series Bible JSON dict (story_threads / thread_beats);
  - ``story_plan``:    物化 Story Plan dict (mode / playback_duration / blocks);
  - ``qc_admission``:  QC Admission dict (decision / blocked_reasons);
  - ``render_recipe``: Render Recipe dict (mode / transitions)。
"""

from __future__ import annotations

from typing import Any, Mapping

from autocut_core.contracts.rules.engine import Finding, Rule

__all__ = [
    "BUILTIN_RULES",
    "STORY_DURATION_HARD_CAP_SECONDS",
    "REPEAT_RATIO_HARD_CAP",
    "ABSTRACT_ONLY_PHRASES",
    "is_abstract_only",
]

# ═══════════════════════════════════════════════════════════════════════════
# 合同常量 — 与 SKILL.md 固定合同逐条对应
# ═══════════════════════════════════════════════════════════════════════════

#: rule 7 / rule 22: Story Plan 不设时长下限, 只保留硬上限 1200 秒。
STORY_DURATION_HARD_CAP_SECONDS: float = 1200.0

#: rule 21: Partition 最终硬合同 repeat_ratio ≤ 10%。
REPEAT_RATIO_HARD_CAP: float = 0.10

#: rule 7: Broad Story Script beats 数量 4–14。
SCRIPT_BEAT_COUNT_MIN: int = 4
SCRIPT_BEAT_COUNT_MAX: int = 14

#: rule 22: 整集型播放占比警告线 (>40% warning, >50% 阻断)。
FULL_EPISODE_RATIO_WARN: float = 0.40
FULL_EPISODE_RATIO_BLOCK: float = 0.50

#: rule 23: 永远不得 QC Admission 的 blocked reasons。
NEVER_ADMIT_BLOCKED_REASONS: frozenset[str] = frozenset(
    {"dialogue_incomplete", "same_source_causal_gap", "missing_continuity_contract"}
)

#: rule 32: Teaser→正文黑场时长 (秒)。
TEASER_TO_BODY_BLACK_FRAME_SECONDS: float = 0.35

#: 抽象短语黑名单 — 与旧 validate_story_artifacts.is_abstract_only 保持逐字一致,
#: 行为基准见 tests/unit/test_validate_rule_equivalence.py。
ABSTRACT_ONLY_PHRASES: set[str] = {
    "矛盾升级",
    "女主反击",
    "男主反击",
    "关系破裂",
    "发现背叛",
    "真相揭晓",
    "冲突升级",
    "完成反转",
    "留下悬念",
}


def is_abstract_only(value: Any) -> bool:
    """判定 Beat 内容是否只有抽象描述 (无具体可观察内容)。

    与 validate_story_artifacts.py 的同名函数逐字等价 (行为基准
    由等价性测试锁定), 供 rule_04_beat_concrete_content 与
    示范规则复用。
    """
    if not isinstance(value, str):
        return True
    compact = "".join(value.split()).strip("。！!？?，,；;：:")
    return not compact or compact in ABSTRACT_ONLY_PHRASES or (
        len(compact) < 12
        and any(phrase in compact for phrase in ABSTRACT_ONLY_PHRASES)
    )


def _as_number(value: Any) -> float | None:
    """安全取数值 — 非法值返回 None (规则不因此误报)。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _clip_duration(clip: Mapping[str, Any]) -> float | None:
    duration = _as_number(clip.get("duration"))
    if duration is not None:
        return duration
    start = _as_number(clip.get("source_start"))
    end = _as_number(clip.get("source_end"))
    if start is not None and end is not None and end > start:
        return end - start
    return None


# ═══════════════════════════════════════════════════════════════════════════
# story_script 组 — SKILL.md rule 4 / 6 / 7 / 8
# ═══════════════════════════════════════════════════════════════════════════


def _check_script_beat_count(
    payloads: Mapping[str, Any],
) -> list[Finding]:
    script = payloads["story_script"]
    beats = script.get("beats")
    if not isinstance(beats, list):
        return [
            Finding(
                code="beats_missing",
                message="story script must declare a beats array",
                location="beats",
            )
        ]
    if not (SCRIPT_BEAT_COUNT_MIN <= len(beats) <= SCRIPT_BEAT_COUNT_MAX):
        return [
            Finding(
                code="beat_count_out_of_range",
                message=(
                    f"Broad Story Script beats 数量必须在 "
                    f"{SCRIPT_BEAT_COUNT_MIN}–{SCRIPT_BEAT_COUNT_MAX} 之间, "
                    f"实际 {len(beats)}"
                ),
                location="beats",
                suggestion="不为凑时长做无功能扩充; 结构缺口走 expand_story_scope",
            )
        ]
    return []


def _check_script_required_roles(
    payloads: Mapping[str, Any],
) -> list[Finding]:
    script = payloads["story_script"]
    beats = script.get("beats") or []
    roles = [beat.get("role") for beat in beats if isinstance(beat, dict)]
    mode = (script.get("teaser_contract") or {}).get("mode", "single_highlight")
    findings: list[Finding] = []
    if mode == "single_highlight":
        for required_role in ("teaser_intent", "escalation", "payoff"):
            if required_role not in roles:
                findings.append(
                    Finding(
                        code="missing_required_role",
                        message=f"missing required beat {required_role}",
                        location="beats",
                    )
                )
        if roles and roles[0] != "teaser_intent":
            findings.append(
                Finding(
                    code="first_beat_not_teaser",
                    message="first beat must be teaser_intent",
                    location="beats[0]",
                )
            )
    else:
        for required_role in ("escalation", "payoff"):
            if required_role not in roles:
                findings.append(
                    Finding(
                        code="missing_required_role",
                        message=f"missing required beat {required_role}",
                        location="beats",
                    )
                )
        if "teaser_intent" in roles:
            findings.append(
                Finding(
                    code="teaser_intent_in_mode_none",
                    message=(
                        "teaser_contract.mode=none is incompatible with a "
                        "teaser_intent beat"
                    ),
                    location="beats",
                )
            )
    if not ({"orientation", "setup"} & set(roles)):
        findings.append(
            Finding(
                code="missing_orientation_or_setup",
                message="requires orientation or setup",
                location="beats",
            )
        )
    return findings


def _check_script_end_hook(
    payloads: Mapping[str, Any],
) -> list[Finding]:
    script = payloads["story_script"]
    beats = script.get("beats") or []
    roles = [beat.get("role") for beat in beats if isinstance(beat, dict)]
    hook = script.get("ending_hook_intent") or {}
    if hook.get("may_be_empty") is False and (
        not roles or roles[-1] != "end_hook"
    ):
        return [
            Finding(
                code="last_beat_not_end_hook",
                message="last beat must be end_hook",
                location=f"beats[{len(roles) - 1}]" if roles else "beats",
                suggestion=(
                    "确无合法 Hook 时把 ending_hook_intent.may_be_empty 置为 "
                    "true 并省略 end_hook Beat"
                ),
            )
        ]
    return []


def _check_story_granularity_broad(
    payloads: Mapping[str, Any],
) -> list[Finding]:
    script = payloads["story_script"]
    granularity = script.get("story_granularity")
    if granularity != "broad":
        return [
            Finding(
                code="granularity_not_broad",
                message=(
                    f"story_granularity 必须为 broad, 实际 {granularity!r}; "
                    "缺少该标记的 Story 产物不再兼容"
                ),
                location="story_granularity",
                suggestion="从 Broad Story Catalog 重新生成",
            )
        ]
    return []


def _check_beat_concrete_content(
    payloads: Mapping[str, Any],
) -> list[Finding]:
    script = payloads["story_script"]
    findings: list[Finding] = []
    for index, beat in enumerate(script.get("beats") or []):
        if not isinstance(beat, dict):
            continue
        if is_abstract_only(beat.get("concrete_story_content")):
            findings.append(
                Finding(
                    code="abstract_story_content",
                    message=(
                        f"beats[{index}].concrete_story_content "
                        "is abstract or too vague"
                    ),
                    location=f"beats[{index}].concrete_story_content",
                    suggestion="必须包含可观察的具体内容, 不得使用抽象 Logline",
                )
            )
    return findings


# ═══════════════════════════════════════════════════════════════════════════
# series_bible 组 — SKILL.md rule 3 / 12
# ═══════════════════════════════════════════════════════════════════════════


def _check_thread_kind_required(
    payloads: Mapping[str, Any],
) -> list[Finding]:
    bible = payloads["series_bible"]
    findings: list[Finding] = []
    for index, thread in enumerate(bible.get("story_threads") or []):
        if not isinstance(thread, dict):
            continue
        kind = thread.get("thread_kind")
        if kind not in ("arc", "coda"):
            findings.append(
                Finding(
                    code="thread_kind_missing",
                    message=(
                        f"story_threads[{index}] thread_kind 必填且只能为 "
                        f"arc|coda, 实际 {kind!r}"
                    ),
                    location=f"story_threads[{index}].thread_kind",
                )
            )
    return findings


#: coda Beat 允许的 terminal 角色/phase。
_CODA_TERMINAL_ROLES = frozenset({"payoff", "consequence", "coda"})


def _check_coda_beat_limits(
    payloads: Mapping[str, Any],
) -> list[Finding]:
    bible = payloads["series_bible"]
    beats_by_thread: dict[str, list[dict[str, Any]]] = {}
    for beat in bible.get("thread_beats") or []:
        if isinstance(beat, dict) and isinstance(beat.get("thread_id"), str):
            beats_by_thread.setdefault(beat["thread_id"], []).append(beat)
    findings: list[Finding] = []
    for index, thread in enumerate(bible.get("story_threads") or []):
        if not isinstance(thread, dict) or thread.get("thread_kind") != "coda":
            continue
        thread_id = thread.get("id", f"story_threads[{index}]")
        beats = beats_by_thread.get(thread_id, [])
        if not 1 <= len(beats) <= 2:
            findings.append(
                Finding(
                    code="coda_beat_count",
                    message=(
                        f"typed coda {thread_id} 只能包含 1–2 个 Beat, "
                        f"实际 {len(beats)}"
                    ),
                    location=f"story_threads[{index}]",
                )
            )
            continue
        phases = {beat.get("phase") for beat in beats}
        if "coda" not in phases:
            findings.append(
                Finding(
                    code="coda_phase_missing",
                    message=f"typed coda {thread_id} 至少一个 Beat 必须为 phase=coda",
                    location=f"story_threads[{index}]",
                )
            )
        non_terminal = [
            beat.get("id", f"beat[{i}]")
            for i, beat in enumerate(beats)
            if beat.get("phase") not in _CODA_TERMINAL_ROLES
        ]
        if non_terminal:
            findings.append(
                Finding(
                    code="coda_non_terminal_beat",
                    message=(
                        f"typed coda {thread_id} 含非 terminal Beat: {non_terminal}"
                    ),
                    location=f"story_threads[{index}]",
                )
            )
    return findings


def _check_resolved_setup_payoff(
    payloads: Mapping[str, Any],
) -> list[Finding]:
    bible = payloads["series_bible"]
    phase_by_thread: dict[str, set[str]] = {}
    for beat in bible.get("thread_beats") or []:
        if not isinstance(beat, dict):
            continue
        thread_id = beat.get("thread_id")
        phase = beat.get("phase")
        if isinstance(thread_id, str) and isinstance(phase, str):
            phase_by_thread.setdefault(thread_id, set()).add(phase)
    findings: list[Finding] = []
    for index, thread in enumerate(bible.get("story_threads") or []):
        if not isinstance(thread, dict):
            continue
        if thread.get("status") != "resolved":
            continue
        if thread.get("thread_kind") == "coda":
            continue
        phases = phase_by_thread.get(thread.get("id", ""), set())
        if not {"setup", "payoff"} <= phases:
            findings.append(
                Finding(
                    code="resolved_thread_lacks_setup_payoff",
                    message=(
                        f"story_threads[{index}] is resolved but lacks "
                        "setup/payoff Thread Beats"
                    ),
                    location=f"story_threads[{index}]",
                    suggestion="arc + resolved 缺 setup/payoff 必须硬停止, 不得静默降级",
                )
            )
    return findings


# ═══════════════════════════════════════════════════════════════════════════
# story_plan 组 — SKILL.md rule 20 / 21 / 22
# ═══════════════════════════════════════════════════════════════════════════


def _check_plan_duration_cap(
    payloads: Mapping[str, Any],
) -> list[Finding]:
    plan = payloads["story_plan"]
    duration = _as_number(plan.get("playback_duration"))
    if duration is not None and duration > STORY_DURATION_HARD_CAP_SECONDS:
        return [
            Finding(
                code="plan_duration_over_cap",
                message=(
                    f"Story Plan 播放时长 {duration:.3f}s 超过硬上限 "
                    f"{STORY_DURATION_HARD_CAP_SECONDS:g}s"
                ),
                location="playback_duration",
                suggestion="Plan 阶段不设时长下限, 只保留 1200s 硬上限",
            )
        ]
    return []


def _check_repeat_ratio_cap(
    payloads: Mapping[str, Any],
) -> list[Finding]:
    plan = payloads["story_plan"]
    ratio = _as_number(plan.get("repeat_ratio"))
    if ratio is None:
        playback = _as_number(plan.get("playback_duration"))
        unique = _as_number(plan.get("merged_unique_source_duration"))
        if playback and playback > 0 and unique is not None:
            ratio = (playback - unique) / playback
    if ratio is not None and ratio > REPEAT_RATIO_HARD_CAP:
        return [
            Finding(
                code="repeat_ratio_over_cap",
                message=(
                    f"repeat_ratio {ratio:.4f} 超过硬合同 "
                    f"{REPEAT_RATIO_HARD_CAP:.0%}"
                ),
                location="repeat_ratio",
                suggestion="Compiler 请求前过滤, materializer 用同一公式复验",
            )
        ]
    return []


def _plan_mode(plan: Mapping[str, Any]) -> str:
    mode = plan.get("mode")
    if isinstance(mode, str) and mode:
        return mode
    return "single_highlight"


def _check_first_block_contract(
    payloads: Mapping[str, Any],
) -> list[Finding]:
    plan = payloads["story_plan"]
    blocks = plan.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        return [
            Finding(code="blocks_missing", message="Story Plan must declare blocks")
        ]
    first = blocks[0] if isinstance(blocks[0], dict) else {}
    mode = _plan_mode(plan)
    findings: list[Finding] = []
    if mode == "single_highlight":
        if first.get("kind") != "teaser":
            findings.append(
                Finding(
                    code="first_block_not_teaser",
                    message="single_highlight 的第一 Block 必须是未来高光 Teaser",
                    location="blocks[0]",
                )
            )
        else:
            if first.get("orientation") != "teaser":
                findings.append(
                    Finding(
                        code="first_block_orientation",
                        message="首 Block orientation 由编译器固定为 teaser",
                        location="blocks[0].orientation",
                    )
                )
            if first.get("temporal_position") != "start":
                findings.append(
                    Finding(
                        code="first_block_temporal_position",
                        message="首 Block temporal_position 由编译器固定为 start",
                        location="blocks[0].temporal_position",
                    )
                )
            if first.get("reuse_mode", "none") != "none":
                findings.append(
                    Finding(
                        code="first_block_reuse_mode",
                        message="首 Block reuse_mode 由编译器固定为 none",
                        location="blocks[0].reuse_mode",
                    )
                )
    else:
        if first.get("kind") == "teaser":
            findings.append(
                Finding(
                    code="mode_none_has_teaser_first",
                    message="mode=none 的第一 Block 必须是正文且不得出现 Teaser Block",
                    location="blocks[0]",
                )
            )
        if any(
            isinstance(block, dict) and block.get("kind") == "teaser"
            for block in blocks
        ):
            findings.append(
                Finding(
                    code="mode_none_teaser_block",
                    message="mode=none 不得出现任何 Teaser Block",
                    location="blocks",
                )
            )
    return findings


def _check_teaser_single_clip(
    payloads: Mapping[str, Any],
) -> list[Finding]:
    plan = payloads["story_plan"]
    blocks = plan.get("blocks") or []
    findings: list[Finding] = []
    if _plan_mode(plan) == "single_highlight":
        teaser_blocks = [
            block for block in blocks
            if isinstance(block, dict) and block.get("kind") == "teaser"
        ]
        if len(teaser_blocks) != 1:
            findings.append(
                Finding(
                    code="teaser_block_count",
                    message=f"Teaser 有且只有一个 Block, 实际 {len(teaser_blocks)}",
                    location="blocks",
                )
            )
        for block in teaser_blocks:
            clips = block.get("clips") or []
            if len(clips) != 1:
                findings.append(
                    Finding(
                        code="teaser_clip_count",
                        message=f"Teaser Block 必须恰好物化一个 Clip, 实际 {len(clips)}",
                        location="blocks[teaser].clips",
                    )
                )
    # 同 Block 内不得复用同一 span_candidate_id (Teaser↔body 或 body↔body 跨 Block 可复用)
    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            continue
        seen: set[str] = set()
        for clip in block.get("clips") or []:
            candidate_id = (
                clip.get("span_candidate_id") if isinstance(clip, dict) else None
            )
            if not isinstance(candidate_id, str):
                continue
            if candidate_id in seen:
                findings.append(
                    Finding(
                        code="intra_block_candidate_reuse",
                        message=(
                            f"blocks[{index}] 内复用 span_candidate_id "
                            f"{candidate_id}; 同 Block 内不得复用"
                        ),
                        location=f"blocks[{index}].clips",
                    )
                )
            seen.add(candidate_id)
    return findings


def _check_full_episode_guard(
    payloads: Mapping[str, Any],
) -> list[Finding]:
    plan = payloads["story_plan"]
    playback = _as_number(plan.get("playback_duration")) or 0.0
    full_clips = 0
    full_seconds = 0.0
    for block in plan.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        for clip in block.get("clips") or []:
            if not isinstance(clip, dict) or not clip.get("is_full_episode"):
                continue
            full_clips += 1
            duration = _clip_duration(clip)
            if duration is not None:
                full_seconds += duration
    findings: list[Finding] = []
    if full_clips >= 2:
        findings.append(
            Finding(
                code="full_episode_clip_count",
                message=f"两条及以上整集型 Clip ({full_clips}) 阻断",
                location="blocks[].clips",
            )
        )
    if playback > 0:
        ratio = full_seconds / playback
        if ratio > FULL_EPISODE_RATIO_BLOCK:
            findings.append(
                Finding(
                    code="full_episode_ratio_block",
                    message=f"整集型播放占比 {ratio:.1%} 超过 50% 阻断",
                    location="blocks[].clips",
                )
            )
        elif ratio > FULL_EPISODE_RATIO_WARN:
            findings.append(
                Finding(
                    code="full_episode_ratio_warn",
                    message=f"整集型播放占比 {ratio:.1%} 接近 50% 阻断线",
                    location="blocks[].clips",
                    severity="warning",
                )
            )
    return findings


# ═══════════════════════════════════════════════════════════════════════════
# qc_admission 组 — SKILL.md rule 23
# ═══════════════════════════════════════════════════════════════════════════


def _check_qc_admission_blocked_reasons(
    payloads: Mapping[str, Any],
) -> list[Finding]:
    admission = payloads["qc_admission"]
    decision = admission.get("decision")
    if decision != "accepted_for_qc":
        return []
    blocked_reasons = {
        reason
        for reason in admission.get("blocked_reasons") or []
        if isinstance(reason, str)
    }
    forbidden = blocked_reasons & NEVER_ADMIT_BLOCKED_REASONS
    if forbidden:
        return [
            Finding(
                code="never_admit_blocked_reason",
                message=(
                    f"blocked reasons {sorted(forbidden)} 永远不得 Admission "
                    "(对白/同源因果连续性硬错误或缺少当前 continuity 合同)"
                ),
                location="blocked_reasons",
                suggestion="重选素材或生成新的基础 Story Plan 后重新 QC",
            )
        ]
    return []


# ═══════════════════════════════════════════════════════════════════════════
# render_recipe 组 — SKILL.md rule 32
# ═══════════════════════════════════════════════════════════════════════════


def _check_render_transition_policy(
    payloads: Mapping[str, Any],
) -> list[Finding]:
    recipe = payloads["render_recipe"]
    mode = recipe.get("mode") or "single_highlight"
    black_frames = [
        transition
        for transition in recipe.get("transitions") or []
        if isinstance(transition, dict)
        and transition.get("kind") == "black_frame_silence"
    ]
    if mode == "single_highlight":
        findings: list[Finding] = []
        if len(black_frames) != 1:
            findings.append(
                Finding(
                    code="black_frame_count",
                    message=(
                        f"single_highlight 只在 Teaser—正文边界插入一次黑场, "
                        f"实际 {len(black_frames)} 次"
                    ),
                    location="transitions",
                )
            )
        for transition in black_frames:
            if transition.get("position") != "teaser_to_body":
                findings.append(
                    Finding(
                        code="black_frame_position",
                        message="黑场只能位于完整 Teaser Block 与正文第一个 Block 之间",
                        location="transitions",
                    )
                )
            seconds = _as_number(transition.get("seconds"))
            if seconds is None or abs(seconds - TEASER_TO_BODY_BLACK_FRAME_SECONDS) > 1e-3:
                findings.append(
                    Finding(
                        code="black_frame_duration",
                        message=(
                            "黑场静音必须为 "
                            f"{TEASER_TO_BODY_BLACK_FRAME_SECONDS:g} 秒, "
                            f"实际 {seconds!r}"
                        ),
                        location="transitions",
                    )
                )
        return findings
    if black_frames:
        return [
            Finding(
                code="mode_none_black_frame",
                message="mode=none 不生成 Teaser Block、黑场或对应 fade",
                location="transitions",
            )
        ]
    return []


# ═══════════════════════════════════════════════════════════════════════════
# 规则表
# ═══════════════════════════════════════════════════════════════════════════

BUILTIN_RULES: tuple[Rule, ...] = (
    # ── story_script ────────────────────────────────────────────────
    Rule(
        rule_id="rule_07_script_beat_count",
        check_fn=_check_script_beat_count,
        description="Broad Story Script beats 数量为 4–14",
        group="story_script",
        source="SKILL.md 固定合同 rule 7",
    ),
    Rule(
        rule_id="rule_06_script_required_roles",
        check_fn=_check_script_required_roles,
        description=(
            "Story Script 结构合法条件因 teaser 模式而异: single_highlight 以 "
            "teaser_intent 起手, mode=none 首 beat 是正文角色; 必含 "
            "escalation/payoff 与 orientation|setup"
        ),
        group="story_script",
        source="SKILL.md 固定合同 rule 6",
    ),
    Rule(
        rule_id="rule_06_script_end_hook",
        check_fn=_check_script_end_hook,
        description="ending_hook_intent.may_be_empty=false 时末 Beat 必须为 end_hook",
        group="story_script",
        source="SKILL.md 固定合同 rule 6",
    ),
    Rule(
        rule_id="rule_08_story_granularity_broad",
        check_fn=_check_story_granularity_broad,
        description="Story 产物必须携带 story_granularity=broad 身份标记",
        group="story_script",
        source="SKILL.md 固定合同 rule 8 / 版本说明",
    ),
    Rule(
        rule_id="rule_04_beat_concrete_content",
        check_fn=_check_beat_concrete_content,
        description="每个 Beat 必须包含可观察的具体内容, 不得是抽象 Logline",
        group="story_script",
        source="SKILL.md 固定合同 rule 4 / rule 11",
    ),
    # ── series_bible ────────────────────────────────────────────────
    Rule(
        rule_id="rule_12_thread_kind_required",
        check_fn=_check_thread_kind_required,
        description="Registry 每条 Thread 必填 thread_kind=arc|coda",
        group="series_bible",
        source="SKILL.md 固定合同 rule 12",
    ),
    Rule(
        rule_id="rule_12_coda_beat_limits",
        check_fn=_check_coda_beat_limits,
        description=(
            "typed coda 只能包含 1–2 个 payoff/consequence/coda Beat, "
            "且至少一个 phase=coda"
        ),
        group="series_bible",
        source="SKILL.md 固定合同 rule 12",
    ),
    Rule(
        rule_id="rule_03_resolved_setup_payoff",
        check_fn=_check_resolved_setup_payoff,
        description="arc 且 resolved 的 Thread 必须含 setup 与 payoff Beat",
        group="series_bible",
        source="SKILL.md 固定合同 rule 3 / 失败与恢复",
    ),
    # ── story_plan ──────────────────────────────────────────────────
    Rule(
        rule_id="rule_22_plan_duration_cap",
        check_fn=_check_plan_duration_cap,
        description="Story Plan 不设时长下限, 只保留 1200 秒硬上限",
        group="story_plan",
        source="SKILL.md 固定合同 rule 7 / rule 22",
    ),
    Rule(
        rule_id="rule_21_repeat_ratio_cap",
        check_fn=_check_repeat_ratio_cap,
        description="Partition 最终硬合同 repeat_ratio ≤ 10%",
        group="story_plan",
        source="SKILL.md 固定合同 rule 21",
    ),
    Rule(
        rule_id="rule_20_first_block_contract",
        check_fn=_check_first_block_contract,
        description=(
            "首 Block 的 teaser/start/reuse_mode=none 由编译器固定; "
            "mode=none 不得出现 Teaser Block"
        ),
        group="story_plan",
        source="SKILL.md 固定合同 rule 20",
    ),
    Rule(
        rule_id="rule_21_teaser_single_clip",
        check_fn=_check_teaser_single_clip,
        description="Teaser 有且只有一个 Clip; 同 Block 内不得复用 Candidate",
        group="story_plan",
        source="SKILL.md 固定合同 rule 20 / rule 21",
    ),
    Rule(
        rule_id="rule_22_full_episode_guard",
        check_fn=_check_full_episode_guard,
        severity="error",
        description="两条及以上整集型 Clip 或整集型播放占比超过 50% 阻断",
        group="story_plan",
        source="SKILL.md 固定合同 rule 22",
    ),
    # ── qc_admission ────────────────────────────────────────────────
    Rule(
        rule_id="rule_23_qc_admission_never",
        check_fn=_check_qc_admission_blocked_reasons,
        description=(
            "dialogue_incomplete / same_source_causal_gap / 缺少当前 continuity "
            "合同的旧 Plan 永远不得 Admission"
        ),
        group="qc_admission",
        source="SKILL.md 固定合同 rule 23",
    ),
    # ── render_recipe ───────────────────────────────────────────────
    Rule(
        rule_id="rule_32_render_transition_policy",
        check_fn=_check_render_transition_policy,
        description=(
            "single_highlight 只在 Teaser—正文边界插入一次 0.35 秒黑场静音; "
            "mode=none 不生成"
        ),
        group="render_recipe",
        source="SKILL.md 固定合同 rule 32",
    ),
)
