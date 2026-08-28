# VLM WindowContextPack — 外部剧情资产的受控投影设计

**状态：P0 契约、Normalizer、映射集校验、Pack、运行时 `context_prepare`
Command/ArtifactSet 和 VLM v7 请求绑定已实现。已提交 Pack 的跨主机续跑只读
PostgreSQL/Blob，不重新请求外部 API。**
**适用范围：** 当前 `source_prep -> context_prepare -> vlm` 语义路径。
**不适用范围：** ASR/VAD/字幕输入、镜头/高光时间点、Stage 4 物理剪辑与旧
`global_context`。

## 1. 决定

外部 API 的书籍、章节、人物和关系资产可以帮助 VLM 理解剧情，但它们不是视频证据。
因此不允许从 `SourceKnowledgeInputSet` 直接拼接 prompt，也不允许恢复旧架构“全局上下文
注入每个窗口”的做法。

唯一可见给模型的外部信息是版本化、不可变、窗口级的 `WindowContextPack/v1`：

```text
External API response
  -> ExternalContextSnapshot/v1 (immutable raw Blob)
  -> NormalizedNarrativeContext/v1
  -> EpisodeContextBinding/v1
  -> WindowContextPack/v1
  -> VLM prompt v7 and request identity
```

当前 VLM prompt v6、parser v4 和历史请求保持不变。使用 Context Pack 必须注册新的
prompt/parser/request strategy，不能将新字段静默补入旧请求。

## 2. 现有契约与缺口

`SourceKnowledgeInputSet` 仅是 Stage 1 owner 输入容器：它可引用 `script`、
`episode_metadata`、`character_metadata`、`relationship_metadata` 等不可变 Blob，
但没有规定如何选择、截断、反剧透或绑定到一个本地视频窗口。因此它不能充当模型可见
Context Pack。

当前实际 VLM factory 只绑定已提交 SourcePrep 的 `WindowManifest`、proxy Blob、prompt、
schema、模型和策略。它没有外部 API source、episode mapping 或 context hash。这正是
P0 需要新增的明确投影边界。

## 3. 数据与责任边界

### 3.1 四种不可混淆对象

| 对象 | 是否模型可见 | 责任 |
| --- | --- | --- |
| `ExternalContextSnapshot/v1` | 否 | 保存一次 API 原始响应和请求身份，供审计与重建。 |
| `NormalizedNarrativeContext/v1` | 否 | 以稳定 ID、语言和可见性字段规范化原始资产。 |
| `EpisodeContextBindingSet/v1` | 否 | 校验整套显式映射，不允许一个本地或外部集被重复绑定。 |
| `EpisodeContextBinding/v1` | 否 | 唯一地把本地 source episode 映射到外部 episode/chapter。 |
| `WindowContextPack/v1` | 是 | 依据 policy 选择的紧凑剧情辅助文本，且有精确 hash。 |

API 字幕、ASR、VAD、shots、highlight、镜头时间点从上述所有“模型可见”路径排除。它们
可以在未来被离线质量对比工具读取，但不能成为语义 VLM 输入、视频观察或物理选点依据。

### 3.2 必要字段审计

| 字段 | 必要性 | 原因 |
| --- | --- | --- |
| `raw_blob_ref`、`raw_content_hash`、`fetch_identity` | ✅ | 旧 run 必须可使用原始 API 事实重建。 |
| `series_external_id`、`external_episode_id`、`external_chapter_id`、本地 source identity | ✅ | 防止把第 N 个本地文件猜成 API 第 N 集。 |
| `mapping_method`、`mapping_status`、`conflict_reason` | ✅ | 映射错误必须降级，而不是隐式成功。 |
| `known_from_episode`、`known_through_episode` | ✅ | 关系和前情的反剧透边界。 |
| `selection_policy_version`、`policy_hash`、`context_pack_hash` | ✅ | 请求身份和历史重跑。 |
| 原始 API URL、Authorization、cookie | ❌ | 不进入 Artifact、Pack、Receipt 或 debug；由受保护运行配置处理。 |
| API subtitles/shots/highlights | ❌ | 不提供剧情辅助所必需的内容，且会污染 VLM-first 语义边界。 |
| 每个 VLM 字段的来源标签 | ❌ | 输出按对象组划分，避免冗余来源噪声。 |

