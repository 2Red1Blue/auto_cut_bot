# 09 模型边界重构：丰富语义、确定性投影与局部恢复

## 1. 状态、范围和依据

状态：2026-09-05 的待实施方案。不是已上线行为，也不是新的整套生产契约。
代码调查基线为 `eb2358b7`；执行分支仍为 `feat/v213-contract-codegen`。
本方案细化 [06 责任边界](./06-llm-program-responsibility-redesign.md)、
[07 字段保真账本](./07-v23-field-parity-and-external-references.md) 和
[08 字幕与精切融合](./08-subtitle-aware-vlm-to-exact-span-design.md)，沿用其 Global Phase ID。
现状说明仍见 [03 Stage 2](./03-stage2-portfolio.md) 与 [05 Debug](./05-errors-debug-status.md)。

本次只重构模型与 Kernel 的边界，不改五阶段职责、双 Runtime、Store/Command/Receipt，
不引入新 Agent 编排框架。VLM 继续理解画面与烧录字幕；SenseVoiceSmall/FSMN 只做物理时序。
ASR/VAD 文本、字幕轨和 API 高光不进入模型语义输入。先复用已付费响应，不能以新建全量 run 代替定位。

### 1.1 已确认的故障与证据限制

| 现象 | 实际证据 | 结论 |
|---|---|---|
| Stage 2 被解释为错字段后空默认 | Mac `pipeline_run_ccc10a8fcc4d4c8497c7aad25b584928` 原始响应包含正确 `thread_refs` 和 `material_requirements`；四个提案材料数为 2/5/7/3 | 不接受“改随机种子重跑即可”的归因；`narrative_refs` 是 Python 派生属性 |
| 人物引用不闭合 | 该输入有 6 个 entity、0 个 character；模型把存在的 person entity ID 标成 character | 确认类型衔接冲突；不能把 entity 直接改名为 character |
| 事实遗漏 | 提案 001/003 遗漏所选义务要求的 4 个事实；002/004 闭包正确 | 固定的集合运算归程序，不让模型重复抄写 |
| WSL 三次 VLM 失败 | `pipeline_run_b0bb0b8b6ba4417999cdbcf2e9397592` Receipt `d7cd9cce-26aa-49a7-86d5-25d99834a65d` 的三次原因分别是 narrative_functions/tags/editing_modes 非规范顺序 | 合法集合排序不值得付费重新生成；归一化后仍需检查其余内容 |
| 候选伪帧支持 | `candidate_catalog.py` 对 VideoObservationSupportV4 使用整个 manifest 的 frame hashes | 可寻址帧不等于模型声称的支持帧，必须分类型表达 |

上述 Stage 2 原始内部 exception 尚未从当时数据库恢复，不能宣称已证明唯一首个抛错点。
Mac 与 WSL 的 run 不混用：前者不是 PC 本次执行成功的证据。

## 2. 开源方案吸收与不采用的部分

调研日期：2026-09-05；链接为项目官方文档。吸收机制，不增加运行时依赖。

