# Story Evidence Retrieval

## 目的

把已批准的 Story Script 转换为确定性、自包含、可追溯的原片证据包，供下一阶段
Span Candidate Compiler 使用。Evidence Packet 只负责召回“哪些原片证据与这个
Story/Beat 有关”，不决定最终剪辑边界、播放顺序、转场或成片。

## 入口合同

只处理同时满足以下条件的 Story：

- `decision=approved`。
- 当前 Story Script SHA-256 等于 `approved_script_sha256`。
- Story Script、审批记录和当前 Story Portfolio 的 SHA-256 一致。
- `production_slot` 未变化。
- `not_feasible` 不得进入；`partial` 必须已显式接受风险。
- 正常生产时，已批准 Story 数量和生产槽位完整覆盖 `1..N`。

身份/哈希条件不满足时停止。某个已批准 Story 自身证据覆盖不完整时，该 Story
Packet 标为 `incomplete`，批次在仍有可用兄弟 Story 时标为 `partially_ready`；
不得改写其 Approval，也不得阻断其他 Story。

## 命令

```bash
python3 /absolute/skill/scripts/build_story_evidence_packet.py \
  /absolute/job/story-approval.json

python3 /absolute/skill/scripts/validate_story_evidence.py \
  /absolute/job
```

默认读取同一任务目录中的：

- `series-bible.json`
- `event-cards.jsonl`
- `highlight-hook-catalog.json`
- `source_manifest.json`
- `window_manifest.json`
- `window-summaries.jsonl`

默认输出：

```text
story-evidence/
  index.json
  <story-id>.json
story-evidence-review.md
story-evidence-validation.json
```

## 召回规则

逐 Beat 编译以下直接种子：

- `beats[].event_ids`
- `beats[].must_show[].evidence_event_ids`
- `retrieval_requirements.event_ids`
- `retrieval_requirements.thread_beat_ids`
- `candidate_suggestions`
- `retrieval_requirements.candidate_ids`

再按 Retrieval Requirements 扩展：

- Fact → `facts[].event_ids`
- Character → `characters[].evidence_event_ids`
- Relationship → `state_changes[].event_id`
- Story Thread → `story_threads[].event_ids`
- Thread Beat → `thread_beats[].event_ids`
- Candidate → `candidates[].event_ids`

执行 `lookback`：

- `same_episode`：实体扩展只保留直接种子所在集。
- `earlier_episodes`：实体扩展只保留不晚于直接种子最晚集的 Event。
- `whole_series`：允许全剧范围的同线实体扩展。

Thread Beat 是逐集义务的主索引；Story Thread 全量 Event 扩展只作为补充上下文，
不能用来证明某个 required Thread Beat 已覆盖。直接种子不受 lookback 裁剪。第一版固定
`semantic_search_used=false`、`vector_search_used=false`；结构化召回不足时保留缺口，
不要用文本相似结果掩盖缺证据。

must-show Fact 对应的 Event 也属于 Fact 扩展，只能进入 Context。Packet 必须在
must-show 和 Beat 两层分别保存 `direct_event_ids` 与
`fact_context_event_ids`；兼容 `resolved_event_ids` 只是两者并集。任何
`observable_via` 的 must-show 都必须至少有一个显式、可定位
`evidence_event_ids` 才能标为 `covered`，不得用 Fact 关系代替画面/对白/动作证据。

### 分层原片范围

每个 Beat 同时保存：

- `direct_range_refs`：Beat、must-show、显式 Retrieval Event 及显式 Candidate
  所绑定 Event 的直接范围。
- `candidate_range_refs`：Script 显式引用的 Highlight/Hook Candidate 自身范围。
- `context_range_refs`：人物、关系、Fact、Thread 等实体扩展召回的 Event 范围。
  由这些 context Event 关联出的 Candidate 范围也必须留在本层。
- `range_refs`：以上三层的兼容并集。

下游不得再无差别使用 `range_refs`。`tight` 只能从 direct/candidate 层起锚；
context 层只用于生成更宽的 scene/context 选项。这样实体同线扩展不会把高光锚点
自动放大为整集。

## 相邻窗口

从 Event/Candidate 的 `evidence_window_ids` 出发，默认向前后各扩展一个连续窗口。
Manifest 未显式保存前后指针时，按同一 Source 的开始时间推断。

相邻窗口用于提供：

- 进入场景前的人物状态。
- 对白、动作和反应是否被窗口边界截断。
- Event 之后的直接结果。
- 后续 Span Candidate 的自然场景扩边依据。

它仍然是理解证据，不是最终 Clip 边界。

## Packet 状态

- `ready`：所有 Beat 均有可定位证据，未遗留定向视频复核风险。
- `needs_video_review`：证据包完整，但存在 partial、非线性返回、屏幕文字、
  Highlight/Hook 边界等风险。
- `incomplete`：至少一个 must-have Beat 或 required Thread Beat 没有可定位证据；
  不得进入正式 Story Plan。
- Evidence Index 的 `partially_ready`：至少一个 Packet incomplete、同时至少一个
  Packet 可继续；Span 只编译可继续的 Packet。

允许 `needs_video_review` 进入 Span Candidate Compiler，但必须把风险和待复核窗口继续携带。

## 失效规则

Packet 绑定：

- Story Approval SHA-256
- Story Script SHA-256
- Story Portfolio SHA-256
- Series Bible SHA-256
- Event Cards SHA-256
- Candidate Catalog SHA-256
- Source/Window Manifest SHA-256
- Window Summaries SHA-256

修改一个 Story Script 只重建该 Story 的审批和 Evidence Packet。修改共享证据源时，
重建所有引用该输入指纹的 Packet。

## Evidence 阶段边界

Evidence Packet 与验证报告生成后，才允许进入 Span Candidate Compiler。在
Evidence Retrieval 阶段不得：

- 决定精确 Clip 起止点。
- 安排非线性播放顺序。
- 生成 Story Plan、QC、转场或 MP4。

## Thread Beat 覆盖

Packet schema `1.2` 使用 `structured-thread-beat-recall-v4`。显式
`retrieval_requirements.thread_beat_ids` 对应的 Event 是 direct seed，不受
`lookback` 裁剪；`lookback` 只约束 Fact、Character、Relationship、Story
Thread 等上下文扩展。Packet 明确保存：

- `coverage_summary.required_thread_beat_ids`
- `coverage_summary.covered_thread_beat_ids`
- `coverage_summary.missing_required_thread_beat_ids`
- `beat_evidence[].resolved_thread_beat_ids`
- `beat_evidence[].direct_event_ids`
- `beat_evidence[].fact_context_event_ids`
- `beat_evidence[].must_show_evidence[].direct_event_ids`
- `beat_evidence[].must_show_evidence[].fact_context_event_ids`
- `evidence_catalog.thread_beats`

Span Candidate Compiler 继续派生 `supports_thread_beat_ids`。后续 Story Plan 只有在
实际选中的 Span 支撑全部 required Thread Beat 时才能进入视频 QC。
