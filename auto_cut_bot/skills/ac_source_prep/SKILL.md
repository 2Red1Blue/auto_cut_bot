---
name: ac_source_prep
description: 素材准备 — VLM-First 架构：视频切窗（480p 压缩）、全局上下文提取、VLM 逐窗语义分析、PySceneDetect 边界修正、置信度质量门控、高光精确标注。覆盖 Stage 1-4，将原始视频转化为结构化窗口分析产物，供下游剧集理解和故事生成使用。
version: 0.1.0
stages: [1, 2, 3, 4]
status: active
triggers:
  - "准备素材"
  - "视频切窗"
  - "source_windows"
  - "窗口分析"
  - "vlm_analysis"
  - "window_analysis"
  - "全局上下文"
  - "global_context"
  - "置信度检查"
  - "confidence_check"
  - "VLM 分析"
anti_triggers:
  - "渲染视频" → 使用 ac_render
  - "QC 检查" → 使用 ac_qc
  - "生成故事" → 使用 ac_story_generation
  - "生成计划" → 使用 ac_plan_orchestration
---

# 素材准备 (ac_source_prep) — VLM-First Stage 1-4

VLM 直接从视频提取所有语义信息（对白、角色、场景、节拍、高光候选），
API/剧本仅提供全局上下文注入。不需要 ASR 转录、剧本解析或多源对齐。

## Stages

| Stage | Name | Description | Status |
|-------|------|-------------|--------|
| 1 | `source_windows` | 素材扫描、窗口切分、480p CRF32 压缩、PySceneDetect 全片检测 | active |
| 2 | `global_context` | API/剧本提取全剧级上下文 + API 高光 shots 落库 | active |
| 3 | `vlm_analysis` | VLM 逐窗语义分析 + PySceneDetect 边界修正 + 高光精确标注 | active |
| 4 | `confidence_check` | VLM 输出质量门控，按需触发 ASR 补充 | active |

### 内部步骤（vlm_analysis 内联）

| 步骤 | 说明 |
|------|------|
| 2.5 | PySceneDetect 边界修正 — 将 VLM 时间范围 snap 到精确帧边界 |
| 2.6 | 高光精确标注 — VLM highlight candidates 经 PySceneDetect 标注后写入 shots 表 |

## VLM 输出 Schema

VLM 输出使用 Pydantic v2 模型定义，位于 `autocut_core/schema/window.py`：

### `WindowAnalysisResult` — 单窗口完整输出

```python
class WindowAnalysisResult(BaseModel):
    source_id: str
    episode: int
    window_id: str
    window: Window              # {start: float, end: float}
    window_summary: str         # 窗口级叙事摘要
    timeline_segments: list[TimelineSegment]  # 时空识别 (present/flashback/dream)
    boundary_context: BoundaryContext         # 场景连续性
    character_appearances: list[CharacterAppearance]  # 角色出场记录
    scene_locations: list[SceneLocation]              # 场景位置
    story_beats: list[StoryBeat]             # 叙事节拍 + cause/effect
    dialogue_and_text: list[DialogueEvent]   # 对白/字幕 + 说话人 + 置信度
    visual_events: list[VisualEvent]         # 视觉事件 + emotion/conflict
    candidates: list[HighlightCandidate]     # 高光/hook 候选 + strength
```

### 子模型

| Model | 关键字段 |
|-------|---------|
| `TimelineSegment` | start, end, mode(枚举), entry_signal, exit_signal, summary |
| `StoryBeat` | start, end, function, summary, characters[], cause, effect, open_question |
| `DialogueEvent` | start, end, speaker_or_source, kind(枚举), text, confidence(枚举), source_accuracy |
| `VisualEvent` | start, end, description, characters[], emotion, action, conflict, visual_impact |
| `HighlightCandidate` | id, start, end, type(枚举), strength(1-10), reason, anchor, lead_in, payoff_or_open_question, dialogue_excerpt |
| `CharacterAppearance` | name, description, role, first_seen, last_seen, source |
| `SceneLocation` | name, description, start, end, time_of_day, characters_present[] |
| `BoundaryContext` | starts_mid_scene, ends_mid_scene, continues_from_previous_window, continues_into_next_window, start_state, end_state |
| `SourceAccuracy` | agreement(枚举), chosen_source(枚举), vlm_override_text, reason |

### 枚举类型

| 枚举 | 值 |
|------|-----|
| `TimelineMode` | present, flashback, flashforward, dream, unknown |
| `Confidence` | high, medium, low |
| `DialogueKind` | dialogue, screen_text |
| `CandidateType` | highlight, hook |
| `AGREEMENT_TYPES` | subtitle_match, subtitle_divergence, no_subtitle, screen_text_only |
| `CHOSEN_SOURCE` | subtitle, audio, screen_text |

### 向后兼容

`SourceAccuracy` 包含 `field_validator` 自动将旧版 agreement 类型（both_match, asr_only, vlm_override 等）映射为新类型。

## 注入策略

### 始终注入（VLM prompt 前缀）

