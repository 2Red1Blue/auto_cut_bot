"""PySceneDetect 边界修正与 VLM 结果融合。

将 VLM 分析结果中的事件时间戳对齐到 PySceneDetect 检测到的镜头边界，
提升时间精度。

支持两种 scene_boundaries 格式:
- v1.1 (场景范围): {"episodes": {"1": [[0.0, 2.68], [2.68, 3.96], ...]}}
- v1.0 (切点列表): {"episodes": {"1": [0.0, 2.68, 3.96, ...]}}

修正后的事件时间戳会更新到 result["start"]/result["end"] 字段，
原始值保留在 result["original_start"]/result["original_end"]。
"""

from __future__ import annotations

from typing import Any

from autocut_core.logging import get_logger

# ASR anchor three-tier cascade (optional import)
try:
    from autocut_core.audio.asr_anchor import (
        AudioAnchorResult,
        three_tier_snap_start as _asr_tier_snap_start,
        three_tier_snap_end as _asr_tier_snap_end,
    )
    _HAS_ASR_ANCHOR = True
except ImportError:
    _HAS_ASR_ANCHOR = False
    AudioAnchorResult = None  # type: ignore


logger = get_logger(__name__)


# ── 可配置多模态高光打分排序模块 ──────────────────────────────────────
import json as _json
from pathlib import Path as _Path
from collections import defaultdict as _defaultdict