| 来源 | 吸收 | 不照搬 |
|---|---|---|
| [PydanticAI Output](https://pydantic.dev/docs/ai/core-concepts/output/) | 类型化输出与业务 validator 分开；把具体可修复错误反馈给模型 | 不把 Schema 通过视作事实正确；不让 validator 替模型创造事实 |
| [LangChain Structured Output](https://docs.langchain.com/oss/python/langchain/structured-output) | Provider 原生结构约束与本地验证并存；错误分类决定反馈 | 不整体迁移 LangChain；不因某 Provider 不支持 Schema 就悄悄改模式 |
| [Instructor Retry](https://python.useinstructor.com/concepts/retrying/) | 按错误选择重试，限制次数与累计用量，保留失败尝试 | 不在现有 Command 外再套一层独立自动重试；累计预算不是单请求硬上限 |

本项目推导：模型生成领域意图，程序恢复精确身份；结构化输出降低格式错误，不能保证外部引用存在，
也不能保证字幕识别、剧情解释或剪辑价值正确。仍需真实 fixture、业务验证和成片验收。

## 3. 一条边界、三类职责

```text
已提交输入 -> 版本化模型视图 + 私有引用映射 -> 原生 Schema 请求
                                                    ↓
原始响应 Blob -> 严格解码 -> 有限规范化 -> 类型化草案 -> 确定性领域投影
                                                    ↓
                             独立业务校验 -> ArtifactSet / Receipt
```

| 字段/信息 | 所有者 | 规则 |
|---|---|---|
| 人物外观、动作、关键屏幕字幕、情绪变化、事件与时间模式 | VLM | 保留丰富信息；允许无法判断，不强制高置信或零误差 |
| 情节解释、人物对应假设、关系理解、候选价值 | VLM/文本 LLM | 与观察区分；没有证据不能升级为已证事实 |
| 故事标题、主张、钩子、义务选择、叙事组织、目标时长 | Stage 2 LLM | 保留创作空间，受已给素材与策略约束 |
| owner/namespace、Blob/Artifact/Source ID、hash、授权事实 | Kernel | 私有 envelope 保存，不要求模型复制 |
| 所选义务的必选事实并集 | Kernel | 从精确 NarrativeGraph 推导；不得补造图中缺失事实 |
| enum 集合的规范顺序 | 版本化 normalizer | 只排序明确的无序集合，不排序叙事顺序，不去重掩盖非法输入 |
| 必须执行的物理安全检查 | Policy + Kernel | 模型不能关闭；模型可提出更严格的材料需求 |
| 帧/sample 端点、映射误差上界、精确 A/V 配对 | 物理 producer + ExactSpan | VLM 粗区域不是切点，也不证明零时间误差 |

## 4. Stage 2 compact v2 的可实施合同

### 4.1 版本选择与输入

使用已持久化的 `generation.prompt_version` 注册 `stage2-proposal-compact-v2`，一次性选择
context builder、response schema、decoder 和投影策略。不得只更改提示词却沿用 v1 decoder。
旧 prompt_version 走原 v1 分支；不得全局替换 `prepare_stage2_request` 的旧构造逻辑。

模型视图保留以下内容，删的是重复的技术身份，不是剧情：

- `subjects`：已观察 person entity 或已建立的 character，附显示描述、别名和身份状态；
- `facts/events/threads/obligations`：原意、关联、义务成功条件与必要上下文；
- `episodes`：分集摘要和前后关系；
- `candidates`：语义区域、候选价值、时长估计、不确定性与可支持义务；
- `policy_choices`：允许的风格、类型、预告策略、时长和必须的材料约束。

确定性别名使用独立类型命名空间，例如 `p1/f1/e1/t1/o1/c1/s1`。映射由完整精确身份排序后生成，
排序键为 canonical `(member_ref, object_type, object_id)`；同一个请求内一一对应。
映射与输入内容绑定，跨 run 的 `p1` 不代表同一个人物。模型请求不包含全长 hash。
前缀固定为 p=subject、f=fact、e=event、t=thread、o=obligation、c=candidate、s=source；
character 与 person entity 均属 subject 视图，但映射值保留各自 object_type。
初版 c 引用仅用于输入视图中的素材说明/关联，不是输出中可指定最终选片的字段。
私有 envelope 保存映射内容/摘要、完整输入绑定、prompt/schema/projection 版本和 provider 请求 hash。
Compiler 与 Reader 根据同一已提交输入重建映射，不信任模型提供映射，也不信任运行时临时字典。

初版不强制 2K token，也不按字符粗暴截断。先去除冗余身份和重复结构，记录真实字节/token/语义项计数。
超过显式请求预算时报 `CONTEXT_BUDGET_EXCEEDED`，不静默删事实；后续分片是另一个有覆盖证明的优化。

### 4.2 模型输出与程序输出

compact 模型提案字段及投影责任：

| 模型字段 | 含义/投影 |
|---|---|
| `title`, `narrative_claim`, `audience_hook` | 保持文本语义，不压缩成标签 |
| `thread_refs`, `obligation_refs`, `key_subject_refs` | 当前请求的短引用，分别限定类型 |
| `genre_tags`, `editing_profile_ref`, `teaser_strategy` | 只能从模型视图允许值中选取 |
| `target_duration_seconds` | 保留 min/max 目标，不等于物理可剪时长 |
| `material_requirements` | 每个选中义务的材料需求，使用 obligation/source 短引用；明确最低时长和额外约束 |

根必须包含固定 schema discriminator 和 proposals 数组，不要求回填输入 hash。
程序生成 proposal ID、恢复精确引用与 input binding、计算必选事实并集、合并不可关闭的物理要求。
源授权取交集，额外限制只能收紧：没有合法素材时返回不可行，不放宽 Job 的授权。
可行性仍由 Kernel 求解，模型输出的候选排序/价值不是 Admission。

初版约束合并是小型确定性函数，不引入通用 Policy 框架：

1. `G` 为已提交 SourceGrant 中允许 render_source 的源，`J` 为 Job 允许集合，`M` 为模型额外允许集合；
   模型用显式 `source_selection=all_granted|subset`，前者取 M=G，后者须提供非空合法 source refs。
2. 有效源为 `(G ∩ J ∩ M) - (Job 禁止集合 ∪ 模型禁止集合)`；未知源不是空集合而是输入错误。
   交集为空则该材料不可行。选择 all_granted 也不能越过 Job 限制。
3. 物理检查集合为 `StoryPolicy.required ∪ 模型 additional_checks`；检查枚举必须来自策略注册集合，
   没有 disable 字段。阈值若允许模型收紧，minimum 取 max、maximum 取 min，区间冲突为不可行。
   初版 wire 不开放未定义合并语义的数值阈值。
4. 保留基线策略 hash、模型原始选择和合并结果；测试证明增加限制不可能扩大有效素材集合或删除必做检查。

关闭未知字段；拒绝错类型、未知/跨请求引用、重复引用、缺材料义务、超预算、非法时长。
缺字段不得替换为空集合。“程序推导字段”必须从新 wire 中删除，不能既让模型输出又偷偷覆盖。
不在 v1 decoder 上增加 `requirements -> material_requirements` 等猜测别名。

### 4.3 person 与 character 的兼容范围

`entity(kind=person)` 是画面里观察到的人；`character` 是有身份依据的归并对象。
新 `key_subject_refs` 支持这两个明确的分支，保留原 object_type；其他 entity 类型拒绝。
它表示“故事涉及的主体”，不表示已经完成跨镜头身份确认。

领域侧新增 v2 draft/proposal codec；v1 `key_character_refs` 的解码、类型检查和序列化不变。
v2 用 `key_subject_refs`，提供内部统一的主体引用访问，不以 `isinstance` 大范围放宽旧 codec。
Stage 3 的故事输入和独立校验必须读懂 v2；否则 v2 不得成为默认生产策略。
新旧 Artifact 的 schema/strategy 明确区分；不得把含 entity 的新结构写成旧 character-only 版本。

### 4.4 必须接入的三个重建点

- `story_design_compiler.py`：从 audited raw + 精确输入解码/投影；
- `story_design_evaluation.py`：自行重建草案并验证，不调用 Compiler 当作 oracle；
- `compile_story_portfolio_command.py`：命令重放时重建相同 request/hash/投影。

共享纯 decoder 可以复用，但评估器不信任 compiler 已生成的事实闭包、候选判定或 pass。
旧 request golden bytes/旧 Artifact roundtrip 必须保持。若旧 request 已因 earlier thinking/parser 改动漂移，
单独记录不兼容原因并恢复对应版本，不得把当前安装版本冒充旧版本。

### 4.5 持久化、旧响应再处理与部署

Stage 2 Store reader 从持久化 request envelope 读取版本与策略绑定，不读取当前默认 profile 来决定解码。
新策略的 schema/decoder/projection ID 与实现内容 hash 进入私有请求身份；Artifact 标明新 schema/strategy，
Receipt 绑定 exact request/outcome，无需给每张表重复加一套版本列。
未知策略返回明确的 implementation-unavailable，不 fallback 到 v1。Reader 必须重新核对 raw Blob、
原 attempt、provider invocation、完成状态及精确前置 Artifact；三处重建都从该边界获取版本。

保存的 Stage 2 raw 同样支持零 Provider 的派生 reprocess。新命令键绑定原 request/raw hash、目标策略和输入，
相同键再次执行只重放新 Receipt，不覆盖原失败。默认只允许同 wire 的明确版本投影修复。
v1 到 compact-v2 不直接调用 v2 decoder；需要独立、显式启用的迁移 `stage2-draft-v1-to-compact-v2`：

- 保留所有故事选择、文本、时长、材料限制和提案顺序；v1 完整引用在原绑定输入中转换成 v2 短引用；
- required facts 从原选中义务重新推导，差异记录为领域闭包派生，不标成新模型观察；
- 仅 key_character_refs 的错误类型可在同一 graph owner、同一个 object_id 唯一指向 person entity 时，
  转为 v2 subject，并记录 before/after；这是显式迁移，不是通用“错引用自动纠正”；
- 其他未知 ID、跨 owner、缺故事选择、需要猜测身份或改变材料要求时停止本地迁移，报告需模型修复的字段。

迁移按“严格 v1 结构解码 → 未准入草案 → 受限迁移 → v2 全部业务验证”执行，不先要求旧 Graph 准入成功。
这里不是宽松 JSON 解析：真实样本填的是 `object_type=character`，能通过 v1 DTO 的声明类型检查，
失败的是稍后的 Graph 成员存在性检查。若连 v1 结构都不合法，初版迁移就拒绝，不新增宽松 decoder。
所有 reprocess 都追加父 attempt ID、原 request/raw hash、目标策略 hash 和派生结果；
原 Blob、原 attempt 与原 Receipt 只读不更新。提交和重放再次验 hash，发现内容变更即拒绝。

首版部署不加新的 HTTP 协商服务：先部署支持旧读者和新策略的同一 Kernel 包，保持旧默认；
验证 Pipeline/Agent/Stage 3/Store 所需 reader 均支持后，再切冻结策略的默认值。
在任务提交前检查所需策略支持情况；缺读者时拒绝创建该新版本任务，不中途写入半份 Artifact。
进行中的任务仍用原冻结版本。回退默认值不删除新 reader；已写 v2 后不能回滚成不认识 v2 的二进制。

## 5. VLM：先可恢复，再扩充 rich wire

### 5.1 原始响应、归一化和历史版本

新增明确 normalizer 身份 `vlm-enum-set-order-v1`，只处理 candidate 的
`editing_modes/narrative_functions/tags` 三个无序枚举集合。严格 JSON 与重复键、值合法性和唯一性先检查。
保留 raw bytes/hash；另存规范化结果、变换列表、版本/hash。归一化幂等，不改变语义对象数和引用集合。
未知 enum、未知 fact、残缺 JSON、被截断响应不在这一修复范围。

新解析策略显式绑定 schema、normalizer、decoder、projection；Generation 与 Store replay 走相同分派。
旧 hash 不能简单加入白名单后调用新 helper。先登记实际存在的历史版本及可获得的精确实现 bundle；
只有验证过完整依赖和历史 fixture 的实现才可声称支持原样重放。
找不到旧实现时返回 `PARSER_IMPLEMENTATION_UNAVAILABLE`，可查看旧记录，不自动付费再生成。
新版本 dispatcher 的源码不得作为旧实现 hash 的隐式组成；历史身份验证以冻结 bundle 的内容为准。

恢复入口是显式本地 reprocess：读取原始 request/source/window/raw Blob，指定目标解析策略，
新建派生 Command/Receipt，记录来自哪个原 attempt。原 failed receipt 和 raw bytes 不改写。
支持只选择某集/窗口；数据库已存在的相同派生身份直接重放，禁止每次产生新 provider invocation。
源/窗口/模型响应归属、原响应完成状态和 Blob hash 均需核对；只有 debug 文件而缺持久化绑定时，
可以离线诊断，不能直接伪装 committed 成功。

### 5.2 候选支持不再伪造 frame 证据

新 Candidate support 为带 discriminator 的联合类型：

- `video_observation`：视频绑定、模型粗区域、真实不确定性状态；**无 supporting_frame_ids**；
- `frame_anchored`：只保留模型声明并校验存在的 anchors；
- v3 历史分支：维持已有非空 frame 支持结构，绝不全局改为可空。

frame anchor 的存在性只对原 committed request 绑定的同一视频源/窗口的帧身份集合验证，
跨源、跨窗口或不属于该请求的帧一律拒绝。集合是验证边界，不是可自动补入的观察证据列表。

CandidateCatalog 新 schema/strategy、candidate projection、duration/feasibility 和 Stage 2/3 读者同时更新。
VideoObservation 只能产生语义搜索区域，后续 FramePtsIndex 提供的可用帧也不能补写为模型观察证据。
使用整个 manifest 的帧集合是本次明确要删除的行为，不是可接受的 fallback。

### 5.3 富语义升级保持独立

先保留 V23 当前语义字段并修复投影、恢复，再按 07 parity 逐字段迁移 rich wire。
重要字幕、多人/多事实事件、时间模式依据、情绪转折与连续性 unknown 都应保留/补强，
不再用“每事件恰好一个 Fact”“continuity 一律 false”“uncertainty=0”作为提高合规率的捷径。
本次 Stage 2 compact 上线不以 rich wire 全量迁移为前置，也不修改已保存的 V23 内容以伪造 parity。
compact-v2 是 Stage 2 的文本草案协议，不是替代 VLM 感知输出。首批不改变 VLM 语义字段的 wire 形状，
normalizer 只排序集合。新增 rich 字段按 07/08 的独立版本、证据和 fixture 落地后才允许消费者依赖；
不得从“保留富语义”这句目标推断当前已经具备新增字幕/连续性字段。

## 6. 错误信息、重试与重跑范围

| 错误类别 | 动作 | 是否调用模型 |
|---|---|---|
| 合法 enum 集合顺序 | 版本化本地规范化后完整验证 | 否 |
| 输入 owner/类型衔接 bug、程序投影 bug | 修程序，用原 raw 新建 reprocess | 否 |
| 真正未知引用、模型遗漏必需的叙事选择 | 带最小相关上下文与具体错误的当前阶段修复 | 预算允许时才是 |
| 请求参数 4xx、权限问题 | 修请求/配置，不重复相同错误 | 不盲重试 |
| 429/暂态网络错误 | 现有 Command 预算内退避；结果不明先 reconcile | 按原调用状态决定 |
| 素材客观不可行 | 明确不可行/需新故事选择，不删除必选义务凑成功 | 不默认重跑 VLM |
| 解析版本不在安装包内 | 恢复精确实现，或显式迁移原 raw | 否 |

结构化诊断包含 stage/phase、error_code、rule_id（存在时）、JSON path、proposal/attempt、
原 raw hash、期望类型/允许别名的受限摘要、retryability 和推荐重算范围。
内部保留 cause 链；不把密钥、完整 Prompt、signed URL 写入错误反馈。不能只留一句 provenance does not close。

重试仍由一个 Command 层拥有；adapter/SDK 不另开验证重试循环。每次修复使用独立 attempt 和预算，
保存确切反馈与请求。相同输入与相同诊断不无限反复。局部修复后仍整体校验本阶段 ArtifactSet，
不把单提案通过等同整个 Portfolio 成功。

首次实现保留现有 `generation-retry-v1` 的每命令 1–3 次冻结上限及 backoff，不增加自动语义修复调用。
本地 reprocess 的 Provider 预算固定为零；发现需模型修复时先返回明确的修复请求。
后续启用定向模型修复时，反馈改变了请求就必须创建新派生 Repair Command，绑定原 request/raw/诊断和
反馈 payload hash，不能用同一个已冻结 request hash 偷换 prompt；该 lineage 的累计调用预算不得因
换 Command key 清零。这部分不在首批 A/B/C/D 的自动执行范围。
Provider attempt key 由 command/request identity 与 ordinal 唯一决定；重放同一 attempt 不产生新 key。
在结果不明时，只允许 reconcile；lease 到期不等于 Provider 已失败，不得据此盲发下一次请求。
只有确定 failed/not-dispatched 的状态才可在剩余预算内退避重试。无法核实的状态停留在可查询的
outcome-unknown，保留恢复线索；新增测试覆盖未知结果、进程恢复、重复回调和预算耗尽。
稳定诊断键为 `(stage, error_code, rule_id, JSON path, proposal identity)`，不含错误描述全文或模型措辞。
即使每次错误不同，也不能突破命令硬次数上限；未来 Repair lineage 必须另有冻结的阶段/提案累计上限，
未实现该共享预算前不得启用自动 Repair Command。
Debug 按 stage/operation/attempt 保存 request、raw、normalized、projection、diagnostics；阶段摘要可以更新，
历史 attempt 文件不可被 reconcile 的摘要覆盖。

## 7. 分批实施与验收

沿用全局阶段，以下是本任务工作包，不另建 Global Phase 编号：

| 工作包 | 全局阶段 | 交付与前置 |
|---|---|---|
| A 诊断与 fixture | P0 | 精确错误路径；冻结两环境证据及字节 hash；回归不调用 Provider |
| B Stage 2 compact | P3 中的兼容边界子项 | v1 保持；v2 builder/schema/hydrator、主体类型、三个重建点和 Stage 3 消费齐全后才切默认 |
| C VLM reprocess | P0/P1B | 版本分派、有限 normalizer、原 raw 新派生 Receipt；可与 B 分文件并行 |
| D 候选证据类型 | P1A | 去伪帧；Catalog/读者版本一致；在新 Stage 2 真实运行前完成 |
| E 单集真实验收 | P2/P3 的边界验收 | Git 同步 PC WSL；先 A/B/C/D 离线回放，再仅付费一次目标 Stage 2；不重新调用已可用 VLM |

实施顺序：A → (B 与 C 并行) → D 集成 → E。同一文件只有一名实现者；交叉模块由主 Agent 合并审查。
每包及时 Git 提交；Mac 仅编辑与静态审查，pytest/数据库测试在 PC 的隔离验证 checkout/测试库执行。
真实数据库不 reset；业务运行通过 Pipeline HTTP，不能用 Agent gateway 成功代替。

必须通过：

1. 三个真实 enum 失败响应本地再处理：无 provider 调用；通过或准确报告剩余非排序错误，不能预先保证全部成功。
2. Stage 2 真实旧输出离线复现 person/character 与必选事实问题；新输入/投影解决程序职责问题。
   旧 v1 不得用新规则悄悄接受；迁移记录明确区分编程修复、派生闭包与模型新决定。
3. v2 的错 alias、错种类、非 person 实体、重复键/引用、超预算和伪造 owner 全部拒绝；不静默补默认。
4. 相同输入/版本得到相同 request、映射、领域结果；旧 v1 golden bytes/receipt replay 不漂移。
5. 改一个字段不能绕过三处独立重建；编译器和 evaluator 一致，且 evaluator 不调用 compiler。
6. video 支持没有伪 frame refs；frame 支持仅包含实际 anchors；不会因此获得物理端点许可。
7. 使用原视频人工对照重要字幕、事件、候选价值和时间模式，07 parity 不因 token 优化退化。
   Schema 通过或对象数相等都不能代替这项检查。
8. Stage 2 失败只重跑受影响阶段；上游复用必须有可验证绑定，无法复用时说明缺什么，不自动全量重跑。
9. native Schema 不支持的 Provider 明确报能力错误，或由冻结策略显式选替代方式；不静默降级。

首个代码提交只做 A，不等待整套 rich wire 迁移。B/C/D 各自未达标时保留旧默认配置，
不能用新 Policy hash 或放宽校验强推上线。最终验收记录需分别列出单元测试、离线真实 raw、
真实模型调用和本地成片，不能互相代替。

## 8. 本轮审查记录

- 两个只读研究 Agent 分别核对 Stage 2 和 VLM 入口；未写业务代码、未运行测试。
- 第一轮监督审查 `74ba911f-a167-474b-8918-12e2fb3bad57`：Codex 要求补充策略合并、
  Stage 2 持久化/重放、旧响应再处理和部署顺序；Claude 240 秒超时，无有效结论。
- 第二轮 `58d1f2c8-7daf-4a94-a943-94899a56dcb5`：Claude 同意开始 A，提醒帧身份绑定与重试上限；
  Codex 要求明确迁移解码顺序、append-only lineage 和未知结果重试。这些均在 §4.5/§5.2/§6 补充。
- 主审核实 Codex 所称“必须宽松解码”的推论不适用于真实样本：v1 DTO 接受标称 character ref，
  Graph 检查才发现其实际 node_type=entity。采用严格解码后受限迁移，不增加宽松 decoder。
- 最后的文字澄清由主审核对源码；不能宣称两位审查者对最终全文一致批准，更不是代码测试通过。
  B/C/D 在各自实现提交前仍须按本节已知问题做独立代码审查。
- 当前仅方案/任务文档已创建。没有新的付费模型调用、数据库修改、运行时切换或重构代码测试。
