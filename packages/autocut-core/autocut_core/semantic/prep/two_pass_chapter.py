"""autocut_core.semantic.prep.two_pass_chapter - Two-pass Chapter Digest with Rolling Context.

Pass 1: Plot summary and story thread extraction (focus on narrative structure)
Pass 2: Character/relationship state changes (focus on entities)
Rolling context passes stable keys across chapters to ensure naming consistency.
"""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any

PASS1_SYSTEM_PROMPT = """你是专业短剧剧情分析师，负责生成章节剧情摘要和故事线进展。
你的任务是：
1. 基于提供的多集摘要和事件列表，生成本章整体剧情摘要
2. 梳理本章内故事线的进展，标注每个事件对应的故事线阶段
3. 提炼本章新增的剧情事实和留下的未解决悬念
4. 所有事件引用使用提供的E01/E02等短ID，不要自行生成ID

输出严格遵循JSON格式，不要输出额外说明。
"""

PASS1_USER_TEMPLATE = """## 章节信息
章节ID：{chapter_id}
包含集数：{episodes}
章节主题：{chapter_title}
核心冲突：{core_conflict}
弧光类型：{arc_type}
前序已有故事线（必须复用ID，不要新建）：{existing_threads}

## 集摘要
{episode_summaries}

## 事件列表（DSL格式）
{event_dsl}

## 输出格式
```json
{{
  "summary": "本章整体剧情摘要，300字以内",
  "story_thread_updates": [
    {{
      "thread_id": "T01或新建thread-xxx",
      "title": "故事线标题",
      "status": "introduced/advanced/resolved",
      "summary": "本章内故事线进展",
      "event_eids": ["E01", "E03"]
    }}
  ],
  "new_facts": ["事实1", "事实2"],
  "new_open_questions": ["悬念1", "悬念2"]
}}
```
- 前序故事线必须复用已有thread_id，不要重复创建
- 新故事线使用thread-xxx格式ID
- 事件引用仅使用E开头的短ID
"""


PASS2_SYSTEM_PROMPT = """你是专业短剧角色分析师，负责梳理章节内角色状态变化和关系演变。
你的任务是：
1. 列出本章出场的核心角色在章首和章末的状态变化
2. 梳理角色之间的关系变化
3. 所有事件引用使用提供的E01/E02等短ID
4. 角色key必须复用前序已有key，新角色使用char-xxx格式

输出严格遵循JSON格式，不要输出额外说明。
"""

PASS2_USER_TEMPLATE = """## 章节信息
章节ID：{chapter_id}
本章剧情摘要：{chapter_summary}
前序已有角色（必须复用ID，不要新建）：{existing_characters}
前序已有关系（必须复用ID，不要新建）：{existing_relationships}

## 事件列表（DSL格式）
{event_dsl}

## 输出格式
```json
{{
  "character_rollup": [
    {{
      "character_key": "char-xxx或复用已有key",
      "name": "角色名",
      "aliases": ["别名1", "别名2"],
      "state_at_start": "本章开始时状态",
      "state_at_end": "本章结束时状态",
      "evidence_eids": ["E01", "E05"]
    }}
  ],
  "relationship_rollup": [
    {{
      "relationship_key": "rel-xxx或复用已有key",
      "character_key_a": "char-xxx",
      "character_key_b": "char-xxx",
      "summary": "关系状态与变化",
      "evidence_eids": ["E02"]
    }}
  ]
}}
```
- 已有角色/关系必须复用key，不要重复创建
- 新角色/关系使用char-xxx/rel-xxx格式ID
- 事件引用仅使用E开头的短ID
"""


