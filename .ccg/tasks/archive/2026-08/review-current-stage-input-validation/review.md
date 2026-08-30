# 当前各阶段输入与格式校验审查

审查基线：`feat/v213-contract-codegen`，本地 HEAD `984e75e1`，并单独识别工作区中的 VLM V4/Stage 1 与 Candidate V2 候选改动。本文不把未提交候选实现视为已交付能力。

## 总结论

当前设计不是“校验整体过头”，而是存在明显的错位：

- Source、持久化、引用闭合、时间基与物理端点校验总体必要且合理；
- VLM 和 Stage 1–3 的 JSON closed schema、资源上限和引用 allowlist 值得保留；
- 部分无业务语义的输出顺序被当成失败条件，属于过严；
- 审计 envelope 与模型可见 projection 没有彻底分离，Stage 2/3 输入明显过重；
- 生产 authority 已使用 V23 prompt + V4 pack，但 Stage 2/3 仍存在 V3-only 路径；
- HTTP 流水线实际只闭合至 Stage 3 或 Media Preflight，Stage 4 ExactSpan、生产 Recipe、Render、Publication QC 尚未接线。

因此：当前 Source → Context → VLM → Stage 1 的方向可继续收敛；当前整条“真实自动成片流水线”仍是 No-Go。

## 判定原则

一个字段是否必要，必须区分三个边界：

1. `durable envelope`：为幂等、CAS、重放、审计保留完整 hash、Receipt、Artifact ref 与 policy identity；
2. `model projection`：只放模型完成本阶段推理所需的语义内容、短 alias 和业务约束；
3. `local admission/compiler`：独立复算引用闭合、来源授权、时间合法性和物理可行性，不能要求模型自证。

审计字段通常应保留在 1，但不应因为“持久化需要”而进入 2。

## 逐阶段审查

