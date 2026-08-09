# Story QC、代理渲染与状态分类

## 目的

Story QC 在已通过 Plan Validator 的 Story Plan Candidate 之后执行，回答三个问题：

1. `Story Coverage`：已批准 Script 的 must-have Beat、must-show、Payoff 和
   Hook 是否真的被选中原片覆盖。
2. `Story Flow`：这些原片按计划顺序连续播放时，观众是否能够理解人物、时空、
   因果、非线性返回和局部兑现。
3. `Cut Safety`：每个 Clip 的入点、出点、相邻连接、对白、动作、反应和音频
   是否能够安全保留。

Story QC 不重新选择故事、不自由改写时间码、不选择正式转场，也不生成正式成片。
吞字、词/音节截断和切点语音活动固定由本地双路 VAD 判断，不依赖 Qwen 视频判断。
12 秒内能够确定性向外恢复的边界在代理渲染前进入单轮 Boundary Repair；基础
Story Plan 不变，QC 改为绑定派生 Plan Index。无法安全扩边但可受控降级的边界
进入 `fade_fallback`，数据或硬合同错误仍进入 `blocked_replan`。

Story Plan 的结构性硬约束（must-have Beat / must-show / required Thread Beat
覆盖、sequence_edges 连贯性、viewer_knowledge 前置 Fact、Clip 时间码位于
Source 内、Teaser 15 秒、Story ≤1200 秒、重复率、整集率等）由
`materialize_story_plans.py` + `validate_story_plans.py` 在进入 QC 前已确定性
保证。QC 阶段不再复述这些静态断言，Coverage / Flow / Cut Safety 结论只来自：

- QC 独有的 Proxy 结构检查（`plan-ready` 记录 Plan/Admission 状态，
  `proxy-av-streams`、`proxy-duration` 校验 QC Proxy 实际渲染结果）。
- 本地 Demucs + 双路 Silero VAD 逐切点分类（`local-audio-boundary-<clip>`）。
- Qwen `qwen3.7-plus` 对完整代理与 Junction 的 Coverage / Flow / Cut Safety 判断。

## 输入

Candidate Arena 模式只接受：

- 当前 `story-plan-candidates/index.json` 与 `story-plan-validation.json`。
- `status=ready_for_video_qc` 的 Candidate 按 Story 与 rank 分轮准入；每个已执行 Candidate 投影到独立 QC
  workspace，复用同一套 Outro Sanitizer、VAD、Boundary Repair、Proxy、Qwen QC、
  Aggregator 与 Validator，缓存和派生 Plan 不跨 Candidate 共用。
- 当前正式 `story-plans/index.json` 在 Winner 发布前必须保持 `stale`。

旧单 Plan 兼容模式只接受：

- 当前有效的 `story-plans/index.json`。
- `status=ready_for_video_qc` 的 Story Plan；或已物化、`status=blocked` 且不含
  连续性硬错误，并由有效 `story-plan-qc-admission.json` 明确标记
  `accepted_for_qc` 的 Plan。
- Story Plan 所绑定的批准 Script。
- 当前 `source_manifest.json`。
- 本地原片路径。远程任务默认读取任务根目录的
  `local-source-manifest.json`；相关 Source 必须已下载并通过探测与哈希校验。

Story Plan、Script、Source Manifest、本地下载源、音频 Plan/报告或 QC Batch
的 SHA-256 变化后，旧 QC 代理和报告失效。

可选输入 `junction-edit-constraints.json` 只接受操作员已复核的视觉安全区间和
禁画区间。它不会让模型寻找插画镜头，也不会改变 Story Plan；Boundary Repair
结束后，本地确定性编译器将它转换为 `junction-edits/index.json` 和逐 Story Edit
Plan，并把 Plan、约束、Source Manifest 与本地源 SHA-256 全部绑定。

人工 Admission 只改变 QC 准入，不修改基础 Plan 的 `blocked` 状态、指标、
`blocked_reasons` 或 `repair_routes`。Admission 必须绑定 Plan Index、Plan 和原始
blocked reasons 的 SHA-256，并写入 Proxy Manifest 与正式 QC 报告。Plan 或原因变化后
旧 Admission 自动失效；`dialogue_incomplete`、`same_source_causal_gap`、缺少当前
continuity contract 的旧 Plan，以及尚未物化时间轴的 preflight 失败都不能放行。