## 4. EpisodeContextBinding/v1：先证明“是哪一集”

Context prepare 只能接受 owner/API 明确提供的映射。请求配置保存的是非推断的
`OwnerEpisodeMap`；SourcePrep 成功后，系统将其与实际 source ID 和 SHA-256 结合，形成
不可变 `EpisodeContextBinding`，并放入 `EpisodeContextBindingSet`：

```json
{
  "kind": "EpisodeContextBinding/v1",
  "local_relative_path": "episode-012.mp4",
  "local_episode_index": 11,
  "local_source_sha256": "sha256:...",
  "series_external_id": "book-42",
  "external_episode_id": "episode-912",
  "external_chapter_id": "chapter-912",
  "external_episode_ordinal": 12,
  "mapping_method": "owner_explicit",
  "mapping_status": "bound"
}
```

`external_episode_ordinal` 是 API 内当前 series 的明确、正整数剧情顺序；它不是
`local_episode_index`。v1 **禁止**依文件名、列表顺序、标题模糊匹配或“第 12 项”自动绑定。以下任一条件成立，
Context prepare 产出显式 `video_only` Pack：

- 本地 source、episode index 或 source hash 不匹配；
- 外部 episode/chapter 缺失、重复、跨 series，或与另一 local source 冲突；
- API 返回的 `external_episode_ordinal` 与显式 binding 不一致；
- 映射不是 `owner_explicit`；
- API 返回的主体语言/系列信息违反已配置的 source binding。

这不是整个 Job 的失败：语义 VLM 可继续纯视频分析，但 Receipt 必须记录
`context_mode=video_only` 与非敏感 reason code，例如 `EXTERNAL_EPISODE_BINDING_MISSING`。

## 5. 反剧透可见性策略

`ContextSelectionPolicy/v1` 以当前 binding 的 `external_episode_ordinal=k` 作为唯一剧情
可见性上限，绝不使用本地上传排序。

| 资产 | 进入当前集 VLM 的条件 |
| --- | --- |
| 剧名、类型、语言、无剧情的稳定角色基础卡 | 可用。 |
| 当前集标题、当前集摘要、当前章节人物 | `external_episode_ordinal == k`。 |
| 前情摘要 | `external_episode_ordinal < k`，按从近到远截断。 |
| 人物关系 | `known_from_external_episode_ordinal <= k`；缺少该字段即不可用。 |
| 未来集摘要、结局、最终关系、全剧详细剧情 | 永远不可用。 |
| API 字幕、shots、highlight、ASR/VAD | 永远不可用。 |

全剧简介只能使用经 Normalizer 分类为 `stable_premise` 的极短、无未来情节版本；无法
证明无剧透时，不放入 Pack。角色资料中的“隐藏身份”“最终伴侣”“结局状态”等同样属于
future knowledge，必须在 Normalizer 阶段剔除而不是指望 prompt 约束模型忽略。

## 6. WindowContextPack/v1

### 6.1 结构

```json
{
  "kind": "WindowContextPack/v1",
  "mode": "api_assisted",
  "source_binding_hash": "sha256:...",
  "normalized_context_hash": "sha256:...",
  "selection_policy_version": "context-selection-v1",
  "selection_policy_hash": "sha256:...",
  "known_through_external_episode_ordinal": 12,
  "selected_refs": ["ep:912", "ch:ivy", "rel:ivy-ronan"],
  "rendered_context": "<canonical compact text>",
  "context_pack_hash": "sha256:..."
}
```

