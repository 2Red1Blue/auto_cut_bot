"""三层LLM Prompt模板
注意：本文件所有模板均为普通字符串，不是f-string！
- 单大括号 {xxx} 是str.format()占位符，不需要转义
- 双大括号 {{}} 才会被format解析为单大括号（用于JSON示例）
"""

LAYER1_SEGMENTER_PROMPT = """你是专业的影视编剧分析师，请根据提供的剧集摘要，分析全剧的整体剧情结构，输出JSON格式结果：
要求：
1. 合理划分剧情章节（chapter），每个章节是一个完整的剧情弧（铺垫-发展-高潮-收尾），章节之间可以有1集重叠用于转折
2. 识别核心故事线、主要角色（含别名/身份/阵营/核心关系）、全局转折点、张力曲线、核心世界观规则、关键道具、伏笔映射、高光名场面
3. tension字段取值1-10，越高代表剧情张力越强
4. importance字段取值0-1，越高代表越核心
5. 输出严格遵循JSON格式，不要输出任何额外解释内容，不要加markdown代码块包裹
6. 角色别名要包含所有剧中出现的称呼（外号、身份代称、化名等），不要遗漏
<!-- THEMES_REQUIREMENT -->

## 剧集摘要（共 {{ total_episodes }} 集）
{{ episode_summaries }}

输出JSON字段（使用短字段名节省空间，字段含义如下）：
{{
  "t": "剧集名称（如果能识别到）",
  "th": [{{"n": "主题名称", "d": "主题核心内涵（1句话）"}}], // 3-7个主题，按重要性排序
  "ch": [{{
    "s": 起始集, "e": 结束集, "n": "章节标题", "a": "铺垫/升级/高潮/收尾",
    "cc": "本章核心冲突（1句话）", "clx": 高潮集数,
    "kc": ["核心出场角色char_id"], "rb": ["本章必须覆盖的3-5个关键剧情节点"]
  }}],
  "st": [{{
    "id": "thread-xxx", "n": "故事线名称", "d": "故事线描述",
    "ca": "本条线完整起承转合概述", "s": 起始集, "e": 结束集,
    "kn": ["本条线5-8个核心节点"], "it": "P/S/T（primary/secondary/tertiary）", "i": 0-1
  }}],
  "c": [{{
    "id": "char-xxx", "n": "标准名", "al": ["别名/外号/代称"],
    "idn": "身份描述", "f": "阵营（天使/恶魔/人类/女巫等）",
    "fs": 首次出场集数, "le": 最后出场集数,
    "cr": [{{"r": "父/母/女/妻/敌/友/爱", "t": "关联角色ID"}}],
    "it": "P/S/M（protagonist/supporting/minor）", "i": 0-1, "a": "角色弧光概述"
  }}],
  "wr": [{{
    "id": "rule-xxx", "d": "核心世界观/规则/诅咒/魔法设定（1句话）", "ep": 首次揭示集数
  }}],
  "kp": [{{
    "id": "prop-xxx", "n": "关键道具名称", "s": 首次出现集数, "rc": ["关联角色char_id"], "i": 0-1
  }}],
  "fm": [{{
    "s": 伏笔埋下集数, "p": 伏笔回收集数, "d": "伏笔内容+回收说明", "rt": "关联故事线ID"
  }}],
  "tp": [{{
    "e": 集数, "d": "转折点描述", "sg": "M/m（major/minor）", "i": 0-1, "mi": true/false,
    "rt": ["关联故事线ID"], "rc": ["关联角色char_id"], "clx": true/false
  }}],
  "hs": [{{
    "e": 集数, "th": "时间点提示（开篇/中/尾）", "sc": 1-10, "r": "高光原因（情绪爆点/名台词/视觉奇观）"
  }}],
  "tm": [{{"e":集数, "t":"jump/flashback", "d":"时间跳跃/闪回说明（如七年后时间跳转）"}}], // 全局时间线标记：时间跳跃/闪回节点
  "tc": [每集张力值1-10，数组索引=集数-1，如第1集对应tc[0]，不要输出其他字段]
}}

## 提取规则
1. 核心角色8-12个，别名要全（如Lucifer需包含路西法/黑衣男子/恶魔领主/撒旦等所有称呼）
2. 世界观规则3-7条，仅提取贯穿全剧的核心基础规则，不要罗列单章细碎设定
3. 关键道具3-5个，仅提取贯穿多集、推动剧情的核心信物，不要罗列普通物品
4. 全局伏笔5-10个，仅提取跨章回收的核心伏笔，不要列单章小悬念
5. 高光名场面预标记5-10个，优先标记情绪爆发力强、传播度高的名场面
6. mi字段：仅标记真正推动主线的关键转折为true，全剧仅5-10个，不要全部标记
7. tm字段标记所有跨集时间跳跃、闪回节点，帮助校准时间线
8. tc字段是纯数字数组，索引=集数-1，值为1-10的张力值，不要加ep/keywords等冗余字段
8. 所有描述简洁明了，避免冗余长文本，严格使用给定短字段名"""

