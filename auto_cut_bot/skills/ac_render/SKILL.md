---
name: ac_render
description: 渲染输出 — 将 QC 通过的 Story Plan 编译为不可变 Render Recipe，使用本地 FFmpeg 从原始素材生成 1080×1920 正式 MP4 成片。覆盖 Stage 26。支持冷开场黑场分隔、Filler Tail 300s 兜底、内容寻址 Clip 缓存和可选注解字幕。Pipeline automation skill for auto cut bot.
metadata:
  auto_cut_bot:
    emoji: "🎥"
    always: false
version: 1.0.0
status: active
stages: [26]
triggers:
  - "渲染视频"
  - "生成成片"
  - "Render Recipe"
  - "story_render"
  - "本地渲染"
  - "正式渲染"
  - "编译 Recipe"
  - "成片输出"
  - "注解字幕"
  - "Filler Tail"
anti_triggers:
  - "Story QC" → ac_qc
  - "Story Plan 生成" → ac_plan_orchestration
  - "故事脚本" → ac_story_generation
tools:
  - ffmpeg_video_editor  # FFmpeg command generation from natural language
  - db_query  # schema discovery + raw SQL
---

# ac_render — 渲染输出

把 QC 后 Winner Publisher 已发布的唯一有效 Story Plan 编译为不可变 Render Recipe，使用本地 FFmpeg 从原始素材生成正式 MP4 成片。不重新选择 Story、Beat、Span、Block 或时间码。

## Stage Range

| Stage | Name | Description |
|-------|------|-------------|
| 26 | `story_render` | Render Recipe 编译 + 本地正式渲染 + 验证 + 可选注解 |

## 快速开始

```bash
# 编译 Recipe（本地素材已含有效 path）
python3 /absolute/skill/scripts/build_story_render_recipes.py /absolute/job

# 远程素材需显式指定本地下载清单
python3 /absolute/skill/scripts/build_story_render_recipes.py \
  /absolute/job --local-source-manifest /absolute/local-download-job/source_manifest.json

# 验证 → 渲染 → 最终验证
python3 /absolute/skill/scripts/validate_story_render_recipes.py /absolute/job
python3 /absolute/skill/scripts/render_story_videos.py /absolute/job --jobs 2
python3 /absolute/skill/scripts/validate_story_renders.py /absolute/job

# 可选：生成注解字幕（非 delivery 产物）
python3 /absolute/skill/scripts/build_story_annotations.py /absolute/job \
  --story-id story-xxx --with-captioned-mp4
```

## 输入 / 输出

- **输入**：`story-plans/index.json`（Winner 发布后）、`story-qc/index.json`、`source_manifest.json`、本地原片
- **输出**：`story-render-recipes/index.json`、`story-renders/<slot>-<story-id>.mp4`、`story-render-validation.json`
- **可选**：`story-annotations/<story-id>.srt`、`story-annotations/<slot>-<story-id>.captioned.mp4`
- **副作用**：`.render-cache/`（内容寻址 Clip 缓存，跨 Story 复用）

## 参考文档

| 文档 | 用途 |
|------|------|
| [references/render-design.md](references/render-design.md) | 渲染设计：Recipe 编译、Timeline、转场、Filler Tail、部分重渲染 |
| [references/短剧信息流剪辑知识库_编导思路版_内部对齐.md](references/短剧信息流剪辑知识库_编导思路版_内部对齐.md) | 剪辑知识库 — 编导思路版 |
| [references/短剧信息流剪辑知识库_技术诊断版_内部对齐.md](references/短剧信息流剪辑知识库_技术诊断版_内部对齐.md) | 剪辑知识库 — 技术诊断版 |
| [references/短剧信息流素材剪辑知识库_第一部分_分剧集类型剪辑思路.md](references/短剧信息流素材剪辑知识库_第一部分_分剧集类型剪辑思路.md) | 剪辑知识库 — 分剧集类型 |

## 合同规则

| Rule | 描述 | 状态 |
|------|------|------|
| 31 | 正式渲染使用 QC 绑定的有效 Plan | 文字约定 |
| 32 | Teaser→正文黑场唯一性（0.35s）、mode=none 无转场 | 已落地 |
| 33 | 本地原片、1080×1920/25fps/H.264/AAC 规格 | 文字约定 |
| 34 | Recipe/MP4/报告 SHA-256 绑定链 | 文字约定 |
| 35 | 正式渲染范围排除项（无 J-cut/L-cut、字幕、BGM、调色） | 文字约定 |
| 36 | Filler tail 300s 兜底（拼到集尾 + 跨集追加整集） | 文字约定 |

详见 [shared_contracts/references/contract-rule-matrix.md](../shared_contracts/references/contract-rule-matrix.md)。

## 关键约束

- **准入条件**：QC 报告 `status=approved`；或 `review` + `--include-review`；auto 模式只接受 `--include-auto-safe-review`（全部 finding 为 `fade_fallback` 白名单）
- **输出规格**：1080×1920、25fps、H.264 libx264 CRF 18、yuv420p、AAC 48kHz stereo 192kbps、faststart
- **转场**：`single_highlight` 在 Teaser 末—正文首插入一次 0.35s 黑场静音（两侧 0.18s fade）；`mode=none` 不插入
- **Filler Tail**：不足 300s 时拼到集尾再跨集追加整集，候选集耗尽不阻断
- **内容寻址缓存**：每个 Clip 按 source_sha256 + 时间范围 + 编码参数独立缓存，单 Clip 修改只重渲染 1 个 Clip

## 已知问题

- 不支持通用 J-cut/L-cut、动态转场、标题卡、字幕烧录、BGM、响度美化、调色、补帧
- 不支持远程渲染或自动上传
- 仅 `reviewed_bridge` / `right_av_overlap` 作为受控音视频分离例外

## 修改记录

## Agent-Native Execution

使用 db_query 自主查询数据库，不在 Pipeline Stage 硬编码顺序中执行。

1. db_query(operation="schema") → 发现可用数据
2. db_query(operation="raw", sql="...") → 按需查询
3. 在上下文中处理数据（LLM 推理或编译）
4. database_write → 写回 DB
5. 上下文已有数据 → 不重复查询
