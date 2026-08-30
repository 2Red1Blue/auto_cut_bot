# 当前各阶段输入契约与格式校验审查

## 结论

当前设计不是“校验太多”这一种问题，而是两类相反问题同时存在：

1. Source、Context、Media Preflight、Exact Span 的确定性输入总体合理；其哈希、时钟、证据闭合字段虽多，但用于授权、重放和物理安全，不能删除。
2. VLM、Stage 1、Stage 2、Stage 3 的模型可见输入混入了大量审计字段、完整哈希、完整 Artifact 和可由 Kernel 派生的常量，导致 token 浪费、模型抄写错误和不必要的重试。
3. 当前 `semantic_story` 不是可运行闭环：权威配置固定 VLM V4，而 Stage 1–3 committed reader 明确拒绝 V4。测试把这种拒绝当作预期，因此测试通过不能证明真实链路可运行。
4. Media Preflight 和 Stage 4 内核已有较强输入契约，但未接入当前 `semantic_story` HTTP DAG；Render/QC 仍是 fixture-only、单源、单段、纯视频 MVP，production profile 被明确拒绝。

因此当前判断为：**单独 VLM 可以运行；当前完整语义链 No-Go；正式 Recipe → Render → Publication QC No-Go。**

## 实际 HTTP DAG

当前 `semantic_story` 创建以下命令：

```text
source_prep
→ context_prepare
→ vlm
→ stage1_narrative
→ stage2_portfolio
→ stage3_blueprint
```

其安装权威同时声明 `timed_speech=false`、`physical_edit=false`、`render_qc=false`、`external_publication=false`。Media Preflight 只存在于另一条 execution profile，不是当前 semantic story HTTP 闭环的一部分。

## 逐阶段审查

### 0. HTTP Run Request

输入：`profile`，以及 `source_root` / `source_reference` 二选一。

必要性：

- `profile`：essential，但当前只允许 test/shadow，不能表达正式运行。
- `source_reference`：essential，适合作为稳定的跨机器源引用。
- `source_root`：conditional，仅适合本机运维；公共 API 可最终收敛为 source reference。

校验判断：closed object、二选一、canonical hash 都合理。当前重跑 API 只有 `vlm/full_stage`，没有 episode/window 选择，因此任何小修改都可能导致整批 VLM 重跑，属于 P1 能力缺口。

### 1. Source Prep

核心输入：受权根、精确相对路径、每个源的内容 hash/byte size、source count、whole-series completion policy。

必要性：

- 路径、内容 hash、大小、source count、源顺序：essential，用于跨机器 materialization、幂等和全剧原子性。
- `all_or_nothing`：essential，符合整批原子可见要求。
- 宿主绝对路径：removable from persisted/public contract；只应留在私有 materialization 层。

校验判断：总体正确。不要为“简单”删除内容身份或 source census。

### 2. Context Prepare

实际链路：外部 API raw snapshot → normalizer → explicit OwnerEpisodeMap → WindowContextPack。

必要性：

- immutable raw snapshot hash、series/episode binding、selection policy/hash、known-through episode、rendered context：essential。
- `selected_refs`、suppression counts：essential for audit，removable from model prompt。
- endpoint origin、credential scope：essential for provenance，但禁止把密钥或 Authorization 进入 Artifact/Prompt。
- API subtitle/shot/highlight/full spoiler synopsis：removable from VLM prompt；可保留为离线对照或后续专用资产。

校验判断：显式集映射、反剧透截断、8KB Context Pack 上限合理。问题是所有 API/normalizer 失败都固化为成功的 `video_only`：

- metadata 是 optional 时合理；
- 某 run 明确要求 API-assisted 时不合理，瞬时失败会被永久降级。

应给 run/profile 增加 `context_requirement=optional|required`，并支持只重跑 Context/VLM 受影响窗口。

### 3. VLM

当前模型输入包括：完整视频窗口、窗口 duration、可选的 spoiler-safe Context Pack、长格式约束、JSON Schema。

当前模型输出包括：entities、facts、events、window summary、continuity，以及完整 candidate hypotheses（anchor/support/context/payoff、tags、narrative functions、editing modes、measurements 等）。

必要性：

- entities/facts/events/support/confidence/window summary：essential。
- continuity：conditional；只有能从重叠窗口或后处理证明时才应输出，不能要求单窗口模型自证跨窗口连续性。
- candidate hypotheses：removable from VLM vNext。Stage 2 当前真正使用的是粗粒度事件/事实/来源/区间/置信度，不需要 VLM 同时完成候选评分、标签、剪辑模式和 measurements。
- frame allowlist / proxy support：若 VLM 用抽帧证据，则 evidence identity essential；但物理切点仍必须由 FramePtsIndex/ASR/VAD/visual evidence 生成，不能把 VLM support 当最终端点。

格式校验：