LAYER2_PASS1_BEAT_PROMPT = """你是专业的影视剧情分析师，请根据当前章节信息和本章事件，提取剧情节拍（beat）：
本章信息：
- 章节范围：第{chapter_start_ep}集到第{chapter_end_ep}集
- 核心冲突：{chapter_core_conflict}
- 前情摘要：{prev_summary}
- 重叠集：{overlap_eps}
- 本章事件列表（E{{事件序号}} [第X集]: {{事件内容}}）：
{chapter_events_dsl}
- 已有故事线：{global_threads_prompt}

要求：
1. **Beat粒度规则**：Beat是**情节级叙事单元**（对应完整的剧情动作/决策/信息揭露，如"主角被陷害入狱"、"主角结识盟友"），不是镜头级动作描述；不要把单个表情、反应、走位等镜头级内容单独输出为beat；连续发生在同一场景、同一故事线、同一phase的关联动作必须合并为一个beat
2. episode字段填写本章内相对集数（从1开始）
3. phase字段只能取：setup(铺垫)/escalation(升级)/turn(转折)/reveal(揭秘)/payoff(高潮)/consequence(后果)/coda(收尾)
4. event_eids填写对应事件的短ID（如["E1","E2"]），普通beat关联1-3个核心事件，高潮beat最多关联5个核心事件；合并多个关联动作的beat要包含所有相关事件ID，不要罗列无关事件
5. depends_on_beat_sids填写依赖的之前节拍短ID
6. beat_sid使用B1/B2...格式的短ID，不要生成长ID
7. 覆盖本章90%以上的核心剧情事件，水内容（前情回顾/广告/无意义过场）标记到excluded_episodes说明原因，不要强行生成beat凑数
8. **Beat 去重规则**：每个 beat 只属于一个 thread，禁止将同一个 beat 复制到多个 thread 下。如果某个事件同时涉及多条故事线，选择最相关的那条线程归属，不要重复生成
9. 直接输出纯JSON，不要加markdown代码块包裹，不要输出额外解释文字
<!-- FORESHADOW_REQUIREMENT -->
<!-- WORLD_RULES_REQUIREMENT -->
<!-- QUESTIONS_REQUIREMENT -->

输出严格JSON格式：
{{
  "summary": "本章整体剧情摘要",
  "story_thread_updates": [
    {{
      "thread_id": "对应故事线ID",
      "beats": [{{"beat_sid": "Bxxx", "episode": 章内相对集数, "phase": "阶段", "event_eids": ["E1"], "summary": "节拍内容", "depends_on_beat_sids": ["Bxxx"]}}],
      "event_eids": ["本故事线覆盖的所有事件ID"]
    }}
  ],
  "excluded_episodes": [{{"episode": 集数, "event_eids": ["对应事件ID"], "reason_type": "water_content/recap/preview", "explanation": "排除理由"}}],
  "new_facts": ["本章新揭示的事实/设定"]
}}"""

LAYER2_PASS2_ENTITY_PROMPT = """你是专业的影视角色分析师，请根据本章剧情和节拍信息，提取角色状态变化和关系：
本章信息：
- 章节范围：第{chapter_start_ep}集到第{chapter_end_ep}集
- 本章节拍：
{beats_dsl}
- 前序活跃角色（最近3章出现过的）：
{rolling_chars_prompt}

要求：
1. 标记每个角色在本章开始和结束时的状态
2. 标注角色的重要性层级：protagonist(主角)/supporting(重要配角)/minor(次要角色)
3. 列出本章新出现/变化的角色关系
4. evidence_eids填写支撑该结论的事件短ID
5. character_key使用char-xxx格式的短ID，新角色自行生成语义化ID
6. relationship_key使用rel-charA-charB格式
7. 直接输出纯JSON，不要加markdown代码块包裹，不要输出额外解释文字

输出严格JSON格式：
{{
  "character_rollup": [
    {{
      "character_key": "角色唯一标识（char-xxx格式）",
      "name": "角色名",
      "aliases": ["别名列表"],
      "state_at_start": "本章开始时状态",
      "state_at_end": "本章结束时状态",
      "importance_tier": "protagonist/supporting/minor",
      "evidence_eids": ["支撑结论的事件短ID，如E1"]
    }}
  ],
  "relationship_rollup": [
    {{
      "relationship_key": "关系唯一标识（如rel-charA-charB）",
      "character_key_a": "角色A ID",
      "character_key_b": "角色B ID",
      "summary": "关系描述和本章变化",
      "importance": 0-1的重要性分值,
      "evidence_eids": ["支撑结论的事件短ID"]
    }}
  ],
  "new_character_reasoning": "新角色出现的理由说明，没有新角色则为空字符串"
}}"""