### Junction Edit 约束合同（可选）

```json
{
  "schema_version": "2.0",
  "method": "operator-junction-edit-constraints-v2",
  "edits": [
    {
      "id": "remove-unwanted-tail",
      "story_id": "story-broad-003",
      "effect": "audio_tail_visual_repair",
      "strategy": "right_av_overlap",
      "from_clip_id": "clip-002",
      "to_clip_id": "clip-003",
      "left_video_end_seconds": 94.8,
      "right_entry_visual_review": "safe",
      "forbidden_visual_ranges": [
        {
          "source_id": "ep001",
          "start_seconds": 94.8,
          "end_seconds": 95.84,
          "reason": "operator-reviewed unwanted shot"
        }
      ],
      "reason": "preserve dialogue tail without showing forbidden frames"
    }
  ]
}
```

v2 约束的 `effect` 固定为 `audio_tail_visual_repair`，`strategy` 只能是
`reviewed_bridge` 或 `right_av_overlap`。旧 v1 `audio_tail_over_bridge` 约束和已落盘
Junction Plan 继续按 `reviewed_bridge` 读取，效果语义不变。

`right_av_overlap` 必须是同集相邻正文 Clip，右侧入口经过人工视觉复核，左尾音不超过
1.2 秒，且编译器必须绑定当前双路 VAD 报告并证明重叠区同时对白不超过 0.1 秒；右侧
画面和原声入口都保持 Plan 原值。`reviewed_bridge` 继续按正式 25fps 向上取整桥接帧数，
桥接原声静音，最多只补不足一帧的尾部静音。两种策略都禁止保留画面或替代画面命中
禁画区间、替换 Teaser→正文黑场或与同一 Junction 的 `fade_fallback` 叠加；直接
overlap 还禁止作用于任一 Teaser/filler Clip。

## 阶段

### 1. 准备 QC 代理和视频复核任务

```bash
python3 /absolute/skill/scripts/story_candidate_qc.py prepare \
  /absolute/job --backend qwen \
  --local-audio-source-manifest /absolute/local-download-job/source_manifest.json \
  --audio-boundary-python /absolute/job/.venv-audio-boundary/bin/python \
  --candidate-rank 1 --allow-partial

python3 /absolute/skill/scripts/run_semantic_batch.py \
  /absolute/job/story-qc-candidate-batch.json \
  --backend qwen --workers auto --requests-per-minute 0

python3 /absolute/skill/scripts/story_candidate_qc.py assemble /absolute/job

python3 /absolute/skill/scripts/publish_story_plan_winners.py /absolute/job

python3 /absolute/skill/scripts/validate_story_qc.py /absolute/job
```

旧单 Plan 兼容命令仍为：

```bash
python3 /absolute/skill/scripts/prepare_story_qc.py \
  /absolute/job --backend qwen \
  --local-audio-source-manifest /absolute/local-download-job/source_manifest.json \
  --audio-boundary-python /absolute/job/.venv-audio-boundary/bin/python
```

每个 Candidate 生成：

- 必要时生成一轮不可变音频 Boundary Patch 与派生 Story Plan。
- 一份按 Story Plan 顺序拼接的完整低码率 `story-proxy.mp4`；默认硬切，有已编译
  Junction Edit 时应用与正式渲染相同的音画效果。
- 一份覆盖全部 Clip 入点/出点的本地音频边界报告。
- 每个相邻 Clip 的 Junction 预览。
- 一份 `proxy-manifest.json`。
- 一组严格 `story_video_qc` 多模态任务。

中间媒体位于：

```text
story-qc-candidates/workspaces/<plan-candidate-id>/
  .qc-cache/story-qc/<story-id>/<plan-hash>-<source-hash>/
```

它们是可重建缓存，不是新的顶层业务实体。

完整 Story Proxy 默认使用 360×640、H.264 180 kbps、AAC 48 kbps，保证
5–20 分钟视频能够作为低成本连续叙事代理。Junction 使用更高分辨率和码率。

代理视频：