- 应保留：strict UTF-8 JSON、duplicate key 拒绝、closed object、byte/depth/count 上限、ID 唯一、引用闭合、support 范围、event/fact 时间相交、candidate semantic closure。
- 应改变：set-like 枚举顺序不应让模型负责；在新 parser/version 内稳定排序和去重。不能原地改变已冻结 V4。
- `measurements` 非空、候选 support 与多个事件重叠等约束本身可验证，但放在 VLM 阶段没有足够下游价值，造成高失败率。

真实 PC 记录显示，每个尝试 input 约 33,985 tokens；output 分别约 16,141 / 5,838 / 18,298 tokens。模型提示正文只有约 2.4K 字符，主要成本是视频 token 和过于丰富的响应，不是 Context Pack 文本。

已出现的真实拒绝包括：非 canonical tags、未知 fact ref、measurement closure 失败、candidate support 与事件不相交。它们证明现有 VLM 候选结构的收益不足以覆盖失败和成本。

### 4. Stage 1 Narrative

当前输入：所有窗口的 summary/continuity/entities/facts/events，另附一份完整 `allowed_refs`；每个引用包含完整 window SHA 与对象 ID。

当前输出：`input_binding_sha256`、beats、obligations、story threads、merge proposals。

必要性：

- 核心观察图与窗口顺序：essential。
- allowed reference universe：essential for Kernel，removable in full-hash form from model。应生成短生命周期 alias，如 `w03/e12`，Kernel 保存 alias→full ref map。
- `input_binding_sha256`：essential for request/receipt，removable from model output；Kernel/adapter应附加。
- beat/obligation/thread 的语义选择：essential model output。
- Artifact ID、owner hash、policy identity：Kernel 派生，不应由模型填写。

格式校验问题：

- byte cap 16MB、total text 1MB 只是资源上限，不是可用 token budget；全剧会越过模型上下文。
- response schema 可接受空 beats/obligations/threads，但 strict-global Admission 后续才拒绝，浪费付费尝试。
- 缺少明确的 beat→obligation required-fact closure。

P0 阻断：当前 reader 明确拒绝 V4，而安装 authority 固定 V4。

### 5. Stage 2 Portfolio

当前模型输入：Stage 1 全部 member refs、source grant、CandidateCatalog 全量 payload、三份 policy、EpisodeDigest/EventCard/NarrativeGraph 全量 payload。

必要性：

- NarrativeGraph、EpisodeDigest、简化的 material availability、allowed genre/profile/teaser/duration：essential。
- thread/key characters/source allow-deny/min usable duration：conditional。
- 完整 stage1 member refs/hash、source grant internals、candidate policy internals、authorization purpose、input binding echo：removable from model，保留在 audit envelope。

当前模型输出中真正应由模型决定的是：title、claim、obligation/thread/character choice、genre/profile/duration/teaser/hook。以下应由 Kernel 派生：proposal/requirement IDs、required facts、per-obligation requirements、physical constants、完整 SemanticObjectRefs、source authorization、input binding。

CandidateCatalog v1 依赖 VLM 已给出的 rich candidate，和“VLM 只产核心观察”方向冲突。建议新增版本化的 `candidate-catalog-v2` / `beat-material-catalog-v1`，由已准入 Stage 1 beat/obligation/event/fact 确定性投影生成，而不是新增一个独立 Candidate LLM 阶段。

response schema 的 proposal/material requirement 静态最小数量应与 JobPolicy 一致，避免先接受空数组、后由 Admission 拒绝。

### 6. Stage 3 Editorial Blueprint

当前模型输入是最严重的上下文问题：完整 Source manifest、每个 VLM request/pack、全部 Stage 1/2 的 13 个成员和策略；只是去重一次，并仍整体进入 prompt。

必要性应拆成两个对象：

- `AuditContextManifest`：所有完整 member refs、hashes、request/attempt/provenance，essential for replay/audit，但禁止进入模型 prompt。
- `EditorialPromptContext`：仅包含选中 proposal、义务/事实闭包、相关事件/material seeds、允许策略和短 alias，essential for model。

当前 16MB batch / 8MB story byte cap 同样不是可用 token budget。Stage 3 应按 story/beat closure 分区，不应把整剧所有 VLM 原始 request/pack 提交给模型。

模型应决定叙事职责、beat 顺序、备选素材语义与时长意图；完整引用、hash、排序 constraint ID、binding 和物理常量由 Kernel 扩展。

### 7. Media Preflight / ASR / VAD

输入很多但大多必要：source identity、FramePtsIndex、AudioSampleBoundarySet、detector/model/policy/calibration identity、audio/video clock、adaptive local speech window policy、materialization limits、runtime capability projection。

分类：

- source/hash/clock/time-base/PTS index/audio samples/model identity/timing error bound/calibration record：essential。
- tool stdout/stderr/argv hashes：essential for audit，removable from downstream model prompt。
- local absolute source path/service URL/timeouts/resource caps：conditional private runtime inputs，不应成为跨机器业务 Artifact。
- CPU/MPS profile 与 PC CUDA capability：conditional，必须按 runtime capability 投影，不能让 CPU calibration 冒充 CUDA authority。

