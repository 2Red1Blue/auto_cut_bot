# Research: FunASR 在 Pipeline 中的当前使用方式

- **Query**: FunASR 模型是否已在 pipeline 中被调用？predict_timestamp？字级时间戳？ASR 结果存储？source_transcripts stage？
- **Scope**: internal
- **Date**: 2026-08-14

## Findings

### 1. FunASR 已在 Pipeline 中被调用 — 两条路径

#### 路径 A: `AsrTranscriptStage`（core pipeline stage）

| 属性 | 值 |
|---|---|
| 文件 | `autocut_core/stages/ac_source_prep/asr_transcript/stage.py` (891行) |
| 触发条件 | **条件触发** — VLM-first 架构，仅在 confidence_check 判定 VLM 无硬字幕/低置信度时触发 |
| 调用方式 | HTTP POST multipart 到 FunASR `/recognition` 端点 |
| 端点配置 | `config.asr_endpoint`（默认空 = 不使用） |
| 并发 | ThreadPoolExecutor，默认 3 workers |
| 重试 | 最多 5 次，指数退避（502/503/504 有专门处理） |

**请求体**: 仅发送 `audio` 文件 + 可选 `language` 参数
- **不请求字级时间戳**: 请求中没有 `predict_timestamp` 或类似参数
- **返回格式**: `{"text": "...", "sentences": [{"text": "...", "start": 0.0, "end": 1.0}, ...], "code": 0}`

**`_parse_segments()` 解析** (L593-614):
```python
# 仅提取句子级 start/end/text
segments.append({
    "start_time": sentence.get("start", 0.0),
    "end_time": sentence.get("end", 0.0),
    "text": seg_text,
})
```

#### 路径 B: `SourceTranscriptsTool`（agent-native tool）

| 属性 | 值 |
|---|---|
| 文件 | `auto_cut_bot/agent/tools/pipeline/source_transcripts.py` (157行) |
| 触发方式 | Agent 作为 subagent tool 调用 |
| 端点 | 默认 `http://localhost:8001/recognition` |
| 功能 | 与路径 A 类似但更简单，额外做 merge_operator 合并 |

### 2. `predict_timestamp` / 字级时间戳

**当前状态: 未使用**

- 代码中**没有任何地方**传递 `predict_timestamp=True` 或类似参数
- FunASR 端点返回的是**句子级** start/end，不是字级/词级
- `_parse_segments()` 只提取 `sentences[].start/end/text`
- 设计文档 `vad-audio-snap-design.md` 中明确记录:
  > "当前项目已有FunASR ASR端点（条件触发），但ASR输出是句子级start/end，没有词级时间戳"
  > "FunASR paraformer + timestamp: 中文/英文, 字级20-50ms, FunASR(已有端点), ✅ 最优中文方案"

### 3. ASR 结果存储

#### DB 表: `subtitles`

通过 `StageDBClient.insert_subtitles()` 写入 (`db/client.py:783-850`):

```sql
INSERT INTO {schema}.subtitles (
    book_id, episode_id, start_time, end_time, speaker,
    text, tone, emotion, group_id, group_tone, source,
    confidence, cer_estimate, kind, language, text_zh, position
) VALUES (...)
ON CONFLICT (book_id, episode_id, start_time, source) DO UPDATE SET ...
```

- `source` 字段区分来源: `"asr"` | `"api"` | `"script"` | `"vlm"`
- UPSERT 语义: 同 book_id + episode_id + start_time + source 冲突时合并而非重复

#### 文件产物

| 产物 | 路径 | 说明 |
|---|---|---|
| source_transcripts.json | `{job_root}/source_transcripts.json` | SourceTranscriptsTool 产出 |
| asr_transcript artifact | 通过 ArtifactBus 发布 | AsrTranscriptStage 产出 |

### 4. `source_transcripts` Stage 实现状态

**已实现**，但有两个入口:

| 入口 | Stage 名 | 状态 |
|---|---|---|
| `AsrTranscriptStage` | `asr_transcript` | 完整实现（891行），含并发转录、落库、交叉验证、说话人识别 |
| `SourceTranscriptsTool` | `source_transcripts` | Agent-native 简化版（157行），含 merge_operator |

`asr_transcript` 的 contract:
```python
StageContract(
    stage_name="asr_transcript",
    input_artifacts=["source_windows"],
    output_artifacts=["asr_transcript"],
    db_reads=["subtitles"],
    db_writes=["subtitles", "speaker_mappings", "boundaries"],
)
```

在 pipeline state 中的位置:
```python
# state.py L15
"source_windows", "source_transcripts", "window_analysis", "event_cards", ...
```

### 5. FunASR 返回格式（当前）

```json
{
  "text": "完整文本",
  "sentences": [
    {"text": "第一句话", "start": 0.0, "end": 2.5},
    {"text": "第二句话", "start": 2.8, "end": 5.1}
  ],
  "code": 0
}
```

**没有**:
- `word-level` / `char-level` timestamps
- `predict_timestamp` 请求参数
- pinyin/音素级别对齐

## Caveats

1. **FunASR 端点必须手动部署**: 默认 `asr_endpoint=""` 为空 = 不使用 ASR
2. **条件触发**: VLM-first 架构下，asr_transcript 通常被 `_should_skip_asr()` 跳过
3. **字级时间戳扩展成本低**: FunASR paraformer 原生支持 `predict_timestamp=True`，只需在请求中添加参数并扩展 `_parse_segments()` 解析
4. **FunASR 源码在仓库中**: `/Users/liuzx/Code/python/work_ai/FunASR/` 有完整的 FunASR 源码