def _load_scoring_config() -> dict:
    """加载外置打分配置，找不到就用默认值。"""
    default_config: dict = {
        "weights": {
            "vlm_visual_score": 0.5,
            "semantic_score": 0.35,
            "global_narrative_score": 0.15,
        },
        "visual_keywords": [
            {"keywords": ["黑翼", "神力", "神火", "爆发", "变身", "全屏闪光", "冲击波"], "score": 10, "tags": ["名场面", "爽点", "视觉奇观"]},
            {"keywords": ["怒吼", "宣战", "决裂", "崩溃", "哭", "牺牲", "死亡", "重逢", "告白", "对峙"], "score": 9, "tags": ["情感高光", "名场面"]},
            {"keywords": ["揭露", "反转", "真相", "决定", "承诺", "发现"], "score": 7, "tags": ["剧情点", "反转"]},
            {"keywords": ["对话", "走路", "过场", "铺垫"], "score": 4, "tags": ["普通剧情"]},
        ],
        "special_tags": [
            {"keywords": ["刺中", "打斗", "战斗", "打飞", "击退"], "tags": ["动作爽点"]},
            {"keywords": ["闪回", "回忆", "过去"], "tags": ["闪回"]},
            {"keywords": ["项链", "吊坠", "三周年"], "tags": ["伏笔回收"]},
        ],
        "vlm_candidate_guarantee": {"enabled": True, "min_vlm_strength": 7},
    }
    config_path = _Path(__file__).parent.parent / "config" / "highlight_scoring.yaml"
    if config_path.exists():
        try:
            import yaml
            with open(config_path, "r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f)
                if loaded and isinstance(loaded, dict):
                    for k, v in default_config.items():
                        if k not in loaded:
                            loaded[k] = v
                    # 校验权重之和
                    w = loaded.get("weights", {})
                    w_sum = sum(w.values())
                    if abs(w_sum - 1.0) > 0.01:
                        logger.warning(
                            "highlight_scoring weights sum=%.3f (expected 1.0), auto-normalizing",
                            w_sum,
                        )
                        if w_sum > 0:
                            loaded["weights"] = {k: v / w_sum for k, v in w.items()}
                    return loaded
        except Exception as e:
            logger.warning("加载打分配置失败，用默认值: %s", e)
    return default_config


_SCORING_CONFIG = _load_scoring_config()


def _load_vlm_summaries(job_root: "_Path | str | None") -> "dict[int, list[dict]]":
    """加载 VLM window summaries，按集数聚合。无 VLM 数据时返回空字典。"""
    if not job_root:
        return {}
    summary_path = _Path(job_root) / "window-summaries.jsonl"
    if not summary_path.exists():
        return {}
    ep_data: dict[int, list[dict]] = _defaultdict(list)
    try:
        with open(summary_path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                w = _json.loads(line)
                ep = w.get("episode")
                if not ep:
                    continue
                for beat in w.get("story_beats", []):
                    ep_data[ep].append({
                        "start": beat.get("start", 0),
                        "end": beat.get("end", 0),
                        "text": beat.get("summary", ""),
                        "function": beat.get("function", ""),
                    })
                for cand in w.get("candidates", []):
                    ep_data[ep].append({
                        "start": cand.get("start", 0),
                        "end": cand.get("end", 0),
                        "text": cand.get("reason", cand.get("summary", "")),
                        "vlm_candidate_score": cand.get("strength", 5),
                        "is_vlm_candidate": True,
                    })
                for seg in w.get("timeline_segments", []):
                    if seg.get("mode") in ("flashback", "recall"):
                        ep_data[ep].append({"type": "flashback", "start": seg.get("start", 0)})
    except Exception as e:
        logger.warning("加载VLM摘要失败，降级为纯文本模式: %s", e)
        return {}
    return ep_data


def _calc_visual_score(
    start: float,
    ep: int,
    vlm_data: "dict[int, list[dict]]",
) -> tuple[float, list[str]]:
    """计算视觉冲击分 (0-10) + 标签列表。"""
    tags: set[str] = set()
    max_score = 0.0
    if ep not in vlm_data:
        return 5.0, []
    beats = vlm_data[ep]
    for beat in beats:
        if beat.get("type") == "flashback" and abs(beat.get("start", 0) - start) < 30:
            tags.add("闪回")
            continue
        b_start = beat.get("start", 0)
        b_end = beat.get("end", b_start + 10)
        if not (b_start - 15 <= start <= b_end + 15):
            continue
        if beat.get("is_vlm_candidate"):
            max_score = max(max_score, beat.get("vlm_candidate_score", 5))
        else:
            beat_text = beat.get("text", "") + beat.get("function", "")
            for kw_group in _SCORING_CONFIG.get("visual_keywords", []):
                if any(kw in beat_text for kw in kw_group.get("keywords", [])):
                    max_score = max(max_score, kw_group.get("score", 5))
                    tags.update(kw_group.get("tags", []))
                    break
        for tag_group in _SCORING_CONFIG.get("special_tags", []):
            if any(kw in beat.get("text", "") for kw in tag_group.get("keywords", [])):
                tags.update(tag_group.get("tags", []))
    final_score = min(10.0, max_score if max_score > 0 else 5.0)
    return final_score, sorted(tags)


def multimodal_score_highlight(
    highlight: dict,
    ep: int,
    vlm_data: "dict[int, list[dict]]",
    global_turning_points: "list[dict] | None" = None,
) -> tuple[float, list[str]]:
    """多模态高光打分: VLM视觉分 + 语义分 + 全局叙事分。

    Returns:
        (加权总分, 标签列表)
    """
    weights = _SCORING_CONFIG.get("weights", {})
    w_vlm = weights.get("vlm_visual_score", 0.5)
    w_sem = weights.get("semantic_score", 0.35)
    w_global = weights.get("global_narrative_score", 0.15)

    semantic_score = float(highlight.get("strength", 5))
    visual_score, tags_list = _calc_visual_score(highlight.get("start", 0), ep, vlm_data)
    tags: set[str] = set(tags_list)

    narrative_score = 5.0
    if global_turning_points:
        for tp in global_turning_points:
            if abs(tp.get("ep", 0) - ep) <= 1 and abs(tp.get("time", 0) - highlight.get("start", 0)) < 60:
                narrative_score = 10.0
                tags.add("全局转折点")
                break

    dialogue_len = len(highlight.get("dialogue_excerpt", "").strip())
    if dialogue_len < 8 and visual_score >= 7:
        tags.add("动作爽点")
    elif dialogue_len >= 8 and visual_score >= 7:
        tags.add("情感高光")
    if not tags:
        tags.add("剧情点")

    total_score = semantic_score * w_sem + visual_score * w_vlm + narrative_score * w_global
    return round(total_score, 2), sorted(tags)


# ── 静音区间辅助函数 ──────────────────────────────────────────────


def _extract_silence_intervals(
    silence_data: dict[str, Any] | None,
    episode_id: str,
) -> list[dict[str, float]]:
    """从 silence_intervals 数据中提取指定集的静音区间列表。

    Args:
        silence_data: silence_intervals 产物 (包含 episodes 字段)
        episode_id: 集数 ID

    Returns:
        静音区间列表 [{"start": float, "end": float, "duration": float}, ...]
        按 start 排序；无数据时返回空列表。
    """
    if not silence_data or not isinstance(silence_data, dict):
        return []
    episodes = silence_data.get("episodes", {})
    intervals = episodes.get(str(episode_id), [])
    if not isinstance(intervals, list):
        return []
    return intervals


def _is_in_silence(
    timestamp: float,
    silence_intervals: list[dict[str, float]],
    margin: float = 0.05,
) -> bool:
    """检查时间戳是否落在某个静音区间内（含 margin 容差）。

    Args:
        timestamp: 待检查的时间点
        silence_intervals: 静音区间列表
        margin: 前后容差 (秒)，默认 0.05s

    Returns:
        True 如果 timestamp 在静音区间内
    """
    for iv in silence_intervals:
        if iv["start"] - margin <= timestamp <= iv["end"] + margin:
            return True
    return False


def _find_safe_snap_point_start(
    candidate_time: float,
    silence_intervals: list[dict[str, float]],
    max_shift: float = 3.0,
    lead_in: float = 0.3,
    *,
    original_timestamp: float | None = None,
) -> float | None:
    """为高光开始时间寻找安全的 snap 目标点。

    从 candidate_time 向前搜索 ≤max_shift 范围，寻找一个静音区间的
    结束点（语音起始处），返回 end_of_silence + lead_in。

    策略：向前找静音结束点 = 语音开始前的安静点，这是最自然的切点。

    Args:
        candidate_time: 候选 snap 时间（切点+lead_in 后的位置）
        silence_intervals: 静音区间列表
        max_shift: 相对于原始 VLM 时间戳的最大偏移距离 (秒)
        lead_in: lead_in 偏移量
        original_timestamp: 原始 VLM 时间戳（用于约束 max_shift 边界）；
            为 None 时以 candidate_time 为基准（向后兼容）

    Returns:
        安全的 snap 目标时间，或 None 如果找不到合适的静音间隙
    """
    # max_shift 相对于原始 VLM timestamp 约束
    anchor = original_timestamp if original_timestamp is not None else candidate_time
    search_start = anchor - max_shift
    search_end = anchor + 0.5  # 允许稍微向后一点

    # 找所有落在搜索范围内的静音区间，优先选最靠近 candidate_time 的
    candidates = []
    for iv in silence_intervals:
        iv_start = iv["start"]
        iv_end = iv["end"]
        # 静音区间的结束点在搜索范围内
        if search_start <= iv_end <= search_end:
            candidates.append(iv)

    if not candidates:
        return None

    # 选择距离 candidate_time 最近的静音结束点（但不超过 candidate_time 太多）
    # 优先选在 candidate_time 之前的静音结束点（避免把高光推迟太多）
    # 同时最终结果必须在 anchor ± max_shift 范围内
    best = None
    best_dist = float("inf")
    for iv in candidates:
        target = iv["end"] + lead_in
        # 硬约束：最终结果不超过 anchor ± max_shift
        if abs(target - anchor) > max_shift:
            continue
        dist = abs(target - candidate_time)
        # 如果静音结束点在 candidate_time 之前，这是最理想的（语音开始处）
        if iv["end"] <= candidate_time + 0.1:
            if dist < best_dist:
                best_dist = dist
                best = target
        elif best is None:
            # 如果没有之前的点，才考虑之后的
            if dist < best_dist:
                best_dist = dist
                best = target

    return best


def _find_safe_snap_point_end(
    candidate_time: float,
    silence_intervals: list[dict[str, float]],
    max_shift: float = 3.0,
    lead_out: float = 0.0,
    *,
    original_timestamp: float | None = None,
) -> float | None:
    """为高光结束时间寻找安全的 snap 目标点。

    从 candidate_time 向后搜索 ≤max_shift 范围，寻找一个静音区间的
    开始点（语音结束处），返回 start_of_silence + lead_out。

    Args:
        candidate_time: 候选 snap 时间
        silence_intervals: 静音区间列表
        max_shift: 相对于原始 VLM 时间戳的最大偏移距离 (秒)
        lead_out: lead_out 偏移量
        original_timestamp: 原始 VLM 时间戳（用于约束 max_shift 边界）；
            为 None 时以 candidate_time 为基准（向后兼容）

    Returns:
        安全的 snap 目标时间，或 None 如果找不到合适的静音间隙
    """
    # max_shift 相对于原始 VLM timestamp 约束
    anchor = original_timestamp if original_timestamp is not None else candidate_time
    search_start = anchor - 0.5
    search_end = anchor + max_shift

    candidates = []
    for iv in silence_intervals:
        iv_start = iv["start"]
        iv_end = iv["end"]
        # 静音区间的开始点在搜索范围内
        if search_start <= iv_start <= search_end:
            candidates.append(iv)

    if not candidates:
        return None

    best = None
    best_dist = float("inf")
    for iv in candidates:
        target = iv["start"] + lead_out
        # 硬约束：最终结果不超过 anchor ± max_shift
        if abs(target - anchor) > max_shift:
            continue
        dist = abs(target - candidate_time)
        if iv["start"] >= candidate_time - 0.1:
            if dist < best_dist:
                best_dist = dist
                best = target
        elif best is None:
            if dist < best_dist:
                best_dist = dist
                best = target

    return best


def validate_scene_boundaries(
    scene_boundaries: dict[str, Any],
    *,
    min_scene_duration: float = 1.0,
    max_scene_duration: float = 600.0,
    max_gap_ratio: float = 0.3,
) -> dict[str, Any]:
    """验证场景边界数据的质量。
    
    检查项：
    1. 数据格式是否正确 (v1.0 或 v1.1)
    2. 场景数量是否合理
    3. 场景时长是否合理 (不过短或过长)
    4. 场景覆盖是否连续 (gap 比例)
    
    Args:
        scene_boundaries: PySceneDetect 场景边界数据
        min_scene_duration: 最小场景时长 (秒)，低于此值视为异常
        max_scene_duration: 最大场景时长 (秒)，高于此值视为异常
        max_gap_ratio: 最大 gap 比例 (0-1)，超过此比例视为覆盖不足
    
    Returns:
        验证结果字典:
        {
            "valid": bool,           # 整体是否有效
            "issues": list[str],     # 发现的问题列表
            "stats": dict,           # 统计信息
            "episodes_quality": dict # 每集的质量信息
        }
    """
    result = {
        "valid": True,
        "issues": [],
        "stats": {
            "total_episodes": 0,
            "total_scenes": 0,
        },
        "episodes_quality": {},
    }
    
    if not scene_boundaries or not isinstance(scene_boundaries, dict):
        result["valid"] = False
        result["issues"].append("scene_boundaries 为空或格式错误")
        return result
    
    episodes = scene_boundaries.get("episodes", {})
    if not episodes or not isinstance(episodes, dict):
        result["valid"] = False
        result["issues"].append("episodes 字段缺失或格式错误")
        return result
    
    result["stats"]["total_episodes"] = len(episodes)
    
    for episode_id, scene_data in episodes.items():
        ep_quality = {
            "valid": True,
            "issues": [],
            "scene_count": 0,
            "duration_range": None,
            "gap_ratio": 0.0,
        }
        
        if not scene_data or not isinstance(scene_data, list):
            ep_quality["valid"] = False
            ep_quality["issues"].append(f"episode {episode_id}: 场景数据为空或格式错误")
            result["episodes_quality"][episode_id] = ep_quality
            result["issues"].append(f"episode {episode_id}: 场景数据为空或格式错误")
            continue
        
        # 判断格式 (v1.0 或 v1.1)
        first_item = scene_data[0]
        is_range_format = isinstance(first_item, (list, tuple))
        
        # 提取场景范围
        scene_ranges = []
        if is_range_format:
            # v1.1: [[start, end], ...]
            for item in scene_data:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    try:
                        start, end = float(item[0]), float(item[1])
                        if start < end:
                            scene_ranges.append((start, end))
                    except (ValueError, TypeError):
                        ep_quality["issues"].append(f"无效的场景范围: {item}")
        else:
            # v1.0: [cut_point, ...]，需要转换为范围
            cut_points = []
            for item in scene_data:
                try:
                    cut_points.append(float(item))
                except (ValueError, TypeError):
                    ep_quality["issues"].append(f"无效的切点: {item}")
            
            cut_points = sorted(set(cut_points))
            for i in range(len(cut_points) - 1):
                scene_ranges.append((cut_points[i], cut_points[i + 1]))
        
        ep_quality["scene_count"] = len(scene_ranges)
        result["stats"]["total_scenes"] += len(scene_ranges)
        
        if not scene_ranges:
            ep_quality["valid"] = False
            ep_quality["issues"].append("没有有效的场景范围")
            result["episodes_quality"][episode_id] = ep_quality
            result["issues"].append(f"episode {episode_id}: 没有有效的场景范围")
            continue
        
        # 检查场景时长
        durations = [end - start for start, end in scene_ranges]
        min_dur = min(durations)
        max_dur = max(durations)
        ep_quality["duration_range"] = (min_dur, max_dur)
        
        short_scenes = sum(1 for d in durations if d < min_scene_duration)
        long_scenes = sum(1 for d in durations if d > max_scene_duration)
        
        if short_scenes > 0:
            ep_quality["issues"].append(
                f"{short_scenes} 个场景过短 (< {min_scene_duration}s)"
            )
        
        if long_scenes > 0:
            ep_quality["issues"].append(
                f"{long_scenes} 个场景过长 (> {max_scene_duration}s)"
            )
        
        # 检查覆盖连续性
        scene_ranges_sorted = sorted(scene_ranges, key=lambda x: x[0])
        total_gap = 0.0
        for i in range(1, len(scene_ranges_sorted)):
            prev_end = scene_ranges_sorted[i - 1][1]
            curr_start = scene_ranges_sorted[i][0]
            if curr_start > prev_end:
                total_gap += curr_start - prev_end
        
        total_duration = scene_ranges_sorted[-1][1] - scene_ranges_sorted[0][0]
        gap_ratio = total_gap / total_duration if total_duration > 0 else 0.0
        ep_quality["gap_ratio"] = gap_ratio
        
        if gap_ratio > max_gap_ratio:
            ep_quality["issues"].append(
                f"场景覆盖不连续 (gap 比例 {gap_ratio:.1%} > {max_gap_ratio:.1%})"
            )
        
        # 判断该集是否有效
        if ep_quality["issues"]:
            ep_quality["valid"] = False
            result["issues"].extend(
                f"episode {episode_id}: {issue}" for issue in ep_quality["issues"]
            )
        
        result["episodes_quality"][episode_id] = ep_quality
    
    # 整体有效性
    if result["issues"]:
        result["valid"] = False
    
    return result



# ── 语音区间辅助函数 (VAD: Demucs+Silero) ──────────────────────────


def _extract_speech_intervals(
    speech_data: dict[str, Any] | None,
    episode_id: str,
) -> list[dict[str, float]]:
    """从 VAD speech_intervals 数据中提取指定集的语音区间列表。

    Args:
        speech_data: VAD 产物 (包含 episodes 字段)
        episode_id: 集数 ID

    Returns:
        语音区间列表 [{"start": float, "end": float}, ...]，按 start 排序。
    """
    if not speech_data or not isinstance(speech_data, dict):
        return []
    episodes = speech_data.get("episodes", {})
    intervals = episodes.get(str(episode_id), [])
    if not isinstance(intervals, list):
        return []
    return intervals


def _is_in_speech(
    timestamp: float,
    speech_intervals: list[dict[str, float]],
    pad: float = 0.0,
) -> bool:
    """检查时间戳是否落在某个语音区间内。

    Args:
        timestamp: 待检查的时间点
        speech_intervals: 语音区间列表
        pad: 前后容差 (秒)

    Returns:
        True 如果 timestamp 在语音区间内
    """
    for iv in speech_intervals:
        if iv["start"] - pad <= timestamp <= iv["end"] + pad:
            return True
    return False


def _find_speech_boundary_start(
    cut_point: float,
    speech_intervals: list[dict[str, float]],
    max_shift: float = 5.0,
    speech_lead: float = 0.15,
    *,
    original_timestamp: float | None = None,
    gap_threshold: float = 0.7,
    min_gap_duration: float = 0.25,
    max_cumulative_gap: float = 1.0,
    max_penetration_speech: float = 2.0,
) -> float | None:
    """当切点落在语音中时，寻找安全的高光起始点。

    策略（严格优先级）：
    1. 回退到语音段起点 - speech_lead（优先）
    2. 短间隙穿透：向前穿过短间隙（≤gap_threshold），累计间隙≤max_cumulative_gap，
       找到对话轮次的真正起点（借鉴 v4 snap_teaser_start）
    3. 如果回退后超出预算，搜索前方/后方最近的≥min_gap_duration语音间隙
    所有选项必须在 anchor ± max_shift 范围内。

    Args:
        cut_point: 当前候选切点时间
        speech_intervals: 语音区间列表
        max_shift: 最大调整距离（秒）
        speech_lead: 语音起点前的安全前导时间（0.15s，来自 start_lead_seconds）
        original_timestamp: 原始 VLM 时间戳（作为预算锚点）
        gap_threshold: 短间隙穿透阈值（0.7s），≤此值的呼吸/句间停顿可穿透
        min_gap_duration: 安全切点所需最小间隙（0.25s，比QC的0.35s宽松）
        max_cumulative_gap: 短间隙穿透累计间隙上限（1.0s），防止穿越过多静音
        max_penetration_speech: 穿透最多带回的额外语音时长（2.0s），防止回退过远

    Returns:
        安全时间点，或 None 如果找不到合适位置
    """
    anchor = original_timestamp if original_timestamp is not None else cut_point
    lo = anchor - max_shift
    hi = anchor + max_shift

    sorted_ivs = sorted(speech_intervals, key=lambda x: x["start"])

    # 找到包含 cut_point 的语音段
    containing = None
    containing_idx = -1
    for i, iv in enumerate(sorted_ivs):
        if iv["start"] <= cut_point <= iv["end"]:
            containing = iv
            containing_idx = i
            break

    if containing is not None:
        # Option 1 & 1.5: 从 containing 段开始，向前穿透短间隙找到轮次起点
        # 始终尝试穿透，不只是在 speech_start 超界时
        utterance_start = containing["start"]
        cumulative_gap = 0.0
        penetrated = 0

        total_speech_added = 0.0
        for j in range(containing_idx - 1, -1, -1):
            prev_iv = sorted_ivs[j]
            gap = utterance_start - prev_iv["end"]
            if gap > gap_threshold:
                break  # 真正的轮次/场景边界
            if cumulative_gap + gap > max_cumulative_gap:
                break  # 累计静音太多
            added_speech = prev_iv["end"] - prev_iv["start"]  # 穿透段的语音时长
            if total_speech_added + added_speech > max_penetration_speech:
                break  # 带回太多前置语音
            if prev_iv["start"] - speech_lead < lo:
                break  # 超出预算
            utterance_start = prev_iv["start"]
            cumulative_gap += gap
            total_speech_added += added_speech
            penetrated += 1

        candidate = utterance_start - speech_lead
        if lo <= candidate <= hi:
            return candidate

        # 穿透/回退都超预算 → 尝试找间隙
        # fallthrough to gap search below

    # Option 2 & 3: 寻找附近的安全语音间隙
    # 收集 cut_point 附近（前后 max_shift 范围内）的所有间隙
    candidates: list[tuple[float, float]] = []

    # 构建间隙列表（第一个语音段之前的间隙从0开始）
    gaps: list[tuple[float, float, float]] = []  # (gap_start, gap_end, gap_dur)
    prev_end = 0.0
    for iv in sorted_ivs:
        if iv["start"] > prev_end:
            gaps.append((prev_end, iv["start"], iv["start"] - prev_end))
        prev_end = max(prev_end, iv["end"])
    # 最后一段之后也有无声区
    gaps.append((prev_end, float("inf"), float("inf")))

    for g_start, g_end, g_dur in gaps:
        if g_dur < min_gap_duration:
            continue
        # 间隙在范围内（间隙与搜索窗口有交集）
        if g_end < lo or g_start > hi:
            continue
        # 间隙的安全切点：在间隙中间位置，留 speech_lead 余量
        # 优先使用间隙中最接近 anchor 的位置
        if g_start <= anchor <= g_end:
            # anchor 就在间隙中 — 理想情况
            safe_point = anchor
        elif g_end <= cut_point:
            # 间隙在 cut_point 前方 → 用间隙末尾（离 cut_point 最近的点）
            safe_point = g_end - speech_lead
        else:
            # 间隙在 cut_point 后方 → 用间隙开头
            safe_point = g_start + speech_lead
        safe_point = max(g_start + 0.1, min(g_end - 0.1, safe_point))
        if lo <= safe_point <= hi:
            candidates.append((abs(safe_point - anchor), safe_point))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def _find_speech_boundary_end(
    cut_point: float,
    speech_intervals: list[dict[str, float]],
    max_shift: float = 5.0,
    tail: float = 0.25,
    *,
    original_timestamp: float | None = None,
    gap_threshold: float = 0.7,
    min_gap_duration: float = 0.25,
    max_cumulative_gap: float = 1.0,
    max_penetration_speech: float = 2.0,
) -> float | None:
    """当切点落在语音中时，寻找安全的高光结束点（对称于 start 版本）。

    策略：
    1. 前进到语音段结束 + tail
    2. 短间隙穿透：向后穿过短间隙（≤gap_threshold），累计间隙≤max_cumulative_gap
    3. 搜索附近的语音间隙
    所有选项必须在 anchor ± max_shift 范围内。

    Args:
        cut_point: 当前候选切点时间
        speech_intervals: 语音区间列表
        max_shift: 最大调整距离（秒）
        tail: 语音终点后的安全余量（0.25s，来自 end_tail_seconds）
        original_timestamp: 原始 VLM 时间戳
        gap_threshold: 短间隙穿透阈值（0.7s）
        min_gap_duration: 安全切点所需最小间隙（0.25s）
        max_cumulative_gap: 短间隙穿透累计间隙上限（1.0s）
    """
    anchor = original_timestamp if original_timestamp is not None else cut_point
    lo = anchor - max_shift
    hi = anchor + max_shift

    sorted_ivs = sorted(speech_intervals, key=lambda x: x["start"])

    # 找到包含 cut_point 的语音段
    containing = None
    containing_idx = -1
    for i, iv in enumerate(sorted_ivs):
        if iv["start"] <= cut_point <= iv["end"]:
            containing = iv
            containing_idx = i
            break

    if containing is not None:
        # Option 1 & 1.5: 从 containing 段开始，向后穿透短间隙找到轮次终点
        utterance_end = containing["end"]
        cumulative_gap = 0.0
        penetrated = 0

        total_speech_added = 0.0
        for j in range(containing_idx + 1, len(sorted_ivs)):
            next_iv = sorted_ivs[j]
            gap = next_iv["start"] - utterance_end
            if gap > gap_threshold:
                break
            if cumulative_gap + gap > max_cumulative_gap:
                break
            added_speech = next_iv["end"] - next_iv["start"]
            if total_speech_added + added_speech > max_penetration_speech:
                break
            if next_iv["end"] + tail > hi:
                break
            utterance_end = next_iv["end"]
            cumulative_gap += gap
            total_speech_added += added_speech
            penetrated += 1

        candidate = utterance_end + tail
        if lo <= candidate <= hi:
            return candidate

    # Option 2 & 3: 寻找附近的安全间隙
    candidates: list[tuple[float, float]] = []

    gaps: list[tuple[float, float, float]] = []
    prev_end = 0.0
    for iv in sorted_ivs:
        if iv["start"] > prev_end:
            gaps.append((prev_end, iv["start"], iv["start"] - prev_end))
        prev_end = max(prev_end, iv["end"])
    gaps.append((prev_end, float("inf"), float("inf")))

    for g_start, g_end, g_dur in gaps:
        if g_dur < min_gap_duration:
            continue
        if g_end < lo or g_start > hi:
            continue
        if g_start <= anchor <= g_end:
            safe_point = anchor
        elif g_end <= cut_point:
            safe_point = g_end - tail
        else:
            safe_point = g_start + tail
        safe_point = max(g_start + 0.1, min(g_end - 0.1, safe_point))
        if lo <= safe_point <= hi:
            candidates.append((abs(safe_point - anchor), safe_point))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def apply_scene_boundary_fusion(
    vlm_result: dict[str, Any],
    scene_boundaries: dict[str, Any],
    episode_id: str,
    *,
    snap_tolerance: float = 0.5,
    lead_in: float = 0.3,
    lead_out: float = 0.0,
    silence_intervals: dict[str, Any] | None = None,
    speech_intervals: dict[str, Any] | None = None,
    audio_max_shift: float = 3.0,
    anchor_results: dict[str, Any] | None = None,
    cue_texts: dict[str, str] | None = None,
    fusion_cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """对齐 VLM 结果到 PySceneDetect 边界。

    使用 forward-snap + lead_in 策略（而非 nearest-cut），避免
    将高光开头 snap 到上一个场景的尾部。

    音频门控（silence_intervals 不为 None 时生效）：
    如果候选切点处正在语音中（不在静音区间内），则搜索附近的静音间隙
    作为安全切点；如果 max_shift 范围内找不到静音间隙，则保持 VLM
    原始时间戳（不 snap），避免切点落在半句话中间。

    Args:
        vlm_result: VLM 分析结果 (包含 candidates 列表)
        scene_boundaries: PySceneDetect 场景边界数据
        episode_id: 集数 ID (字符串)
        snap_tolerance: 对齐容差 (秒)，事件时间戳与最近边界的距离超过此值则不对齐
        lead_in: 切点后偏移 (秒)，跳过转场残影/尾音 (默认 0.3)
        lead_out: 切点后偏移 (秒)，结束侧 (默认 0.0)
        silence_intervals: ffmpeg silencedetect 静音区间数据（fallback）
        speech_intervals: VAD 语音区间数据（Demucs+Silero，优先于 silence_intervals）
        audio_max_shift: 音频门控最大调整距离 (秒)，默认 3.0s

    Returns:
        修正后的 vlm_result (原始数据会被修改)
    """
    if not vlm_result or not scene_boundaries:
        return vlm_result

    episodes = scene_boundaries.get("episodes", {})
    if not episodes or episode_id not in episodes:
        return vlm_result

    scene_data = episodes[episode_id]
    if not scene_data:
        return vlm_result

    # 从场景边界提取切点列表 (按时间排序的唯一切点)
    cut_points = extract_cut_points(scene_data)
    if not cut_points:
        return vlm_result

    # 对齐 candidates 列表中的事件
    candidates = vlm_result.get("candidates", [])
    if not isinstance(candidates, list):
        return vlm_result

    # 提取当前集的音频区间（只提取一次，不在循环内重复）
    ep_silence = _extract_silence_intervals(silence_intervals, episode_id) if silence_intervals is not None else None
    ep_speech = _extract_speech_intervals(speech_intervals, episode_id) if speech_intervals is not None else None

    # Extract per-episode anchor result (ASR-anchored AudioAnchorResult or None)
    ep_anchor = None
    if anchor_results and _HAS_ASR_ANCHOR:
        raw = anchor_results.get(episode_id) if isinstance(anchor_results, dict) else None
        if isinstance(raw, AudioAnchorResult):
            ep_anchor = raw

    # Fusion configuration with sensible defaults
    if fusion_cfg is None:
        fusion_cfg = {}

    for event in candidates:
        if not isinstance(event, dict):
            continue

        event_type = event.get("type", "")
        if event_type not in ("highlight", "hook", "scene"):
            continue

        original_start = event.get("start")
        original_end = event.get("end")

        if original_start is None or original_end is None:
            continue

        # Try to get VLM cue text for this event
        event_cue = None
        if cue_texts:
            eid = event.get("id", "")
            event_cue = cue_texts.get(eid) or cue_texts.get(str(original_start))

        # ═══════════════════════════════════════════════════════════
        # Three-tier ASR/VAD/Visual snap (when anchor data available)
        # ═══════════════════════════════════════════════════════════
        if ep_anchor is not None and ep_anchor.status == "ready":
            snap_s = _asr_tier_snap_start(
                original_start, cut_points, ep_anchor,
                lead_in_audio=0.15,
                lead_in_visual=0.05,  # tight visual lead: skip transition artifacts
                visual_lead_window=float(fusion_cfg.get("visual_lead_window", 0.60)),
                visual_follow_window=float(fusion_cfg.get("visual_follow_window", 0.45)),
                search_radius=float(fusion_cfg.get("audio_search_radius", 2.0)),
                max_shift=audio_max_shift,
                cue_text=event_cue,
                micro_crossfade_ms=float(fusion_cfg.get("micro_crossfade_ms", 80)),
            )
            snapped_start = snap_s["final_start"]
            event["snap_start_decision"] = snap_s["decision"]
            event["snap_start_source"] = snap_s["anchor_source"]
            if snap_s.get("needs_fade"):
                event["snap_start_needs_fade"] = True
                event["snap_start_fade_ms"] = snap_s.get("fade_ms", 80)
            if snap_s.get("audio_onset") is not None:
                event["audio_onset_start"] = snap_s["audio_onset"]

            snap_e = _asr_tier_snap_end(
                original_end, cut_points, ep_anchor,
                lead_out_audio=0.10,
                lead_out_visual=0.05,
                visual_tail_window=float(fusion_cfg.get("visual_tail_window_end", 0.60)),
                search_radius=float(fusion_cfg.get("audio_search_radius", 2.0)),
                max_shift=audio_max_shift,
            )
            snapped_end = snap_e["final_end"]
            event["snap_end_decision"] = snap_e["decision"]
            event["snap_end_source"] = snap_e["anchor_source"]
            if snap_e.get("needs_fade"):
                event["snap_end_needs_fade"] = True
                event["snap_end_fade_ms"] = snap_e.get("fade_ms", 80)
            if snap_e.get("audio_end") is not None:
                event["audio_onset_end"] = snap_e["audio_end"]

            # Ensure start < end
            if snapped_start < snapped_end:
                if "original_start" not in event:
                    event["original_start"] = original_start
                if "original_end" not in event:
                    event["original_end"] = original_end
                event["start"] = snapped_start
                event["end"] = snapped_end
                event["snap_method"] = "three_tier_asr"
            continue

        # 使用 forward-snap + lead_in (legacy Demucs/VAD mode)
        # 音频门控：如果切点在语音中，寻找安全静音间隙
        snapped_start = snap_highlight_start(
            original_start, cut_points,
            tolerance=snap_tolerance, lead_in=lead_in,
            silence_intervals=ep_silence,
            speech_intervals=ep_speech,
            max_shift=audio_max_shift,
        )
        snapped_end = snap_highlight_end(
            original_end, cut_points,
            tolerance=snap_tolerance, lead_out=lead_out,
            silence_intervals=ep_silence,
            speech_intervals=ep_speech,
            max_shift=audio_max_shift,
        )

        # 确保对齐后 start < end
        if snapped_start < snapped_end:
            # 只在 original_start 尚未被保存时记录 VLM 原始值
            if "original_start" not in event:
                event["original_start"] = original_start
            if "original_end" not in event:
                event["original_end"] = original_end
            event["start"] = snapped_start
            event["end"] = snapped_end
            # 标注音频门控效果（仅在音频模式启用时）
            if silence_intervals is not None or speech_intervals is not None:
                # 计算纯视觉模式下的 snap 结果用于对比
                visual_start = snap_highlight_start(
                    original_start, cut_points,
                    tolerance=snap_tolerance, lead_in=lead_in,
                )
                visual_end = snap_highlight_end(
                    original_end, cut_points,
                    tolerance=snap_tolerance, lead_out=lead_out,
                )
                audio_start_skipped = abs(snapped_start - original_start) < 0.001 and abs(visual_start - original_start) > 0.001
                audio_end_skipped = abs(snapped_end - original_end) < 0.001 and abs(visual_end - original_end) > 0.001
                if audio_start_skipped or audio_end_skipped:
                    event["audio_snap_skipped"] = True
                    if audio_start_skipped:
                        event["audio_snap_skipped_start"] = True
                    if audio_end_skipped:
                        event["audio_snap_skipped_end"] = True

    return vlm_result


def extract_cut_points(scene_data: Any) -> list[float]:
    """从场景边界数据提取切点列表。

    支持两种格式:
    - v1.1 范围格式: [[0.0, 2.68], [2.68, 3.96], ...]
    - v1.0 切点列表: [0.0, 2.68, 3.96, ...]

    Returns:
        排序后的唯一切点列表
    """
    if not scene_data:
        return []

    cut_points: set[float] = set()

    first_item = scene_data[0]
    is_range_format = isinstance(first_item, (list, tuple))

    if is_range_format:
        # v1.1: 范围格式，提取所有 start 和 end
        for scene_range in scene_data:
            if isinstance(scene_range, (list, tuple)) and len(scene_range) >= 2:
                cut_points.add(float(scene_range[0]))
                cut_points.add(float(scene_range[1]))
    else:
        # v1.0: 切点列表
        for point in scene_data:
            cut_points.add(float(point))

    return sorted(cut_points)


def snap_to_boundary(
    timestamp: float,
    cut_points: list[float],
    tolerance: float,
) -> float:
    """将时间戳对齐到最近的切点。

    如果时间戳与最近切点的距离超过 tolerance，则保持不变。

    Args:
        timestamp: 原始时间戳
        cut_points: 排序后的切点列表
        tolerance: 容差 (秒)

    Returns:
        对齐后的时间戳
    """
    if not cut_points:
        return timestamp

    # 二分查找最近的切点
    import bisect

    idx = bisect.bisect_left(cut_points, timestamp)

    # 检查 idx 和 idx-1 位置，取更近的
    best_point = timestamp
    best_dist = float("inf")

    for check_idx in (idx - 1, idx, idx + 1):
        if 0 <= check_idx < len(cut_points):
            point = cut_points[check_idx]
            dist = abs(timestamp - point)
            if dist < best_dist:
                best_dist = dist
                best_point = point

    # 超过容差则不对齐
    if best_dist > tolerance:
        return timestamp

    return best_point


def snap_highlight_start(
    timestamp: float,
    cut_points: list[float],
    tolerance: float = 0.5,
    lead_in: float = 0.3,
    *,
    silence_intervals: list[dict[str, float]] | None = None,
    speech_intervals: list[dict[str, float]] | None = None,
    max_shift: float = 5.0,
) -> float:
    """对齐高光开始时间：视觉 snap + 音频门控。

    策略（VAD 模式）：
    1. 在 timestamp ± max_shift 范围内搜索所有视觉切点
    2. 按距离排序，优先选近的切点
    3. 对每个切点做音频安全检查：
       - 切点和 lead_in 候选点都不在语音中 → 安全，直接 snap
       - 切点在静音但 lead_in 进入语音 → 减小 lead_in 或找间隙
       - 切点在语音中 → 回退到语音起点/间隙
    4. 所有切点都不安全 → 做纯音频 snap（忽略视觉对齐）
    5. 纯音频 snap 也失败 → 返回原始 timestamp（不 snap）

    策略（纯视觉/silence 模式）：保持原有 tolerance 行为，向后兼容。

    Args:
        timestamp: VLM 输出的开始时间
        cut_points: 排序后的切点列表
        tolerance: 纯视觉/静音模式的容差（秒）
        lead_in: 切点后的偏移量（秒），跳过转场残影
        silence_intervals: 静音区间列表（fallback）
        speech_intervals: VAD 语音区间（优先）
        max_shift: 音频感知模式下的最大搜索/调整距离（秒）

    Returns:
        对齐后的开始时间
    """
    import bisect

    if not cut_points:
        return timestamp

    def _check_cut(cut_point: float) -> float | None:
        """检查切点是否音频安全，返回 snap 结果或 None。"""
        candidate = cut_point + lead_in
        if speech_intervals is not None:
            cut_in_speech = _is_in_speech(cut_point, speech_intervals, pad=0.1)
            cand_in_speech = _is_in_speech(candidate, speech_intervals, pad=0.05)
            if not cut_in_speech and not cand_in_speech:
                return candidate
            if not cut_in_speech and cand_in_speech:
                safe = _find_speech_boundary_start(
                    candidate, speech_intervals,
                    max_shift=max_shift, speech_lead=0.05,
                    original_timestamp=timestamp,
                )
                if safe is not None:
                    return safe
                return None
            safe = _find_speech_boundary_start(
                cut_point, speech_intervals,
                max_shift=max_shift, speech_lead=0.15,
                original_timestamp=timestamp,
            )
            if safe is not None:
                return safe
            return None
        if silence_intervals is None:
            return candidate
        if _is_in_silence(cut_point, silence_intervals):
            return candidate
        safe = _find_safe_snap_point_start(candidate, silence_intervals, max_shift=max_shift, lead_in=lead_in, original_timestamp=timestamp)
        if safe is not None:
            return safe
        return None

    # ---- VAD 模式：在 max_shift 范围内搜索所有视觉切点 ----
    if speech_intervals is not None:
        lo, hi = timestamp - max_shift, timestamp + max_shift
        # 收集所有可行结果，选择距 timestamp 最近的
        best: float | None = None
        best_dist = float("inf")

        for cp in cut_points:
            if not (lo <= cp <= hi):
                continue
            result = _check_cut(cp)
            if result is None:
                continue
            dist = abs(result - timestamp)
            if dist <= max_shift and dist < best_dist:
                best = result
                best_dist = dist

        # 所有视觉切点都不安全 → 纯音频 snap（不依赖视觉切点）
        audio_safe = _find_speech_boundary_start(
            timestamp, speech_intervals,
            max_shift=max_shift, speech_lead=0.15,
            original_timestamp=timestamp,
        )
        if audio_safe is not None:
            dist = abs(audio_safe - timestamp)
            if dist <= max_shift and dist < best_dist:
                best = audio_safe
                best_dist = dist

        return best if best is not None else timestamp

    # ---- 纯视觉 / silence fallback 模式：保持原 tolerance 逻辑 ----
    idx = bisect.bisect_left(cut_points, timestamp)

    if idx < len(cut_points):
        forward_cut = cut_points[idx]
        if forward_cut - timestamp <= tolerance:
            result = _check_cut(forward_cut)
            if result is not None:
                return result

    if idx > 0:
        backward_cut = cut_points[idx - 1]
        if timestamp - backward_cut <= tolerance:
            result = _check_cut(backward_cut)
            if result is not None:
                return result

    return timestamp


def snap_highlight_end(
    timestamp: float,
    cut_points: list[float],
    tolerance: float = 0.5,
    lead_out: float = 0.0,
    *,
    silence_intervals: list[dict[str, float]] | None = None,
    speech_intervals: list[dict[str, float]] | None = None,
    max_shift: float = 5.0,
) -> float:
    """对齐高光结束时间：视觉 snap + 音频门控（与snap_highlight_start对称）。

    策略（VAD 模式）：
    1. 在 timestamp ± max_shift 范围内搜索所有视觉切点
    2. 按距离排序，优先选近的切点
    3. 对每个切点做音频安全检查：
       - 切点和 lead_out 候选点都不在语音中 → 安全，直接 snap
       - 切点在静音但 lead_out 进入语音 → 减小 tail 或找间隙
       - 切点在语音中 → 前进到语音终点/间隙
    4. 所有切点都不安全 → 做纯音频 snap（忽略视觉对齐）
    5. 纯音频 snap 也失败 → 返回原始 timestamp（不 snap）

    策略（纯视觉/silence 模式）：保持原有 tolerance 行为，向后兼容。

    Args:
        timestamp: VLM 输出的结束时间
        cut_points: 排序后的切点列表
        tolerance: 纯视觉/静音模式的容差（秒）
        lead_out: 切点后偏移量（秒），预留过渡余量
        silence_intervals: 静音区间列表（fallback）
        speech_intervals: VAD 语音区间（优先）
        max_shift: 音频感知模式下的最大搜索/调整距离（秒）

    Returns:
        对齐后的结束时间
    """
    import bisect

    if not cut_points:
        return timestamp

    def _check_cut_end(cut_point: float) -> float | None:
        """检查切点是否音频安全，返回 snap 结果或 None。"""
        candidate = cut_point + lead_out
        if speech_intervals is not None:
            cut_in_speech = _is_in_speech(cut_point, speech_intervals, pad=0.1)
            cand_in_speech = _is_in_speech(candidate, speech_intervals, pad=0.05)
            if not cut_in_speech and not cand_in_speech:
                return candidate
            if not cut_in_speech and cand_in_speech:
                safe = _find_speech_boundary_end(
                    candidate, speech_intervals,
                    max_shift=max_shift, tail=0.05,
                    original_timestamp=timestamp,
                )
                if safe is not None:
                    return safe
                return None
            safe = _find_speech_boundary_end(
                cut_point, speech_intervals,
                max_shift=max_shift, tail=0.25,
                original_timestamp=timestamp,
            )
            if safe is not None:
                return safe
            return None
        if silence_intervals is None:
            return candidate
        if _is_in_silence(cut_point, silence_intervals):
            return candidate
        safe = _find_safe_snap_point_end(candidate, silence_intervals, max_shift=max_shift, lead_out=lead_out, original_timestamp=timestamp)
        if safe is not None:
            return safe
        return None

    # ---- VAD 模式：在 max_shift 范围内搜索所有视觉切点 ----
    if speech_intervals is not None:
        lo, hi = timestamp - max_shift, timestamp + max_shift
        best: float | None = None
        best_dist = float("inf")

        for cp in cut_points:
            if not (lo <= cp <= hi):
                continue
            result = _check_cut_end(cp)
            if result is None:
                continue
            dist = abs(result - timestamp)
            if dist <= max_shift and dist < best_dist:
                best = result
                best_dist = dist

        # 所有视觉切点都不安全 → 纯音频 snap（不依赖视觉切点）
        audio_safe = _find_speech_boundary_end(
            timestamp, speech_intervals,
            max_shift=max_shift, tail=0.25,
            original_timestamp=timestamp,
        )
        if audio_safe is not None:
            dist = abs(audio_safe - timestamp)
            if dist <= max_shift and dist < best_dist:
                best = audio_safe
                best_dist = dist

        return best if best is not None else timestamp

    # ---- 纯视觉 / silence fallback 模式：保持原 tolerance 逻辑 ----
    idx = bisect.bisect_right(cut_points, timestamp)

    if idx > 0:
        backward_cut = cut_points[idx - 1]
        if timestamp - backward_cut <= tolerance:
            result = _check_cut_end(backward_cut)
            if result is not None:
                return result

    if idx < len(cut_points):
        forward_cut = cut_points[idx]
        if forward_cut - timestamp <= tolerance:
            result = _check_cut_end(forward_cut)
            if result is not None:
                return result

    return timestamp