```python
# 从 global_context 表读取，注入到每个窗口的 VLM prompt
global_context = {
    "synopsis": "全剧概括...",
    "themes": ["主题1", "主题2"],
    "relationships": [
        {"source": "角色A", "target": "角色B", "desc": "恋人"},
    ]
}
```

### 按需注入（confidence_check 触发）

| 条件 | 注入内容 |
|------|---------|
| 对白置信度 >20% 非 high | 触发 ASR 补充 |
| 无硬字幕检测 | 触发 ASR |
| 边界连续性异常 | 标记异常窗口 |
| 角色命名不一致 | 注入角色参考表 |

## Input Artifacts

| Artifact | Source | Required |
|----------|--------|----------|
| 本地视频文件或远程 URL 清单 | 外部输入 | yes |
| API 元数据（全剧概括/角色关系） | 外部 API | no（降级到剧本） |
| 完整剧本 | 外部输入 | no（降级到无注入） |

## Output Artifacts

| Artifact | Path Pattern | Consumer |
|----------|-------------|----------|
| Source Manifest | `source_manifest.json` | 全链路 |
| Window Manifest | `window_manifest.json` | 全链路 |
| 480p Compressed Windows | `window-assets/{source_id}/{window_id}-480p.mp4` | vlm_analysis |
| Global Context | `global_context` artifact | vlm_analysis |
| Window Summaries | `window-summaries.jsonl` | ac_series_knowledge |
| Confidence Report | `confidence_report.json` | Agent 决策 |

## References

| Document | Description |
|----------|-------------|
| [references/source-analysis.md](references/source-analysis.md) | 素材扫描、窗口切分、VFR 处理、480p 压缩 |
| [../../docs/design/vlm-first-architecture.md](../../docs/design/vlm-first-architecture.md) | VLM-First 架构设计文档 |
| [../../autocut_core/schema/window.py](../../autocut_core/schema/window.py) | VLM 输出 Pydantic Schema |

## 高光精确标注

VLM candidates (type=highlight) 经 PySceneDetect 标注后写入 `shots` 表：

| 字段 | 来源 | 说明 |
|------|------|------|
| `start_time`, `end_time` | VLM | 原始语义时间范围 |
| `precise_start`, `precise_end` | PySceneDetect | 精确帧边界 |
| `is_highlight` | VLM | true |
| `highlight_score` | VLM | strength (1-10) |
| `highlight_reason` | VLM | 高光理由 |
| `source` | 系统 | vlm / api / vlm+api |
| `window_id` | 系统 | 所属窗口 |

API 高光经 `global_context` 写入 `shots` 表 (source=api)，在 `series_registry` 阶段做 IoU 对比。

## Contract Rules

| Rule ID | Description | Status |
|---------|-------------|--------|
| rule_01 | VLM 为主：所有语义信息从视频直接提取，API/剧本仅提供全局上下文 | landed |
| rule_02 | 模型锁定：Doubao `doubao-seed-2-1-pro-260628`（多模态），Qwen `qwen3.7-max`（文本） | landed |
| rule_03 | 480p CRF32 压缩：平衡 VLM 识别精度与 API 成本 | landed |
| rule_04 | 置信度门控：low_conf >20% 或无硬字幕时触发 ASR 补充 | landed |
| rule_05 | 不注入视觉特征：VLM 能从画面得到的，不提前告诉它 | landed |
| rule_06 | 向后兼容：旧版 agreement 类型自动映射，不阻断已有数据 | landed |
| rule_07 | PySceneDetect 边界修正：VLM 时间范围 snap 到精确帧边界 | landed |
| rule_08 | API 高光不入库判断：API 高光只用于 skill 进化证据，VLM 独立判断 | landed |

## Quick Start

```bash
# 本地素材准备（含 480p 压缩）
python3 /absolute/skill/scripts/prepare_source_windows.py local \
  /absolute/input-root \
  --job-root /absolute/job \
  --backend doubao \
  --window-seconds 240 \
  --overlap-seconds 12 \
  --compress-to-480p

# 执行 VLM 逐窗分析
python3 /absolute/skill/scripts/run_semantic_batch.py \
  /absolute/job/window-analysis-batch.json \
  --backend doubao --workers auto --requests-per-minute 200

# 组装窗口摘要
python3 /absolute/skill/scripts/assemble_story_artifacts.py windows \
  /absolute/job/window-analysis-batch.json \
  --output /absolute/job/window-summaries.jsonl \
  --project /absolute/job/project.json
```

## Version History

- **v2.1.0** (2026-08-12): 新增 scene_boundary（PySceneDetect 边界修正）、高光精确标注、CharacterAppearance/SceneLocation Schema、API 高光保存规则
- **v2.0.0** (2026-08-12): VLM-First 架构 — 移除 ASR 转录、剧本解析、API 元数据采集、四源对齐。新增 global_context、confidence_check、480p 压缩。VLM 输出改用 Pydantic v2 Schema。
- **v1.0.0**: 原始版本 — 四源上下文合并（API/ASR/剧本/VLM）