- 严格使用 Story Plan 的 Clip 顺序和边界。
- 完整 Story Proxy 默认只使用 hard cut；逐 Junction 的 `teaser_to_body` 预览实际
  应用正式 0.35 秒黑场静音与两侧 0.18 秒 fade，让模型按最终可见效果复核。
- 同一 Source 上时间轴精确连续（0.05 秒容差）的相邻 Clip 不生成 Junction
  代理或模型任务；它没有删除源帧，完整 Story Flow 已覆盖整体叙事。
- 同一 Source 但时间轴不连续的 `intra_episode` 是集内非连续压缩剪辑，仍生成 Junction
  任务。它只结合 `planned_story_transition` 复核删减后人物关系、因果和剧情推进
  是否可懂；人物镜头、构图、动作或场景没有无缝接续，不能单独构成
  `review/block`。
- 另一受控例外是已通过本地编译的 pair-level Junction Edit：`reviewed_bridge`
  保持旧 `audio_tail_over_bridge` 的静音桥接语义；`right_av_overlap` 让右侧
  画面和原声从 Plan 原入口同步开始，并把左侧尾音混入右侧头部。
- 除上述效果态预览外，不提前应用正式转场、音频淡化、自动补帧或会掩盖问题的修复。

Junction Edit 编译、效果态代理和本地渲染均不调用远程模型。它只改变受影响
Story 的 Story Flow 请求签名和对应 Junction 请求签名；普通 Junction 使用
`story-video-qc-v10-junction-content-addressed`，存在 Junction Edit 时使用
`story-video-qc-v10-junction-edit-content-addressed`。两者都把批准 Script 的计划剧情
投影、左右 Source 精确范围、转场/效果、人物 aliases、Series Bible SHA、
Prompt/Schema/Stage 和逻辑媒体内容纳入请求签名；Candidate workspace 本地路径
不参与签名。只有完整语义 Context 与逻辑媒体输入都相同时才复用；
Story Flow stage、Window Analysis 和上游 Story Plan 保持不变。

准备阶段调用已安装的 `audio_boundary_guard.py`：

- `references/story-audio-boundary-policy.json` 是 guard、音频计划和音频报告的
  唯一运行时策略源；`qc-rules.json.audio_boundary` 只是逐字段一致的兼容镜像。
  Policy 版本、12 秒扩边阈值或 fade fallback 参数变化时，旧音频报告必须因完整
  policy 指纹不匹配而失效，不得继续读取镜像中的旧阈值。

- Demucs `4.1.0` 分离分析用人声。
- Story QC 实际探测 `<TORCH_HOME>/hub/checkpoints` 的可写性；环境变量缺失或
  只读时自动使用 `<job_root>/.torch-cache`。并发音频 shard 启动前只预取一次
  固定 Demucs 模型；任务 fallback 也不可写或模型下载失败时，在 shard 启动前
  给出类型化错误，不把环境失败伪装成逐切点 `analysis_error`。
  缓存路径、来源和 fallback 原因会跨子进程保留到音频元数据。兼容旧版
  `audio_boundary_guard.py` 时，如其不支持 `prefetch-demucs`，会输出
  `DEMUCS_MODEL_PREFETCH_SKIPPED` 后继续分析；所有 shard 仍显式共用已探测可写的
  `TORCH_HOME`。
- Silero VAD `6.2.1` + ONNX Runtime `1.24.3` 分析原始混音和人声。
- 两路语音区间取并集，任一路命中即否决切点。
- 同源相邻 Clip 在同一时间点连续播放时，该共同点不是两个独立音频切口；报告
  保留原始语音活动证据，但确定性归类为 safe，不生成相反方向的双扩边建议。
- `source_start` 至少保留语音前 0.15 秒，`source_end` 至少保留语音后 0.25 秒。
- 建议移动不超过 12 秒时给出精确建议；超过 12 秒由 Boundary Repair 记录
  `fade_fallback`，不继续扩边。
- 物理源边缘若没有活跃语音可按声学通过；边缘处已有活跃语音且无法继续扩边时
  记录 `fade_fallback` 并在最终 QC 保留 `review`，不能自动 verified。
- VAD 不识别文字和句意，双路静音不能单独证明整句语义完整。

### 1.1 自动 Boundary Repair