def build_pass1_prompt(
    chapter_id: str,
    episodes: list[int],
    chapter_meta: dict[str, Any],
    episode_summaries: list[dict[str, Any]],
    event_dsl: list[str],
    rolling_context: dict[str, Any],
) -> list[dict[str, str]]:
    """Build Pass 1 (plot) prompt."""
    ep_text = "\n".join(
        f"EP{ep['episode']}: 开局：{ep.get('opening_state', '')} → 结局：{ep.get('ending_state', '')}\n摘要：{ep.get('summary', '')}"
        for ep in episode_summaries
    )
    existing_threads = [
        {"id": t["id"], "name": t["name"], "summary": t.get("summary", "")}
        for t in rolling_context.get("threads", [])
    ]
    user_content = PASS1_USER_TEMPLATE.format(
        chapter_id=chapter_id,
        episodes=episodes,
        chapter_title=chapter_meta.get("title", ""),
        core_conflict=chapter_meta.get("core_conflict", ""),
        arc_type=chapter_meta.get("arc_type", "unknown"),
        existing_threads=json.dumps(existing_threads, ensure_ascii=False, indent=2),
        episode_summaries=ep_text,
        event_dsl="\n".join(event_dsl),
    )
    return [
        {"role": "system", "content": PASS1_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def build_pass2_prompt(
    chapter_id: str,
    chapter_summary: str,
    event_dsl: list[str],
    rolling_context: dict[str, Any],
) -> list[dict[str, str]]:
    """Build Pass 2 (entities) prompt."""
    existing_chars = [
        {"id": c["id"], "name": c["name"], "last_state": c.get("state_at_end", "")}
        for c in rolling_context.get("characters", [])
    ]
    existing_rels = [
        {"id": r["id"], "a": r["character_key_a"], "b": r["character_key_b"], "summary": r.get("summary", "")}
        for r in rolling_context.get("relationships", [])
    ]
    user_content = PASS2_USER_TEMPLATE.format(
        chapter_id=chapter_id,
        chapter_summary=chapter_summary,
        existing_characters=json.dumps(existing_chars, ensure_ascii=False, indent=2),
        existing_relationships=json.dumps(existing_rels, ensure_ascii=False, indent=2),
        event_dsl="\n".join(event_dsl),
    )
    return [
        {"role": "system", "content": PASS2_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def merge_chapter_results(
    chapter_id: str,
    episodes: list[int],
    pass1_result: dict[str, Any],
    pass2_result: dict[str, Any],
    short_id_map: dict[str, str],
    chapter_meta: dict[str, Any],
    all_event_ids: set[str],
) -> dict[str, Any]:
    """Merge Pass1 and Pass2 results into standard ChapterDigest format, replacing short IDs with full IDs."""
    # Map short IDs to full event IDs
    def map_ids(id_list: list[str]) -> list[str]:
        result = []
        for sid in id_list:
            if sid in short_id_map:
                result.append(short_id_map[sid])
            elif sid in all_event_ids:
                result.append(sid)
        return sorted(set(result))

    # Build story threads
    story_threads = []
    for t in pass1_result.get("story_thread_updates", []):
        story_threads.append({
            "thread_key": t.get("thread_id", ""),
            "title": t.get("title", ""),
            "summary": t.get("summary", ""),
            "status": t.get("status", "advanced"),
            "event_ids": map_ids(t.get("event_eids", [])),
        })

    # Build character rollup
    character_rollup = []
    for c in pass2_result.get("character_rollup", []):
        character_rollup.append({
            "character_key": c.get("character_key", ""),
            "name": c.get("name", ""),
            "aliases": c.get("aliases", []),
            "state_at_start": c.get("state_at_start", ""),
            "state_at_end": c.get("state_at_end", ""),
            "episode_numbers": list(episodes),
            "evidence_event_ids": map_ids(c.get("evidence_eids", [])),
        })

    # Build relationship rollup
    relationship_rollup = []
    for r in pass2_result.get("relationship_rollup", []):
        relationship_rollup.append({
            "relationship_key": r.get("relationship_key", ""),
            "character_key_a": r.get("character_key_a", ""),
            "character_key_b": r.get("character_key_b", ""),
            "summary": r.get("summary", ""),
            "evidence_event_ids": map_ids(r.get("evidence_eids", [])),
        })

    # Collect all event IDs in this chapter
    all_chapter_event_ids = sorted(set(
        eid
        for t in story_threads
        for eid in t["event_ids"]
    ))

    # Build final chapter digest (compatible with existing schema)
    return {
        "schema_version": "1.1",
        "chapter_id": chapter_id,
        "episodes": list(episodes),
        "title": chapter_meta.get("title", ""),
        "arc_type": chapter_meta.get("arc_type", "unknown"),
        "summary": pass1_result.get("summary", ""),
        "character_rollup": character_rollup,
        "relationship_rollup": relationship_rollup,
        "story_threads": story_threads,
        "fact_keys": [],  # Facts are handled at Registry stage for global normalization
        "event_ids": all_chapter_event_ids,
        "open_question_keys": [],  # Questions are handled at Registry stage
        "facts": pass1_result.get("new_facts", []),
        "open_questions": pass1_result.get("new_open_questions", []),
    }


def update_rolling_context(
    rolling_context: dict[str, Any],
    chapter_result: dict[str, Any],
) -> dict[str, Any]:
    """Update rolling context with stable keys from completed chapter, for next chapter."""
    # Update characters
    existing_char_ids = {c["id"] for c in rolling_context.get("characters", [])}
    for c in chapter_result.get("character_rollup", []):
        cid = c["character_key"]
        if cid in existing_char_ids:
            # Update existing character state
            for existing in rolling_context["characters"]:
                if existing["id"] == cid:
                    existing["name"] = c["name"]
                    existing["state_at_end"] = c["state_at_end"]
                    if c.get("aliases"):
                        existing.setdefault("aliases", []).extend([a for a in c["aliases"] if a not in existing["aliases"]])
                    break
        else:
            # Add new character
            rolling_context.setdefault("characters", []).append({
                "id": cid,
                "name": c["name"],
                "aliases": c.get("aliases", []),
                "state_at_end": c["state_at_end"],
            })

    # Update relationships
    existing_rel_ids = {r["id"] for r in rolling_context.get("relationships", [])}
    for r in chapter_result.get("relationship_rollup", []):
        rid = r["relationship_key"]
        if rid in existing_rel_ids:
            for existing in rolling_context["relationships"]:
                if existing["id"] == rid:
                    existing["summary"] = r["summary"]
                    break
        else:
            rolling_context.setdefault("relationships", []).append({
                "id": rid,
                "character_key_a": r["character_key_a"],
                "character_key_b": r["character_key_b"],
                "summary": r["summary"],
            })

    # Update threads
    existing_thread_ids = {t["id"] for t in rolling_context.get("threads", [])}
    for t in chapter_result.get("story_threads", []):
        tid = t["thread_key"]
        if tid in existing_thread_ids:
            for existing in rolling_context["threads"]:
                if existing["id"] == tid:
                    existing["summary"] = t["summary"]
                    existing["status"] = t["status"]
                    break
        else:
            rolling_context.setdefault("threads", []).append({
                "id": tid,
                "name": t["title"],
                "summary": t["summary"],
                "status": t["status"],
            })

    return rolling_context
