# 01 VLM：视频窗口语义

## 输入前置条件

当前生产方向是 Doubao Ark + V23 contextual candidate-core + V4 parser。一次调用严格绑定
一集已提交的 `PreparedSourceEpisode`：

| 输入 | 含义 | 模型是否直接看到 |
|---|---|---:|
| proxy video `BlobRef` | 压缩后视频字节、MIME、hash、长度 | 看到视频内容，不看到 Blob UUID |
| `WindowManifest` | source/proxy 时钟、窗口范围、采样/PTS 身份 | 只看到允许的窗口时长描述 |
| `WindowContextPack` | 经 Snapshot、Normalizer、显式集映射和防剧透选择后的剧情辅助 | 看到 `rendered_context` |
| `prompt_version`/模板 | 冻结任务定义与字段规则 | 看到模板文本 |
| `response_schema` | V4 closed Schema | provider 约束，不作为自然语言重复输入 |
| `video_fps` | Ark 对上传视频的采样率 | provider 参数 |
| `max_output_tokens` | 输出上限 | provider 参数 |
| `temperature` | 采样温度 | provider 参数 |
| `thinking_type` | `enabled/disabled/auto` | provider 参数 |
| parse/retry/parser contract hash | 本地解析和恢复边界 | 否 |

`WindowContextPack` 只辅助理解身份、关系和当前集剧情。API 字幕、ASR、VAD、镜头、高光、
未来集剧透、Authorization 和 API key 不进入 prompt。上下文缺失时使用显式 `video_only`，
不能混合半份新数据和半份旧缓存。

## 模型被要求完成什么

V23 prompt 要求模型仅根据当前视频输出：

- 局部实体、可见事实和事件；
- 简短窗口摘要；
- 固定形状的 continuity；
- 仅在证据足够时输出 hook/highlight 候选；
- 所有时间用相对播放窗口的整数毫秒半开区间；
- 本地短 ID：`p001`、`f001`、`e001`、`c001`，引用必须闭合；
- 不输出源时间、帧级切点、ASR/VAD 边界、渲染参数或发布决定。

## 模型响应字段

根对象字段固定为：

| 字段 | 含义 | 下游作用 |
|---|---|---|
| `schema_version` | 固定为 `4` | 选择 V4 parser，禁止混读历史 V3 |
| `entities[]` | 当前窗口可见的人、物、地点或屏幕文本源 | Stage 1 角色/对象节点基础 |
| `facts[]` | 当前视频直接支持的可见事实 | Stage 1 覆盖义务与事件事实依据 |
| `events[]` | 基于事实组织的动作、互动、变化、反应、揭示或转场 | Stage 1 Beat/EventCard；Stage 2 候选闭合 |
| `window_summary` | 当前窗口摘要及 fact/event refs | Stage 1 跨窗口理解 |
| `continuity` | 窗口首尾是否承接未完事件、时间段信息 | Stage 1 连续性分析；当前 V23 固定保守值 |
| `candidate_hypotheses[]` | 可选 hook/highlight 语义候选 | Stage 2 `CandidateCatalog` 输入，不是物理剪辑点 |

### `entities[]`

`local_entity_id` 是模型短 ID；`entity_kind` 是封闭类型；`display_label` 和
`visual_description` 描述画面实体；`support` 给出视频观察区间与置信度。全局 `entity_id`
由 Kernel 根据 request identity 派生，模型无权返回。

### `facts[]`

`local_fact_id`、`fact_kind`、`subject_ref`、可空 `object_ref`、`summary`、`support`。
`fact_kind` 只能是 `visible_presence/visible_state/visible_action/visible_change/`
`visible_relation/scene_context/character_appearance/screen_text/temporal_mode`。

### `events[]`

`local_event_id`、`event_kind`、`summary`、`participant_refs`、`fact_refs`、
`cause_event_refs`、`effect_event_refs`、`open_question`、`temporal_mode`、`support`。
当前 V23 要求每个事件恰好锚定一个 fact，且 support 与该 fact 完全一致；因果数组当前为空。

### `candidate_hypotheses[]`

包含候选类型、锚点/支撑/背景/回报事件、选择理由、对白语义摘记、编辑模式、叙事功能、
标签、语义评分和 support。`hook` 必须有未解问题且无 payoff；`highlight` 必须有已经发生的
payoff。`measurements[]` 的 fact/event refs 必须属于该候选的语义闭包。

## Kernel 在响应后补什么

Kernel 派生全局 ID、request/raw-response/manifest hash，把相对毫秒保守映射到 coarse source
区间，并生成 `vlm_request_record`、`vlm_response_record`、`vlm_semantic_pack` 及批次
ArtifactSet/Receipt。VLM 时间只能定位语义区间，Stage 4 必须结合 ASR/VAD/帧/采样证据重新
选择物理端点。

## 主要拒绝条件

- 非严格 JSON、缺字段、额外字段、重复 key、响应过大；
- 本地 ID 不规范、引用未知/跨 owner、enum 非法；
- event 与 fact support 不相交或不一致；
- cause/effect 不互逆、有环或自环；
- candidate 的事件/fact/measurement 闭包不成立；
- prompt、Schema、parser、context pack 或 source identity hash 不一致。

响应解析失败不会被伪装成成功。当前策略可对这类结构/引用拒绝做最多 3 次有界重生成；每次
仍使用同一不可变视频和 Context Pack，但拥有新的 Attempt 身份。耗尽后错误落到
`RETRY_BUDGET_EXHAUSTED`，需要修复输入契约或选择性重跑。