在音频 Boundary Repair 之前先运行纯本地 `Local Outro Sanitizer`：

- 只扫描已下载 Source 的物理尾部，不上传媒体、不调用远程模型。
- 用跨集重复出现的暖色粒子/光斑时序特征建立 cohort 证据，只有高置信度命中
  且 Clip 触及物理集尾时才允许 `source_end` 向内收缩。
- 输出 `story-outro-sanitizer.json`、`story-plan-repairs/round-00.index.json`
  和不可变 `round-00.outro.patch/plan.json`；基础 Plan 不覆盖。
- `round-00` 裁点是后续音频修复的硬上限。音频修复不得向外扩回已识别的包装
  区间；发生冲突时保留裁点并使用 `fade_fallback`。
- 裁尾后的有效 Plan 重新执行完整双路 VAD、代理渲染和视频 QC。

`prepare_story_qc.py` 默认在代理渲染前自动运行修复控制器：

1. 只处理 `adjustment_required`。
2. `source_start` 只能前移，`source_end` 只能后移。
3. 自动修复只执行一轮，单个边界调整不得超过 12 秒。
   同源相邻 Clip 的正 gap 不超过 policy `minimum_safe_gap_seconds` 时，只允许把
   左 Clip 单侧延长到既有右 Clip 入口；不得左右分别扩到各自安全点造成重放。
4. 调整不得越过 Source 物理范围，也不得破坏 Story ≤1200 秒、
   Teaser 15 秒及未经 `teaser_reprise` 声明的同源重叠硬约束。
5. 基础 `story-plans/*.json` 永不覆盖；本轮写入 Patch、完整派生 Plan 和
   有效 Plan Index。
6. 修复后重新执行双路 VAD，不继续叠加第二轮。
7. 建议扩边超过 12 秒、音频门禁返回 `blocked_replan`、源边缘活跃语音，或
   复检仍不安全时进入 `fade_fallback`；方向错误、非有限时间码、
   `analysis_error` 或违反 Plan 硬合同才保留 `blocked_replan`。

派生 Plan 必须重新计算并验证时长、重复率、整集率、Source Usage 与
Editorial Metrics；不得用“扩边幅度较小”代替重验。
重叠校验以父 Plan 为基线，只阻断本轮新建或放大的非 reprise 重叠；父 Plan 中
未变化且已通过 Plan Validator 的重叠不能否决无关边界修复。

正式修复产物：

```text
story-boundary-repair.json
story-plan-repairs/
  round-01.index.json
  <story-id>/
    round-01.patch.json
    round-01.plan.json
```

Patch 保存父 Plan、触发修复的音频报告、before/after 时间码、调整量和结果
Plan 的 SHA-256。有效 Plan Index 同时绑定基础 Plan Index 和完整 Patch 链。

Clip 与 Junction 代理使用内容寻址共享缓存。边界变化后，只重新编码变化的 Clip
及其相邻 Junction；完整 Story Proxy 和 Story Flow 必须随派生 Plan 重建。没有
变化的 Junction 继续复用相同媒体与上下文指纹。

若存在 Junction Edit，缓存键额外包含逐 Story Junction Edit Plan SHA-256；只重建
受影响 Story Proxy、Story Flow 和被编辑的 Junction，其他 Junction 的媒体、上下文
与请求签名保持不变。

可使用 `--disable-auto-audio-repair` 关闭自动修复用于诊断；正式流程不得以此
绕过阻断。

### 2. 运行 Selected Video QC

```bash
python3 /absolute/skill/scripts/run_semantic_batch.py \
  /absolute/job/story-qc-batch.json \
  --backend qwen --workers auto --requests-per-minute 0
```

Qwen 使用 `qwen3.7-plus`：

- `story_flow`：检查 Coverage、Flow 和完整代理中可见的 Cut Safety。
- `junction`：检查相邻 Clip 的叙事连接和切点。

每个 Junction context 都包含 `planned_story_transition.left/right`：它从相邻
Clip 所属 Block 和 Event/Candidate 身份命中批准 Script Beat，携带 Beat ID、角色、
剧情目的、叙事描述、可观察内容和预期人物 ID。该字段只说明“计划在此发生什么”，
不是画面正确性的先验证明。右侧明确计划为命名新人物入场时，不得只因首次出现就判
`character_identity_confused`；若画面人物、动作或可辨认性与批准剧情矛盾，仍应
正常输出 review/block。