| 阶段 | 当前主要输入 | 必须保留 | 应删除、移出模型或调整 | 格式校验判断 |
|---|---|---|---|---|
| HTTP Run | `profile`；`source_root` 或 `source_reference` 二选一；Idempotency-Key | profile、稳定 source identity、幂等键、closed body | 公共 HTTP 的 `source_root` 暴露宿主路径且不利于跨平台重放，优先只公开 `source_reference`；目前无 episode/stage 选择 | unknown field、二选一和幂等冲突校验合理；仅允许 test/shadow，尚无正式 production HTTP 意图 |
| Source Prep | 授权根、Series policy、期望集数、文件快照、ffprobe/时间映射 | source/series identity、内容 hash、来源授权、精确 time base、Frame PTS、ProxyTimelineMap | 单个命令对全剧 `all_or_nothing` 不利于单集重跑；`read_bytes()` 整体读视频不必要，应流式 hash/ingest | 路径边界、用途、集数、hash、manifest 校验合理；建议拆“Series manifest finalizer + episode child” |
| Context Prepare | committed Source、不可变 API Snapshot、Normalizer、显式 episode map、selection policy | episode/source mapping、反剧透可见范围、选取 policy、pack hash、video-only 原因 | raw API 只存审计；仅 `rendered_context` 进入 VLM。selected refs/hash 不必写入 prompt | 最大角色/关系/token/UTF-8 预算合理；reader 缺逐 episode `source_binding_hash` 重放校验；API 原始响应缺 byte/depth 上限 |
| VLM | proxy video/file_id、窗口 duration、可选 rendered context、V23 prompt、V4 strict schema | 视频、窗口与 source/proxy binding、模型/prompt/schema/parser policy、核心实体/事实/事件 | candidate editorial 推断不应与 factual core 原子提交；固定为空的 continuity/因果字段不应长期要求回显 | strict JSON Schema、closed fields、引用闭合、区间范围、大小/深度限制合理；enum 集合顺序不应整包拒绝；model uncertainty 需要本地 policy floor |
| Stage 1 Narrative | committed VLM observations、窗口摘要、实体/事实/事件、Stage 1 policy | 可核验的 facts/events、粗粒度时间顺序、冲突/不确定性、source grant（本地） | 完整 SHA256 object IDs、重复 `allowed_refs`、`input_binding_sha256` 回显不应进入模型；改用短 alias，本地映射回 hash | response closed schema、allowlist、覆盖与依赖复算合理；当前 prompt 缺明确粗粒度事件顺序/区间 |
| Stage 2 Story Design | admitted Stage 1、CandidateCatalog、Job/Story policy、source constraints | Digest/Card/Graph、候选证据与测量、时长/数量/来源约束 | 完整 `stage1_members`、Artifact hash、source grant 全量对象、binding 回显移到 envelope | 最大 P0 是运行路径仍用 V3 candidate projection，不能消费当前 V4；Candidate V2 新文件尚未接入 |
| Stage 3 Blueprint | selected portfolio、义务/fact/candidate、顺序/时长/物理要求 | 选中 story、material obligations、可替代事实集合、候选支持、编辑约束 | 当前把 Source、所有 VLM request record、全部 VLM packs、Stage 1/2 全成员和未选 proposal 作为 prompt；应拆 audit context 与 compact model context | 目标 story/beat 顺序和引用闭合合理；当前 reader 仍 V3-only；context byte bound 只能限制成本，不能使冗余合理 |
| Media Preflight | SourcePrep、VLM coarse semantic interval、Frame PTS、Audio sample、ASR/VAD、detector/calibration identity | source/timebase、局部自适应 ASR/VAD coverage、frame/audio endpoints、capability/calibration receipt | ASR/VAD 不进入 VLM prompt是正确的；VLM 区间只能是局部探测 seed，不能是物理端点 | 校验方向合理；必须给 VLM 不确定度加本地下限并允许扩窗；目前在部分 plan 中位于 Stage 3 后且无 Stage 4 消费者 |
| Stage 4 ExactSpan | coarse desired/anchor range、RootMediaEvidence、A/V clock map、Boundary policy | 独立 frame/audio endpoints、subtitle/visual/dialogue hard constraints、canonical selection | 不应将这些物理字段交给 LLM | 校验总体合理；`unknown` fail-closed 会提高 quarantine，需 detector 校准而非放宽为成功；当前未接运行时 |
| Recipe/Render/QC | ExactSpan、Source Blob、render policy、结构/媒体/发布策略 | source/span binding、静态 recipe validation、确定性 render、结构/编辑/媒体/发布 QC | 当前 `Recipe` 仍是 `fixture_ground_truth_v1` 单 span MVP，不是生产契约 | 本地 fixture 校验可用，但尚无生产 Recipe compiler、HTTP stage 或 Publication QC gate |

## P0：阻止真实流水线的事项

### 1. Stage 2/3 与 VLM V4 不兼容

生产 authority 使用 `strict-semantic-pack-v4` 和 `vlm-semantic-pack-v23-context-assisted-candidate-core`，但已运行的 Stage 2 仍调用旧 `project_candidate_catalog`，Stage 3 仍使用 V3 decoder。当前工作区 V4 reader 只闭合到 Stage 1；Candidate V2 文件尚未接到 runtime、Store lifecycle 和第二次 generation invocation。

修复标准：相同 V4 committed aggregate 必须可被 Stage 1、Candidate Enrichment、Story Proposal、Stage 3 versioned reader 逐层消费；禁止 V4 转写成 V3。

### 2. factual core 与 editorial candidate 的失败域错误

当前 V23 在同一 V4 response 中生成 facts/events/candidate_hypotheses。任一 candidate 的枚举、measurement、support 或引用错误，会使整个窗口的有效事实与事件一起作废。

修复标准：

- VLM 只提交 core observations；
- Stage 2 单独执行 Candidate Enrichment；
- candidate 失败只重试/拒绝 candidate invocation，不污染 VLM factual pack；
- Story Proposal 只消费已提交 CandidateCatalog V2。

### 3. Context Pack 有序绑定未完全复算

首次生成时按 episode 正确构建 pack，但 committed reader 主要验证数量、provenance 和集合 hash；VLM 按数组下标取 pack。相同数量的 packs 若被交换，缺少逐 episode source binding 复算会造成错误剧情辅助进入另一集。