`mode` 只能是 `api_assisted` 或 `video_only`。后者仍是完整、可 hash 的 Pack；它不含
外部文字，明确说明“没有外部剧情上下文”，从而让同一 prompt strategy 的历史重跑精确
复现。

### 6.2 选择顺序与预算

选择必须确定性、同输入同输出。优先级：

1. L1：当前集标题、当前章节一至两句摘要；
2. L2：当前集涉及的 4–8 张人物短卡及最多 8–16 条已知关系；
3. L3：截至当前集的滚动前情摘要，从最近集向前；
4. L4：主题/类型标签，预算不足最先丢弃。

`ContextSelectionPolicy/v1` 需要冻结：每类最大条数、每项最大字符数、排序 tie-break、
截断标记、语言回退和 `unicode-token-estimator-v1`。以该估算器强制 Pack 文本不超过
2,000 tokens；同时以 8 KiB UTF-8 作为绝对字节上限。任何一层超限时整项丢弃，不能截断
ID、名字或关系谓词。选择结果必须列出 `selected_refs` 和 `suppressed_reason_counts`，但
只有 `rendered_context` 进入模型。

角色短卡目标约 35 tokens：规范名称、别名、剧情定位、可稳定确认的极短视觉特征。这里的
名字是 Context 事实，不能改写 VLM 视频观察的 `display_label`。

## 7. VLM 输入、输出与身份

### 7.1 输入规则

新 prompt v7 的输入由三部分组成：冻结指令、完整视频、`WindowContextPack.rendered_context`。
它仍不输入 ASR、VAD、字幕、帧表、PTS、镜头或剪辑候选。prompt 必须明确：Context 是
叙事辅助，视频观察只能由视频支撑；冲突时保留不确定性，不得用 Context 覆盖视频。

`GenerateVlmEvidenceRequest` 的新 v7 变体必须额外绑定：

```text
context_pack_blob_ref
context_pack_sha256
context_selection_policy_hash
episode_context_binding_hash
```

这些字段进入 command request hash、VLM request identity 和 reuse identity。旧 v6 请求
没有它们且仍按原字节重跑；任何 API 更新只会形成新 Snapshot/Pack/new run，绝不重解释
旧 Artifact。

### 7.2 输出规则（P1）

当前 parser v4 的扁平 `entities/facts/events` 不可悄悄改写。使用 Context Pack 的丰富
输出必须注册新 parser/schema，例如：

```json
{
  "video_observations": {
    "entities": [],
    "scene_cards": [],
    "dialogue_candidates": [],
    "visual_events": [],
    "story_beats": [],
    "shot_language": []
  },
  "context_assisted_interpretations": {
    "identity_resolutions": [],
    "relationship_interpretations": [],
    "narrative_interpretations": []
  },
  "candidate_hypotheses": []
}
```

`video_observations` 默认不带逐字段来源标记；它们只可由视频支撑。
`context_assisted_interpretations` 才引用短 `context_ref`，例如画面实体 `p003` 对应角色
`ch01` 的 `likely_match`。API 人名不得直接变成视频实体的 `display_label`，API 关系也
不得单独生成事实、事件、候选或 Stage 4 物理端点。

## 8. 失败、重跑与 API 更新

```text
API unavailable / invalid raw response / binding conflict
  -> persist diagnostic + VideoOnly WindowContextPack
  -> VLM continues with no external text

API response accepted
  -> raw immutable snapshot -> normalized context -> bound Pack
  -> VLM request binds exact Pack hash

historical replay
  -> read original Pack Blob, never refetch API
```

### 8.1 跨机器续跑的配置边界

第一次创建 API-assisted Pack 时，运行环境需要私有 API endpoint、credential 和显式
episode map；它们只用于取得一次 Snapshot。成功提交后，后续同一 `run_id` 的
VLM 重跑、进程重启或换主机续跑只按 `Job + scope + Context Prepare producer + receipt`
读取已提交的 `window_context_pack_set`。它**不得**再次读取 endpoint、credential 或
owner map，也不得重新抓取 API。