Junction 代理只包含切点左侧尾部与右侧头部短 handle，不包含两条 Clip 或 Beat 的
完整内容。因此它只能判断局部切点本身，不能用这几秒判断完整 must-show、Payoff 或
Teaser reprise 是否出现。Junction strict Schema 的 finding code 不包含
`coverage_missing`、`must_show_absent`、`payoff_absent`、
`teaser_reprise_missing`，Validator 对遗留越权响应同样拒绝；模型也不得把“局部未
展示完整 Beat”改写为 `thread_broken`。完整 Coverage 继续由完整 Story Flow 代理判断。

当前 Series Bible 会把计划人物的 canonical name、aliases 与 identity 投影到
`character_reference` 并通过 context SHA-256 进入请求签名。该数据只用于身份消歧，
不是画面通过的先验证明；年龄阶段、称谓或外观 alias 属于同一人物时不得据此制造
`character_identity_confused`，真实人物矛盾仍正常阻断。

其中 `intra_episode` 到达模型时必然已排除同源时间轴连续的 Clip pair，因此语义是
“删去中间源片区间后的集内压缩”，不是无缝接镜。模型仍须阻断真正无法理解的人物
关系、因果或剧情推进，但不得以左右画面主体、构图、动作或场景不同本身代替叙事判断。

Qwen 不再生成 `boundary_start`/`boundary_end` 任务，也不得对吞字、词音节或
说话截断给出通过结论；这些项目以本地双路 VAD 为准。

每个任务使用独立 strict JSON Schema，并把 Schema 纳入请求签名：

- `story_id`、`review_id`、`review_kind` 固定为任务上下文的 `const`。
- `story_flow` 的 Coverage、Flow、Cut Safety 只能为 `pass/review/block`，
  `verified_boundary` 固定为 `not_applicable`。
- `junction` 的 Coverage 固定为 `not_assessed`，Flow、Cut Safety 只能为
  `pass/review/block`，`verified_boundary` 固定为 `not_applicable`。
- `junction` 的 finding code 只允许 Flow/Cut Safety 范围；完整 Coverage code
  仅在 `story_flow` Schema 中可见。

模型只能输出：

- `pass`
- `review`
- `block`

模型不得改写 Story Plan、生成新剧情或猜测代理视频未展示的内容。

### 3. 汇总 Candidate QC 并发布唯一 Winner

正式输出：

```text
story-qc-candidates/index.json
story-plan-winner-selection.json
story-plans/index.json
story-qc/
  index.json
story-qc-review.md
story-qc-validation.json
```

生产 Orchestrator 按 Story 并行执行 Rank 1。某 Story 本轮为 `approved` 时，
更高 rank 以 `earlier_candidate_approved` 记录未执行并立即早停；`review` 或
`blocked` 必须继续下一 rank 寻找 approved。auto-safe fade-only review 只能在所有
Candidate 耗尽后兜底，不得使高 rank 提前停止。每轮使用不可变
`story-qc-candidates/rounds/rank-<NN>.batch.json`；中间 Index 为 schema `1.2`、
method `rank-round-story-plan-candidates-v3`，只有 `status=complete` 才能发布。

已确定性物化但 Plan 状态为 `blocked` 的 Candidate 保留文件与原 rank，
在 Index 中以 `plan_validation_blocked` 记录 Plan SHA、typed
`blocked_reasons` 和 `repair_routes`，不准备代理视频或 QC 任务。
只要每个 Story 仍有 ready Candidate，其他 Candidate 的 Plan 阻断不影响 Arena；
`--allow-partial` 时零 ready Candidate 的 Story 作为
`no_plan_valid_candidate` 拒绝，其他 Story 继续。缺文件、旧哈希、Schema/身份、
确定性重物化或 generation 错误仍是整个 Arena 硬失败。

