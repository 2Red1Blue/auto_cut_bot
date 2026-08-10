---
name: ac_source_prep
description: 素材准备 — 视频切窗、API 元数据采集、ASR 转录、剧本对齐、四源上下文合并。覆盖 Stage 1-5，将原始视频转化为可供下游消费的窗口摘要和理解产物。Pipeline automation skill for auto cut bot.
metadata:
  auto_cut_bot:
    emoji: "📥"
    always: false
version: 1.0.0
stages: [1, 2, 3, 4, 5]
status: active
triggers:
  - "准备素材"
  - "视频切窗"
  - "source_windows"
  - "窗口分析"
  - "window_analysis"
  - "ASR 转录"
  - "素材扫描"
  - "剧本拆分"
  - "source_script"
  - "source_metadata"
anti_triggers:
  - "渲染视频" → 使用 ac_render
  - "QC 检查" → 使用 ac_qc
  - "生成故事" → 使用 ac_story_generation
  - "生成计划" → 使用 ac_plan_orchestration
---

# 素材准备 (ac_source_prep) — Stage 1-5

将原始短剧视频素材转换为结构化的窗口分析产物，供下游剧集理解和故事生成使用。

## Agent-Native Execution

你是流水线的编排器。按顺序调用以下工具：

### 执行顺序
1. `source_windows` → 扫描视频源，生成滑动窗口清单
2. `window_analysis` → 对每个窗口进行 VLM 分析

### 审核 → 写入流程
每个 Stage 执行完成后：
1. 审核 Stage 产物是否完整、正确
2. 审核通过 → 调用 `database_write` 写入 PostgreSQL
3. 审核不通过 → 报告问题，不写入
4. 降级: 如果 Agent 审查超时或失败，系统自动调用 `database_write` 写入 (auto_fallback=true)

### 工具调用规范
- 每个工具执行后，将产物路径存入 session state
- 下游工具从 session state 读取上游产物
- 失败时报告错误，不要静默跳过
- 所有路径使用绝对路径

### 输入
- `job_root`: 作业根目录
- `input_root`: 视频文件目录 (local 模式)
- 或 `url_list`: 远程 URL 清单 (remote 模式)

### 输出
- `source_manifest.json`: 视频源清单
- `window_manifest.json`: 窗口切片清单
- `window_summaries.jsonl`: 窗口分析结果

## Stages

| Stage | Name | Description | Status |
|-------|------|-------------|--------|
| 1 | `source_windows` | 素材扫描、窗口切分、VFR 处理、seek 精度 | active |
| 2 | `source_metadata` | API 元数据采集（角色+字幕+分镜） | planned |
| 3 | `source_transcripts` | FunASR 多模型对齐（Paraformer+cam+++SenseVoice） | planned |
| 4 | `source_script` | 完整剧本 LLM 拆分+字幕时间对齐 | planned |
| 5 | `window_analysis` | 四源上下文合并+降级矩阵 | active |

## Input Artifacts

| Artifact | Source | Required |
|----------|--------|----------|
| 本地视频文件或远程 URL 清单 | 外部输入 | yes |
| API 元数据（角色/字幕/分镜） | 外部 API | planned |
| 完整剧本 | 外部输入 | planned |

## Output Artifacts

| Artifact | Path Pattern | Consumer |
|----------|-------------|----------|
| Source Manifest | `source_manifest.json` | 全链路 |
| Window Manifest | `window_manifest.json` | 全链路 |
| Window Summaries | `window-summaries.jsonl` | ac_series_knowledge |
| Remote Download Manifest | `remote-download-manifest.json` | 下载进程 |
| Local Source Manifest | `local-source-manifest.json` | 本地 VAD/QC/Render |

## References

| Document | Description |
|----------|-------------|
| [references/source-analysis.md](references/source-analysis.md) | 素材扫描、窗口切分、VFR 处理、隐私与远程下载合同 |
| [docs/degradation-strategy.md](../../docs/degradation-strategy.md) | 降级框架：当上游信源缺失时的降级矩阵 |
| [docs/timeline-anchoring.md](../../docs/timeline-anchoring.md) | 时间锚定系统：多信源时间码对齐 |
| [docs/source-metadata-design.md](../../docs/source-metadata-design.md) | Stage 2 API 元数据采集设计 |
| [docs/asr-alignment-pipeline.md](../../docs/asr-alignment-pipeline.md) | Stage 3 FunASR 多模型对齐设计 |
| [docs/source-script-design.md](../../docs/source-script-design.md) | Stage 4 剧本 LLM 拆分设计 |
| [docs/window-analysis-context-merge.md](../../docs/window-analysis-context-merge.md) | Stage 5 四源上下文合并+信源优先级 |

## Contract Rules

| Rule ID | Description | Engine Status |
|---------|-------------|---------------|
| rule_01 | 全剧理解先于故事选择，故事脚本先于原片选段 | landed |
| rule_02 | Qwen 视频理解 `qwen3.7-plus`，文本归纳 `qwen3.7-max`，Doubao `doubao-seed-2-1-pro-260628`；并发对齐 `--workers auto` | landed |
| rule_03 | 完整性必须同时证明摄取覆盖和叙事覆盖 | landed |
| rule_04 | 所有事实/人物/故事线必须引用真实 Event/Fact ID，不得编造 | landed |

## Quick Start

```bash
# 本地素材准备
python3 /absolute/skill/scripts/prepare_source_windows.py local \
  /absolute/input-root \
  --job-root /absolute/job \
  --backend qwen \
  --window-seconds 240 \
  --overlap-seconds 12

# 远程 URL 素材准备
python3 /absolute/skill/scripts/prepare_source_windows.py remote \
  /absolute/video-urls.json \
  --job-root /absolute/job \
  --backend qwen \
  --window-seconds 240 \
  --overlap-seconds 12

# 执行窗口分析
python3 /absolute/skill/scripts/run_semantic_batch.py \
  /absolute/job/window-analysis-batch.json \
  --backend qwen --workers auto --requests-per-minute 0

# 组装窗口摘要
python3 /absolute/skill/scripts/assemble_story_artifacts.py windows \
  /absolute/job/window-analysis-batch.json \
  --output /absolute/job/window-summaries.jsonl \
  --project /absolute/job/project.json
```

## Recovery

- 窗口分析缓存失效条件：上游 Source Manifest、Window Manifest 或对应视频 SHA-256 变化时需重跑。
- 远程下载失败时按 Source ID 报错，不会回显签名 URL；已完成下载的 Source 可复用。
- 窗口重跑使用独立 Source/Clip SHA、范围和 policy 签名；原 Batch Job 保持不变。

## Version History