校验判断：exact types、nonzero identity、source/clock closure、accepted capability reread、CAS/idempotency 都合理。这里的严格校验不是主要 token 或易用性问题，因为它不是 LLM prompt。

需改进：`LocalMediaPreflightPolicy` 把 speech service/runtime 字段和纯物理 detector 字段放在同一大对象中，建议拆成：

1. `PhysicalEvidencePolicy`
2. `TimedSpeechPolicy`
3. `RuntimeCapabilityBinding`

这样只有真正改变时间语义的字段进入 calibration compatibility hash，URL、超时、stdout cap 等运维字段只记录或触发重试，不应迫使全量重新校准。

### 8. Stage 4 Exact Span

核心输入：desired coarse range、anchor range、minimum duration、dialogue requirement、root evidence、candidate-local ASR/VAD evidence、presentation map、exact span policy。

必要性：全部 essential，但职责必须清晰：

- desired/anchor 是编辑意图和粗范围，不是最终端点。
- video/audio 分别使用原生整数 tick 与明确 time base。
- FramePtsIndex、AudioSampleBoundary、visual/subtitle/shot coverage、presentation map、dialogue guard 是硬约束。
- max pair visits 是资源安全边界，不能在耗尽时返回 partial optimum。

当前 exact typed、source/clock closure、complete evidence coverage、full relation hash/count、canonical decision key 的校验合理。不要为了启动方便放宽为 float seconds 或单一 `final source tick`。

需要补充到正式 Recipe 的是独立 video in/out 与 audio in/out、BoundaryProof、DialogueIntegrityProof、policy/evidence binding；当前 renderer Recipe 尚未消费这些生产字段。

### 9. Recipe / Render / QC

当前 Recipe 只有：source hash/size、video timebase、start/end PTS、fixture identity/mode/hash。Renderer 固定为单源、单段、H.264 MP4、video-only；production profile 直接拒绝 fixture recipe。

这套输入对 fixture MVP 是合理的，但对正式成片严重不足：

- 缺 audio range 和 A/V pairing proof；
- 缺多 span、junction、ordering、transition；
- 缺 Blueprint/Portfolio/Admission binding；
- 缺字幕策略、音频 topology、平台输出策略；
- 缺发布级 QC 与 batch publication transaction identity。

当前 QC 只做文件 identity、H.264/video-only topology、full decode、frame hash sample、粗黑帧/冻帧。它不能证明对白未截断、叙事完整、字幕安全、响度/音画同步、平台合规、整批外部原子可见。

应保留确定性派生 QC，禁止 caller 自填 pass；但必须新增生产 Recipe v2 和 PublicationQC/PublishDecision，不能在现有 fixture Recipe 上继续加补丁。

## 跨阶段格式校验原则

### 必须保留

- strict JSON、closed object、duplicate key 拒绝；
- byte/depth/count/text resource bounds；
- canonical immutable hash、Artifact/Receipt/Command exact binding；
- local/global ID 唯一与引用闭合；
- source/clock/time-base/coverage closure；
- independent Admission recomputation；
- semantic failure 与 infrastructure failure 分类，fail-closed。

### 应从模型移到 Kernel

- content-addressed IDs；
- full Artifact/SemanticObjectRef；
- input binding/hash 回显；
- canonical set ordering；
- required fact expansion；
- policy constants、physical requirement constants、authorization purpose；
- candidate material support 的确定性投影；
- audit provenance、request/attempt/provider identities。

### 应保留给模型

- 视频可支持的语义观察；
- 叙事归并与不确定性；
- story claim、选择与编排；
- editorial beat 的职责、顺序和备选语义。

## 优先修复顺序

### P0

1. 实现 V4 committed semantic adapter/reader，删除“V4 unsupported”预期；增加真正从 VLM batch 到 Stage 1 的数据库 E2E 测试。
2. 新建版本化 VLM core contract，移除 rich candidate 输出；新增 deterministic `candidate-catalog-v2` / `beat-material-catalog-v1`。
3. Stage 1–3 增加短 ID alias layer；模型不再回填 input binding、完整 hash/ref、可派生 IDs/常量。
4. Stage 3 拆分 AuditContextManifest 与 EditorialPromptContext，并按 story closure 分区。
5. 新建 production Recipe v2，承接 video/audio spans、proofs、junctions、Blueprint binding；保留现有 fixture Recipe 仅供测试。

### P1

1. 以 model input token budget，而不是 8/16MB byte cap，进行确定性上下文裁剪与分批。
2. 支持 episode/window 级 VLM recompute 和受影响下游 revision，避免全量重跑。
3. Context Prepare 增加 optional/required requirement 与重试/重算语义。
4. Media policy 拆成 physical、timed speech、runtime capability 三层；校准只绑定时间兼容字段。
5. 补齐编辑/发布 QC、独立 publish decision 与外部 batch atomic visibility。

## 验证

定向测试：

```text
8 passed, 62 deselected
```

覆盖 semantic story profile/composition/V4 PostgreSQL reader。关键点是测试通过包含“V4 被 Stage 1 reader 拒绝”的预期用例，因此不能将这一结果解释为端到端通过。