因此跨平台的前提是目标机器能访问同一 PostgreSQL 与 Blob 存储；若 VLM 调用尚未完成，
还需能读取已提交 proxy Blob 并拥有 Ark 凭据。只有创建新 run 或显式刷新 context
version 才需要重新提供 API 配置；密钥永不持久化。

同一 run 内禁止“半新半旧”：不能用旧人物表配新章节摘要，也不能在 fetch 失败时从未绑定的
缓存拼接上下文。Snapshot 的所有 API 响应必须属于一次 `context_snapshot_id`；任一必需
响应不可用时整体降级 `video_only`。

外部 HTTP 失败不应变成无限重试：可按 Context prepare 的有限 retry policy 重试获取，
耗尽后写出 video-only Pack。VLM 的支付/生成 retry policy 与 Context fetch retry policy
必须独立。

## 9. 实施顺序

1. 定义 Pydantic/JSON Schema：Snapshot、Normalized Context、Binding、Pack、Selection
   Policy；加入纯 `video_only` fixtures。
2. 实现无 HTTP 的 Normalizer 与 deterministic selector；先用 owner 提供的 raw fixture
   验证映射、剧透、预算和 hash。
3. 新增 `context_prepare` stage/command，在 SourcePrep 后、VLM 前产出一个 PackSet；VLM
   不自行请求 API。
4. 注册 prompt v7/parser v5/request identity v2；完成 pack 绑定后再修改 VLM factory。
5. 最后实现具体外部 API adapter。adapter 只负责 fetch + raw snapshot，不能写 VLM prompt
   或 Artifact。

当前外部 API 的认证、端点与响应 schema 尚未冻结，因此第 5 步不能凭旧代码猜测实现。
旧实现只可用作字段发现与离线对比参考，不可作为 runtime 依赖。

## 10. 对抗性复审

| 风险问题 | 结论与约束 |
| --- | --- |
| API 章节排序与上传文件顺序不同 | v1 只接受 owner explicit binding；否则 video-only。 |
| 人物关系泄露后续反转 | 无 `known_from_external_episode_ordinal` 的关系禁止进入 Pack。 |
| 本地文件排序与 API 季/章节重编号不一致 | 可见性只用 binding 的 external ordinal；映射不完整即 video-only。 |
| API 更新使旧 VLM 输出不可复现 | 请求绑定不可变 Pack Blob/hash；replay 不 fetch。 |
| API 失败混入旧缓存 | snapshot 原子化；整组失败直接 video-only。 |
| Context 文字被当作画面证据 | 新 schema 分离 video observations 与 assisted interpretations；冲突不覆盖视频。 |
| Context Pack 太大 | 固定 L1–L4、每项上限、2K estimator/8KiB hard cap、确定性 drop。 |
| Context 帮助被用于物理切点 | parser/admission 明确禁止其流入 Stage 4 endpoint source。 |
| 直接复用旧 global_context | 禁止；旧全局注入、best-effort 写库和 API 高光都不进入新路径。 |

**复审结论：** 可以实施 1–3 的纯契约与 deterministic projection；在具体 API endpoint/
认证/响应 schema 被提供并冻结前，不实施第 5 步。这样不阻塞 `video_only` Pipeline，也不以
猜测的 API 格式污染生产语义。

## 11. 关联实现所有权

- 新包建议：`packages/autocut-kernel/src/autocut_kernel/context_pack/`（纯契约和
  selection）；
- Pipeline port/stage：`auto_cut_bot/pipeline/context_prepare/`；
- VLM 接线：`auto_cut_bot/pipeline/vlm/request_factory.py`、
  `auto_cut_bot/pipeline/runtime/vlm_stage.py`；
- 新 prompt/parser 仅在 P1 注册，不修改 v6/v4；
- 不修改：`auto_cut_bot/pipeline/media_preflight/`、Stage 4、旧
  `packages/autocut-core/`。
