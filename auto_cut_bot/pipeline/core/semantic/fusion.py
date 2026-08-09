"""window-analysis-fusion — 多源数据融合引擎。

为 window_analysis Stage 的 VLM 语义分析结果提供与 API 元数据、
ASR 转录文本的交叉验证与边界融合能力。

数据源:
  - VLM (visual events): 窗口分析产出的视觉事件、主体、边界
  - API (source_metadata): 字幕 (subtitles)、分镜 (shots) 边界与主体
  - ASR (asr_transcript): FunASR 转录文本与说话人段

设计原则:
  - 所有方法对 None/空输入安全 (可优雅降级)
  - 基于时间戳窗口的边界去重合并
  - 置信度从高到低: high > medium > low
"""

from __future__ import annotations

import math
from typing import Any


# ═══════════════════════════════════════════════════════════════════════════════
# 边界合并
# ═══════════════════════════════════════════════════════════════════════════════


class DataFusion:
    """多源数据融合 — 无状态, 所有方法纯函数式。

    输入可以是 None 或空列表 (表示该数据源不可用), 所有方法安全处理。
    """

    # ── 边界去重距离阈值 ────────────────────────────────────────────────

    _MERGE_DISTANCE_SECONDS = 2.0  # 两条边界时间差 < 此值视为同一事件

    # ── 边界合并 ────────────────────────────────────────────────────────

    @staticmethod
    def merge_boundaries(
        vlm: list[dict[str, Any]] | None,
        api: list[dict[str, Any]] | None,
        asr: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        """合并三个数据源的边界, 去重后按时间排序返回。

        合并策略:
          1. 标准化所有输入的字段名 (start, end)
          2. 按时间排序
          3. 相邻边界时间差 < _MERGE_DISTANCE_SECONDS 时合并为一条
          4. 合并时保留置信度最高的 source 信息

        每个边界字典结构:
          {
            "start": float, "end": float,
            "event_type": str, "confidence": "high"|"medium"|"low",
            "source": "vlm"|"api"|"asr", "description": str,
            "subjects": list[str],
          }
        """
        collected: list[dict[str, Any]] = []

        for source_name, boundaries in [("vlm", vlm), ("api", api), ("asr", asr)]:
            if not boundaries:
                continue
            for b in boundaries:
                if not isinstance(b, dict):
                    continue
                # 标准化时间字段
                start = float(b.get("start_time", b.get("start", 0)))
                end = float(b.get("end_time", b.get("end", 0)))
                if start >= end:
                    continue  # 跳过无效边界
                collected.append({
                    "start": start,
                    "end": end,
                    "event_type": b.get("event_type", "unknown"),
                    "confidence": b.get("confidence", "low"),
                    "source": source_name,
                    "description": b.get("description", ""),
                    "subjects": DataFusion._normalize_subjects_list(b.get("subjects", [])),
                    # 原始引用 (用于后续诊断)
                    "_origin": b,
                })

        if not collected:
            return []

        # 按开始时间排序
        collected.sort(key=lambda b: (b["start"], b["end"]))

        # 合并相邻边界
        merged: list[dict[str, Any]] = []
        for b in collected:
            if not merged:
                merged.append(b)
                continue
            last = merged[-1]
            # 检查时间窗口重叠/接近
            gap = b["start"] - last["end"]
            if gap < DataFusion._MERGE_DISTANCE_SECONDS and b["start"] <= last["end"] + DataFusion._MERGE_DISTANCE_SECONDS * 2:
                # 合并: 扩展时间范围, 按置信度选择最佳描述
                last["end"] = max(last["end"], b["end"])
                last["confidence"] = DataFusion._max_confidence(last["confidence"], b["confidence"])
                last["subjects"] = DataFusion._merge_subject_lists(last["subjects"], b["subjects"])
                last["_sources"] = list(set(last.get("_sources", [last["source"]]) + [b["source"]]))
                if not last["description"] and b["description"]:
                    last["description"] = b["description"]
            else:
                merged.append(b)

        # 清理内部字段
        for b in merged:
            b.pop("_origin", None)
            b.pop("_sources", None)

        return merged

    # ── 主体信息合并 ─────────────────────────────────────────────────

    @staticmethod
    def enrich_subjects(
        vlm_subjects: list[dict[str, Any]] | None,
        api_subjects: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        """合并 VLM 和 API 来源的主体信息。

        VLM 通过视觉识别角色, 可能缺少角色名称; API 提供完整的角色档案
        (name, persona, personality, role, aliases, traits, 等)。

        合并策略:
          1. 以 VLM 主体为基准 (与视觉帧对齐)
          2. API 主体按 name 匹配, 补充 persona/role/personality 等字段
          3. 仅在 VLM 主体中未出现的 API 主体追加到末尾
          4. VLM 中的 nameless 主体 (label="unknown") 保留, 等待后续
             identity_resolution 阶段匹配

        每个主体字典:
          {"name": str, "label": str, "aliases": list[str],
           "persona": str|None, "role": str|None, "personality": list[str],
           "traits": str|None, "source": "vlm"|"api"|"vlm+api"}
        """
        # 标准化输入
        vlms = DataFusion._normalize_subjects(vlm_subjects, "vlm")
        apis = DataFusion._normalize_subjects(api_subjects, "api")

        if not vlms and not apis:
            return []

        if not vlms:
            return apis

        if not apis:
            return vlms

        # 构建 API 主体 name→data 索引
        api_index: dict[str, dict[str, Any]] = {}
        for s in apis:
            name = s.get("name", "").strip()
            if name:
                api_index[name] = s

        result: list[dict[str, Any]] = []
        merged_names: set[str] = set()

        for vlm_s in vlms:
            name = vlm_s.get("name", "").strip()
            if name and name in api_index:
                # VLM 有名字且在 API 中存在 → 融合
                api_s = api_index[name]
                enriched = dict(vlm_s)
                enriched["source"] = "vlm+api"
                # 补充 API 的丰富字段 (VLM 字段优先)
                for field in ("persona", "personality", "role", "traits", "tone",
                              "voice_timbre", "visual_features", "aliases"):
                    if not enriched.get(field) and api_s.get(field):
                        enriched[field] = api_s[field]
                # 合并 aliases
                if enriched.get("aliases") and api_s.get("aliases"):
                    existing = set(enriched["aliases"])
                    for a in api_s["aliases"]:
                        if a not in existing:
                            enriched["aliases"].append(a)
                result.append(enriched)
                merged_names.add(name)
            else:
                # VLM 主体, 可能无名 (label="unknown")
                result.append(vlm_s)

        # 追加 API 中未在 VLM 中出现的主体
        for api_s in apis:
            name = api_s.get("name", "").strip()
            if name and name not in merged_names:
                api_s["source"] = "api"
                result.append(api_s)

        return result

    # ── 交叉验证 ─────────────────────────────────────────────────────

    @staticmethod
    def cross_validate(vlm_text: str | None, asr_text: str | None) -> float:
        """计算 VLM 描述文本与 ASR 转录文本的 LCS 比率。

        用途: 验证 VLM 对视频内容的理解是否与真实音频一致。
        返回 [0.0, 1.0] 的值, 1.0 表示完全一致。

        基于字符级 LCS 动态规划, 时间复杂度 O(n*m), 空间 O(min(n,m))。
        """
        if not vlm_text or not asr_text:
            return 0.0

        a, b = vlm_text, asr_text
        len_a, len_b = len(a), len(b)

        # 滚动数组优化内存
        if len_a < len_b:
            a, b = b, a
            len_a, len_b = len(b), len(a)

        prev = [0] * (len_b + 1)
        curr = [0] * (len_b + 1)

        for i in range(1, len_a + 1):
            for j in range(1, len_b + 1):
                if a[i - 1] == b[j - 1]:
                    curr[j] = prev[j - 1] + 1
                else:
                    curr[j] = max(prev[j], curr[j - 1])
            prev, curr = curr, prev

        lcs_len = prev[len_b]
        return lcs_len / max(len_a, len_b)

    # ── 置信度加权合并 ────────────────────────────────────────────────

    _CONFIDENCE_ORDER: dict[str, int] = {"high": 3, "medium": 2, "low": 1}

    @staticmethod
    def confidence_weighted_merge(
        events: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        """按置信度合并事件列表, 高置信度事件覆盖低置信度事件。

        适用场景: 同一时间窗口内有多条不同来源的事件记录时,
        优先保留置信度高的记录。

        合并策略:
          1. 按时间排序
          2. 重叠窗口内的事件, 保留置信度最高的
          3. 同置信度时保留第一条 (时间优先)

        每个事件字典:
          {"start": float, "end": float, "confidence": "high"|"medium"|"low",
           "event_type": str, "description": str, ...}
        """
        if not events:
            return []

        # 按时间排序
        sorted_events = sorted(events, key=lambda e: (e.get("start", 0), e.get("end", 0)))

        if len(sorted_events) <= 1:
            return sorted_events

        result: list[dict[str, Any]] = []
        for event in sorted_events:
            if not isinstance(event, dict):
                continue
            if not result:
                result.append(dict(event))
                continue

            last = result[-1]
            last_end = last.get("end", 0)
            event_start = event.get("start", 0)

            # 重叠窗口
            if event_start < last_end:
                last_conf = DataFusion._confidence_score(last.get("confidence"))
                evt_conf = DataFusion._confidence_score(event.get("confidence"))
                if evt_conf > last_conf:
                    result[-1] = dict(event)
                # 同置信度: 保留第一条 (last)
            else:
                result.append(dict(event))

        return result

    # ── 内部辅助 ─────────────────────────────────────────────────────

    @staticmethod
    def _normalize_subjects_list(raw: list[Any] | None) -> list[str]:
        """将 subjects 规范化为字符串列表。"""
        if not raw:
            return []
        result: list[str] = []
        for item in raw:
            if isinstance(item, str):
                result.append(item)
            elif isinstance(item, dict):
                name = item.get("name", "")
                if name:
                    result.append(name)
        return result

    @staticmethod
    def _merge_subject_lists(a: list[str], b: list[str]) -> list[str]:
        """合并两个主体列表, 去重保留顺序。"""
        seen: set[str] = set()
        result: list[str] = []
        for item in a + b:
            if item not in seen:
                result.append(item)
                seen.add(item)
        return result

    @staticmethod
    def _max_confidence(a: str, b: str) -> str:
        """返回两个置信度中较高的一个。"""
        order = DataFusion._CONFIDENCE_ORDER
        return a if order.get(a, 0) >= order.get(b, 0) else b

    @staticmethod
    def _confidence_score(conf: str | None) -> int:
        """置信度字符串 → 数值分数。"""
        return DataFusion._CONFIDENCE_ORDER.get(conf or "low", 0)

    @staticmethod
    def _normalize_subjects(
        subjects: list[dict[str, Any]] | None,
        source: str,
    ) -> list[dict[str, Any]]:
        """将主体列表规范化为统一格式。"""
        if not subjects:
            return []
        result: list[dict[str, Any]] = []
        for s in subjects:
            if not isinstance(s, dict):
                continue
            name = s.get("name", "").strip()
            if not name and s.get("label") != "unknown":
                continue
            result.append({
                "name": name,
                "label": s.get("label", name or "unknown"),
                "aliases": list(s.get("aliases", [])),
                "persona": s.get("persona"),
                "personality": list(s.get("personality", [])),
                "role": s.get("role"),
                "traits": s.get("traits"),
                "tone": s.get("tone"),
                "voice_timbre": s.get("voice_timbre"),
                "visual_features": s.get("visual_features"),
                "source": source,
            })
        return result


# ═══════════════════════════════════════════════════════════════════════════════
# 便捷函数 — 无状态静态方法包装
# ═══════════════════════════════════════════════════════════════════════════════


def merge_boundaries(
    vlm: list[dict[str, Any]] | None = None,
    api: list[dict[str, Any]] | None = None,
    asr: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """合并三个数据源的边界, 去重后按时间排序返回。"""
    return DataFusion.merge_boundaries(vlm, api, asr)


def enrich_subjects(
    vlm_subjects: list[dict[str, Any]] | None = None,
    api_subjects: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """合并 VLM 和 API 来源的主体信息。"""
    return DataFusion.enrich_subjects(vlm_subjects, api_subjects)


def cross_validate(vlm_text: str | None = None, asr_text: str | None = None) -> float:
    """计算 VLM 描述文本与 ASR 转录文本的 LCS 比率。"""
    return DataFusion.cross_validate(vlm_text, asr_text)


def confidence_weighted_merge(
    events: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """按置信度合并事件列表, 高置信度事件覆盖低置信度事件。"""
    return DataFusion.confidence_weighted_merge(events)


# ═══════════════════════════════════════════════════════════════════════════════
# VLM 多源仲裁 — 输出落表
# ═══════════════════════════════════════════════════════════════════════════════


def apply_vlm_arbitration(
    vlm_dialogues: list[dict[str, Any]],
    db: Any,
    book_id: str,
    episode_id: int,
) -> dict[str, Any]:
    """将 VLM 多源仲裁结果写入 subtitles 表。

    VLM 输出的每条 dialogue_and_text 包含 source_accuracy:
      - asr_text, api_text, script_text: 三源原文
      - agreement: 一致性类型
      - chosen_source: "asr"|"api"|"script"|"both"|"vlm"
      - reason: 选择理由
      - vlm_override_text: 所有源都错时 VLM 自己的判断

    写入策略:
      1. 保留三源原文 (asr_text, api_text, script_text)
      2. 最终文本 (text) = chosen_source 对应的版本
      3. chosen_source='vlm' 时 text = vlm_override_text
      4. 统计各源准确率

    返回统计字典: {asr_accuracy, api_accuracy, script_accuracy, total, ...}
    """
    if not db.is_available or not vlm_dialogues:
        return {"total": 0, "note": "no data"}

    stats: dict[str, Any] = {
        "total": 0,
        "both_match": 0,
        "asr_chosen": 0,
        "api_chosen": 0,
        "script_chosen": 0,
        "vlm_override": 0,
        "uncertain": 0,
        "asr_accuracy": 0.0,
        "api_accuracy": 0.0,
        "script_accuracy": 0.0,
    }

    for item in vlm_dialogues:
        if not isinstance(item, dict):
            continue
        sa = item.get("source_accuracy")
        if not isinstance(sa, dict):
            continue

        agreement = sa.get("agreement", "")
        chosen = sa.get("chosen_source", "")
        start_time = float(item.get("start", 0))
        end_time = float(item.get("end", start_time + 1))
        speaker = item.get("speaker", "")

        # 确定最终文本: VLM 选择
        text = item.get("text", "")
        asr_text = sa.get("asr_text")
        api_text = sa.get("api_text")
        script_text = sa.get("script_text")
        vlm_override = sa.get("vlm_override_text")

        if chosen == "vlm" and vlm_override:
            text = vlm_override
        elif chosen == "asr" and asr_text:
            text = asr_text
        elif chosen == "api" and api_text:
            text = api_text
        elif chosen == "script" and script_text:
            text = script_text

        # 统计
        stats["total"] += 1
        if agreement == "both_match":
            stats["both_match"] += 1
        if chosen == "asr":
            stats["asr_chosen"] += 1
        elif chosen == "api":
            stats["api_chosen"] += 1
        elif chosen == "script":
            stats["script_chosen"] += 1
        elif chosen == "vlm":
            stats["vlm_override"] += 1
        else:
            stats["uncertain"] += 1

        # 写入 DB
        try:
            db._execute(
                f"""INSERT INTO {db._t(db._schema, 'subtitles')}
                    (book_id, episode_id, start_time, end_time, speaker, text, source,
                     asr_text, api_text, script_text,
                     agreement, chosen_source, reason, is_verified)
                VALUES (%s,%s,%s,%s, %s,%s,%s, %s,%s,%s, %s,%s,%s, true)
                ON CONFLICT (book_id, episode_id, start_time) DO UPDATE SET
                    text = EXCLUDED.text,
                    asr_text = COALESCE(EXCLUDED.asr_text, subtitles.asr_text),
                    api_text = COALESCE(EXCLUDED.api_text, subtitles.api_text),
                    script_text = COALESCE(EXCLUDED.script_text, subtitles.script_text),
                    agreement = EXCLUDED.agreement,
                    chosen_source = EXCLUDED.chosen_source,
                    reason = EXCLUDED.reason,
                    is_verified = true
                """,
                (
                    book_id, episode_id, start_time, end_time,
                    speaker, text, chosen,
                    asr_text, api_text, script_text,
                    agreement, chosen, sa.get("reason", ""),
                ),
            )
        except Exception:
            continue  # DB write failure is non-blocking

    # 计算准确率
    total = stats["total"]
    if total > 0:
        stats["asr_accuracy"] = round(stats["asr_chosen"] / total, 3)
        stats["api_accuracy"] = round(stats["api_chosen"] / total, 3)
        stats["script_accuracy"] = round(stats["script_chosen"] / total, 3)

    return stats


def _t(schema: str, table: str) -> str:
    """Schema-qualified table name."""
    return f"{schema}.{table}"