修复标准：reader 对每个 `(episode_index, source_id, source_sha256, window/episode binding)` 复算并比较 `source_binding_hash`，不依赖数组位置本身。

### 4. Stage 4 到最终 QC 没有真实流水线接线

当前数据库 run plan 最远为 `stage3_blueprint`，或额外执行 `media_preflight`；没有 `exact_span_compile → recipe_compile → render → publication_qc → local_output_commit` 命令链。现有 renderer 是 fixture MVP。

修复标准：先产出本地文件也必须有独立 `publish_decision` 的本地等价准入（例如 `output_decision=allow|deny`），只有 allow 才能进入正式输出目录。

## P1：应尽快调整的输入与校验

1. **Stage 3 输入减肥**：审计 context 与模型 context 分开。模型只收 selected story、obligations、facts/events/candidates、时长和编辑约束；request/receipt/blob/member hash 留在 envelope。
2. **Stage 1/2 使用短 alias**：模型侧使用 `w001/e001/f001/c001`，本地 alias table 映射到全 SHA256；不要求模型复制 64 位 hash。
3. **取消 binding hash 回显**：模型不负责证明读到正确请求；provider invocation/response identity 在本地 envelope 绑定即可。
4. **canonical enum 本地化**：重复、未知枚举继续拒绝；合法集合仅顺序不同则本地按 registry 顺序规范化，不能报 `NONCANONICAL_ENUM_SET`。
5. **不确定度下限**：`effective_uncertainty = max(model_uncertainty, sampling/provider_policy_floor)`，并记录二者；禁止信任模型自报 0ms 精度。
6. **Context API 资源防护**：在 `response.content`/JSON parse 前限制 Content-Length/stream bytes，并在 parse 前后限制 JSON depth、数组数量和文本总量。
7. **按阶段最小 identity**：VLM child 不应绑定整个 execution profile；只绑定 VLM policy + exact Source/Context predecessors。否则仅改 Stage 2/3 policy 也会迫使全量 VLM 重跑。
8. **Source Prep 单集恢复**：保留全剧 manifest 原子完成语义，但把每集 probe/hash/window 作为独立 child；失败集可重试，成功集可复用。

## 应保留的强校验

以下不是过度设计，不能为了“跑起来”而删除：

- exact source/scope/owner/hash/revision closure；
- committed ArtifactSet/Receipt 与 request hash 一致；
- JSON duplicate key、unknown field、byte/depth/count 限制；
- 未知 enum、重复引用、悬空引用、跨 owner 引用拒绝；
- VLM interval 必须在当前播放窗口内，且只能作为 coarse semantic evidence；
- Frame PTS、Audio sample、time base 与 A/V pairing 独立复算；
- 缺 Source、空 Recipe、缺必选 Beat、失败 QC 一律不能形成正式输出。

## 重跑能力审查

当前公开接口只支持 `VlmFullStageRecomputeRequest`，明确不支持 episode selection。虽然同一 execution profile 内成功的 VLM child 可恢复，但不能诚实地宣称已经支持“单集、单阶段重跑”。此外 stage keys 过度绑定全局 execution profile，会把无关下游 policy 改动传播到上游 VLM。

目标应为：

```text
Run
  ├── stage invocation
  │     ├── episode child invocation
  │     └── aggregate/finalizer
  └── downstream invalidation graph
```

只重跑变化或失败的 child；aggregate 由完整 child set 重新提交；下游根据精确 predecessor hash 失效，上游不反向失效。

## 验证记录

定向执行 Source/Context/VLM/Stage 1–3/ExactSpan/Recipe/Render/QC 测试：`234 passed`。独立语义输入审查又执行了 lint、类型检查及定向测试（`20 passed, 13 skipped`，跳过项为缺少 PostgreSQL 环境）。

这些结果证明已有 validator 内部一致，不证明 Stage 2/3 V4 兼容、Stage 4 已接线或整条生产流水线已闭合。