决选只使用已完成完整 QC 的同 Treatment Candidate：存在 `approved` 时只在
`approved` 中按原 Candidate 质量 rank 选；没有 `approved` 时，仅允许全部非 info
finding 都属于 `local-audio-fade-fallback-source_start/end` 的 `review` 兜底。
`thread_broken`、Coverage、Flow、视觉、环境、source-edge human review 或无类型
review 均不得自动发布。Winner 使用 Candidate Boundary Repair 后的有效 Plan；
Publisher 才能恢复唯一正式 Plan/QC Index。

若没有任何 Winner，Publisher 只发布 blocked 的
`story-plan-winner-selection.json` 并把项目决选阶段记为 blocked；正式
`story-plans/index.json` 必须继续保持 `status=stale`、空 plans，且不得发布正式
Story QC Index。这样修复 QC 代码、Prompt、Schema 或请求签名后仍可从同一批
Candidate 重新准备完整 QC，不能把一次零 Winner 误写成不可恢复的正式 Plan 状态。

Winner 指向的 workspace QC 报告同时保存：

- Coverage QC。
- Flow QC。
- Cut Safety QC。
- 本地音频边界逐切点状态、语音区间、建议时间和调整量。
- 自动修复状态、轮次、Patch 历史、未解决边界与修复元数据哈希。
- 通过边界复核的 Clip。
- 需要人工判断或阻断的 Clip。
- 模型 Findings。
- Boundary Patch 建议。
- Story Plan、Script、Source、Proxy、Batch 和全部视频结果的 SHA-256。
- 适用时的 Junction Edit Index、逐 Story Edit Plan 与效果态 Proxy SHA-256。
- 本地音频 Plan、音频报告、固定引擎、VAD 策略及本地源完整 SHA-256。

每个 `review_asset` 还必须绑定自己的 `context_path` 与 `context_sha256`；
QC Batch 中的 `context_file`、`media_file` 必须与 Proxy Manifest 对应资产逐项一致，
防止任务误看其他 Story、其他边界或陈旧上下文。

## 状态

每个 QC 组和整个 Story 只使用：

- `approved`：QC 独有的 Proxy 结构检查、本地音频门禁与适用的视频复核全部通过。
- `review`：没有硬阻断，但存在主观连接、边界或素材风险需要人工判断。
- `blocked`：缺失 must-have、Payoff、发生剧透、时间范围非法、关键对白/动作
  被截断，或代理/输入已陈旧。

总状态使用最严重结果：

```text
任一组 blocked → Story blocked
否则任一组 review → Story review
否则 → Story approved
```

`approved` Story 直接进入 Render Recipe。手动流程可用
`build_story_render_recipes.py --include-review` 显式接纳任意 `review`；auto
只使用 `--include-auto-safe-review`，并要求完整的非 info finding 集合全部属于
`local-audio-fade-fallback-source_start/end`。对白/因果连续性、视觉/环境、
source-edge human review、无类型 review 或任一 block finding 都不自动渲染。
`blocked` 必须生成新 Story Plan 版本后重新 QC。QC 报告状态 `review` 始终保持
不变，Recipe Index 分别以 `include_review` / `include_auto_safe_review` 审计入口。

## 边界验证

Span Candidate 和 Story Plan 保持不可变。Selected Video QC 不回写：

- `span-candidates/*.json`
- `story-plans/*.json`

本地双路 VAD 与视频复核聚合结果只在 Story QC 报告中保存为：

- `verified_clip_ids`
- `review_clip_ids`
- `blocked_clip_ids`

需要调整边界时，当前流程先生成本地音频 Boundary Patch 和新的派生 Story Plan，
再对受影响范围重新渲染和复核。跨场景、换 Span 的调整不由本阶段自动执行；
超过 12 秒的建议走 `fade_fallback`，不自动扩边。通用自由 J-cut/L-cut 仍不支持；
只有操作员明确给出安全/禁画区间，并由本地编译器闭合为 `reviewed_bridge` 或
`right_av_overlap` 的 `audio_tail_visual_repair`，才可作为受控例外进入效果态复核。

## 隐私

- 精确签名 URL 只从私有 `window-analysis-batch.json` 读取到内存。
- Proxy Manifest、视频上下文、报告和日志不得保存完整签名 URL。
- FFmpeg/FFprobe 对远程源失败时只报告 Source ID，不回显命令或 URL。
