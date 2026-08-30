# Pipeline 阶段与单集重算设计

状态：详细设计；**重算 API 尚未实现，不是当前 API 使用说明**。证据基线：
`17530231`（2026-08-28）。当前可用操作见 [PC 运行说明](pc-semantic-pipeline-run.md)。
本设计补齐现有架构，不恢复已删除的总契约，不引入旧 pipeline、Agent 强制编排、
外部发布或新的人工审批系统。Kernel 负责结果与复用校验，Pipeline 负责固定 DAG；
未来 Agent Runtime 可调用同一类型化接口，不能绕过 Kernel 改写结果。

## 1. 目标、现状与边界

用户需要的是：真实流程出错后能看原始输入输出，修复后只重算必要部分，
不必重复上传、转码或调用已经成功且仍适用的模型结果。

当前三种行为必须分开：

| 行为 | 当前状态 | 目标语义 |
| --- | --- | --- |
| 相同请求、相同幂等键再次提交 | 已有 | 返回同一 run；不是强制重新生成 |
| 同一命令的 transport retry/reconcile | 已有 | 确认可重试才增加 Attempt；结果未知时对账同一次调用 |
| 指定阶段/集数重新生成 | 未闭合 | 新 run、新命令、新结果；通过显式绑定复用旧输入/结果 |

2026-08-28 修复：HTTP `/resume` 可唤醒 accepted/running run 中已有的
pending/indeterminate 命令，包含 semantic_only 的 VLM；校准等待仍只处理
media_preflight。它不是终态阶段重跑入口，不能把已失败/拒绝的 VLM run 恢复为 pending。
完整批次的 VLM finalizer 仍要求同一 Job、完整集序和统一 request policy。

已实现基础：共享 `VlmSemanticPolicyIdentityV1/VlmReuseIdentityV1` 与原始
SourcePrep 精确绑定投影，能够比较兼容性并保留原请求；**尚未接入付费重算派发**。
指纹通过不代表已拥有跨 Job 读取权或可跳过成功 Receipt 验证。

首版范围限定为 **VLM + 已提交、完全相同的 SourcePrep 集合**。已经实现的第一条
路径只接受 `completion_scope=full_stage`：它为完整 VLM 阶段创建一个新的 Run，先以
`BindWholeSeriesSourcesCommand` 把原 Run 的精确 SourcePrep 证据绑定到新 Job，再由
既有 Context/VLM 阶段正常执行。它只在 `semantic_only`、`video_only` Context Pack 且
安装 execution profile 与父 Run 完全一致时启用。`source_prep`、ASR/VAD、故事阶段及
逐集选择性重算仍未实现，必须明确返回 unsupported；不能以 `force=true` 放开任何阶段。
换源文件、改集序或改素材授权集合仍走新的完整 SourcePrep，不声称已经支持增量换源。

## 2. 不可破坏的规则

1. 旧 Command、Receipt、ArtifactSet、生产 Job、profile 和源文件归属不可改写。
2. 复用表示“本次引用了另一生产者的既有成功结果”，不是伪造本次执行成功。
3. 新 run 只能消费其冻结输入清单；禁止执行时查 `latest` 或混用不同快照的子结果。
4. 改动配置不等于一律重算。仅影响本阶段输出/解释的变更才改变兼容指纹；
   原生产者完整 profile/hash 仍用于验证历史真实性，不能从旧记录删除。
5. 失败、拒绝、结果未知、缺 Blob 或引用不闭合的结果均不能作为成功复用。
6. 旧 run 的下游产物保留历史有效性，但不能代表新 run 的完成结果。
7. 相同策略强制重算也可能得到不同模型输出。新的下游必须绑定新输出 hash，
   不能仅因策略没变就沿用旧故事或 Recipe。
8. 调试文件只供查看；没有调试文件不影响已提交结果，存在调试文件也不证明结果成功。

## 3. 首版接口与执行快照

`POST /v1/pipeline/recompute` 已注册。它沿用 Pipeline 的 Bearer 认证与
`Idempotency-Key`，不是 Agent/chat API。当前已接纳的请求比完整设计窄：没有
`episode_numbers`、`target_profile_ref` 或 `continuation`，服务端只允许当前安装的精确
profile 和完整重跑。封闭请求如下（所有字段必填，无任意 provider 参数、路径或 Python
类名）：

```json
{
  "base_run_id": "pipeline_run_00000000000000000000000000000000",
  "expected_version": 3,
  "stage": "vlm",
  "completion_scope": "full_stage"
}
```

上面的其余 `selected_only` 规划字段是后续设计，而非现行 API 契约。服务不能接受它们，
也不能把单集结果伪装成完整 VLM Batch。

- `episode_numbers` 是用户集号，严格升序、无重复、从 1 开始，必须属于源集合；
  内部 `episode_index` 从 0 开始，只在 API 边界转换一次。全阶段重算显式列全体集号。
  `selected_only` 禁止空选择；`full_stage` 允许 `[]`，含义为“仅闭合/补齐”，
  不强制重新生成任何已兼容成功成员。有缺口仍须计划扩展确认，无缺口则零模型调用。
- `target_profile_ref` 必须解析到服务端允许的精确不可变配置，示例不是有效配置名。
  不允许客户端提供已接受标志、任意 hash 或任意 provider URL 来取得执行权限。
- `expected_version` 保护所观察的父快照；首次接纳时读锁并验证。
  相同幂等键重放先匹配请求 hash，返回既有子 run，不能因父版本后来改变而另建。
- 首版父 run 必须为终态 `succeeded/failed/denied/recompute_needed`，且涉及的生成
  调用已对账。runtime failed 不代表同 chunk 的所有 provider 调用都已结束，
  必须读取 Kernel Attempt 链。不能借重算绕过正在执行或结果未知的 provider create。
  对 terminal run 中遗留的未知 Attempt，使用 Kernel 的同 Attempt 对账入口，
  不重新开启父 run；若尚无可调用的维护入口，此类输入先返回 `reconciliation_required`。
- `continuation=inspect`：只执行选择集，不扩展到其他集/下游；
  `continue` 只在 `completion_scope=full_stage` 时合法，表示允许执行计划中的全部缺口。
  `selected_only + continue` 返回 400，不能有隐式行为。

两种闭合范围：

| 范围 | 结果 | 后续 |
| --- | --- | --- |
| `selected_only` | 新的 `VlmSelectionResult/v1`，明确所选集和完整剧集总数 | 仅供真实输入输出检查；不是完整 VLM Batch，不进入故事链 |
| `full_stage` | 完整 `VlmBatch` 新版本，引用本次执行和兼容复用的全部成员 | 仅全部成员满足目标策略才允许进入当前计划的后续阶段 |

选择集总是新生成。`full_stage` 中其余兼容成功成员复用，不兼容/缺失成员进入
`execute` 清单；响应必须列出该扩展及原因，不能默默收费执行。若存在选择集以外的
`execute` 成员，创建后默认持久化 hold；只有用户看过完整计划并提交
`resume` 的精确 plan hash/version 后才执行扩展。即使请求是 `continue` 也不能跳过。

新接口返回 `202`、新 `run_id`、父 run、plan ref/hash、选择/复用/待执行列表及原因、
hold 和预算状态。幂等请求冲突/父版本过期为 `409`，非法形状为 `400`，
越权拒绝且不泄漏外部 Job 存在性；未实现阶段为 `422 unsupported_recompute_stage`。
缺预算或待对账返回明确原因，不创建可派发的新生成 Attempt。

首版每次重算形成独立分支，用明确 `run_id` 查询和消费，**没有全局自动 latest、
自动取代原 run 或共享输出目录覆盖**。不同幂等键可建不同分支，结果互不混写；
共享 lineage 预算仍须 CAS，不能以换 key/run ID 重置预算。不要为此先做发布头系统。
同一 lineage/source/episode 的生成按 §6 串行授权；不同分支可以存在，但不能同时
把同一集重新发送给 provider。不同集仍可并行，不把整剧锁成串行。

## 4. 最小数据设计

复用现有 Run/Command/Receipt、不可变 Artifact 和 outbox；RecoveryLedger 只复用
已有设计约定，**其跨 lineage 预算 controller/head/CAS 尚无可用实现**。
当前 `reserve_generation_attempt/reserve_next_generation_attempt` 仅约束单 Command 的
Attempt 次数，不能代替分支间预算或同集重叠检查；所缺能力属于首个切片的交付物。
不新建一套 ArtifactBus、可写 success 标志、第二套预算计数器或通用任务平台。
新增 schema/version 是显式类型扩展，旧 v2.1.3 数据按原语义读取，不做原地升级。

### 4.1 RecomputePlan/v1（不可变 Artifact payload）

| 字段 | 必要性 | 写入/使用 |
| --- | --- | --- |
| schema_version | 必需 | 接纳时写；分派/解码选择明确版本 |
| base_run_id、base_version、target_run_id | 必需 | 创建时冻结；追溯和快照校验，不改父所有权 |
| lineage_id | 必需 | 从父关系推导并验证；所有分支共同核算预算 |
| target_profile_ref | 必需 | 接纳时 exact resolve；执行和复用都使用这个目标配置 |
| source_binding | 必需 | 绑定完整 SourcePrep receipt/set、producer Job/profile 及授权依据 |
| selection、completion_scope | 必需 | 固定阶段和所选集；区分局部检查与完整批次 |
| nodes[] | 必需 | 每集唯一 node，包含 execute/reuse、依赖 exact refs、原因、指纹 |
| reuse_binding（仅 reuse node） | 条件必需 | 成功生产者 Receipt/ArtifactSet/Job/profile exact refs；执行前重新解析 |
| inherited_origins（仅 selected_only） | 条件必需 | 冻结未选集的已知 origin/outcome 或 absent，供下次规划；不计成功覆盖 |
| control_policy | 必需 | 固定 inspect/扩展确认边界；不在 Artifact 中记录可变批准位 |

`plan_id/hash` 使用 Artifact envelope，不在 payload 重复维护；不存可由 refs
推导的任意“可复用=true”。execute node 的输出在后续独立结果中产生，不更新 plan。
reuse node 必须直接指向原始生产者，不能只指向上一层复用别名，防止循环/无限追溯。
planner 只从指定 base run 及其已冻结的继承映射中选 origin，不扫全库寻找“最新成功”。
已由该分支选择重新生成的集以本分支 outcome 为准；如果失败，不允许偷偷退回祖先成功值。
selected_only 可以继承未选择集的原 origin 映射供下次规划，但不把该映射当作本次
SelectionResult 的成功覆盖。继承链有环或无法精确解析时拒绝计划，不按时间戳猜。
`selected_only.nodes` 只含选择集；`full_stage.nodes` 恰好含源集合所有集且没有
`inherited_origins`。旧普通 run 的初始映射从固定源 manifest 和确定性 child keys
读取已提交结果构造，明确记录 absent/failed，不能把未执行解释成成功。

### 4.2 持久化运行控制（Run 的投影扩展）

| 字段 | 必要性 | 写入/使用 |
| --- | --- | --- |
| recompute_plan_ref（nullable） | 条件必需 | 只有重算 run 设置；配对 id/hash FK，不允许外部任意写 |
| hold_reason、allowed_frontier | 条件必需 | inspect/扩展确认时写；Store 派发前检查哪些阶段/集可以执行 |
| version | 复用现有 | 接纳/hold 解除 CAS；不用新的平行 version |

具体 SQL 落在现有 runtime 表的增量 migration；exact FK 必须使用目标表完整唯一键。
创建目标 Run、plan set、幂等映射和 outbox 采用同一数据库事务。
plan、源集合、生产者 Receipt 的存在性/归属/状态在提交前由 Kernel/Store 验证。
控制字段不能改变 receipt 的终态；只控制以后是否派发。
`allowed_frontier` 是封闭的阶段/集集合，不能是任意 SQL/表达式或另一份 DAG。

### 4.3 复用绑定与 Blob 读取

旧 Store 按生产 Job 校验 Blob claim；**给新 run 一个旧 BlobRef 并不足够**。
新增 `BoundVlmInputs/v1`：目标 run/plan exact ref + 原生产者 exact refs。首版由专用的
`BindWholeSeriesSourcesCommand` 将此授权投影为目标 Job 的**只读 source binding**：它在一个
事务中验证 origin 成功 Receipt、源集合与当前授权策略，给同一个 immutable Blob object 增加
目标 Job claim，并写入目标 scope 的等价 SourceManifest 与 `source_reuse_binding/v1`。
不会复制字节、不会转移 origin owner，也不会放宽现有
`read_immutable_blob(job, ref)` 的 Job 边界。

所以 request factory、finalizer 与下游读取器继续只读取目标 Job；不增加可由普通
Runtime/HTTP 调用的 `read_blob_as(producer_job)` 后门。`source_reuse_binding/v1` 保留 origin
Job/Receipt/ArtifactSet/slot/reference：**专用写入事务**独立重读并闭合该 origin；目标 Job
读取 SourceManifest 时则验证严格双成员集、目标 scope、binding target/policy/hash，不能把
任意两成员集解释为 source reuse。接纳和真正绑定时均验证当前源授权；过期/撤回的授权不能凭
旧 Receipt 创建新 binding。

## 5. 兼容性、范围扩展与下游失效

不要修改历史 request hash。新增封闭的 `VlmReuseIdentity/v1`，分别从原生产事实和
目标计划确定性推导；**兼容指纹是可复用的必要条件，不是充分授权证明**。
指纹按集比较原 origin 与目标输入，不要求不同集的输入 hash 相同。
批次统一的是 `VlmSemanticPolicyIdentity/v1`（模型/提示词模板/schema/采样/解析规则），
不是不同集的完整渲染后 prompt。新 finalizer 显式定义这一投影契约并保留原始 policy，
禁止直接删掉旧 finalizer 的完整 policy 相等检查来实现“兼容”。

首版 VLM 指纹包括：同一源集合及授权范围、单集源/代理/TimelineMap 与输入帧集合、
完整 system/user 上下文、模板/别名映射版本、模型的明确版本和 provider 项目隔离域、
输出 schema、采样/生成参数（包括输出 token 上限）、语义解析/验证策略。
原请求有全剧背景/人物表时，其 exact ref 也必须加入，不能假定单集只依赖单集媒体。
不要为了提升命中率去掉任何影响语义的字段。

无关的 debug 路径、日志级别、worker 并发、HTTP 端口、未被该阶段读取的后续
render 配置不进入指纹；transport retry 调度与最大生成次数单独冻结/核算，不决定
已成功语义输出是否相同。仍记录原代码/profile/hash 作为审计证据。
模型/解析器是否兼容不能由模型自称；没有明确版本/依赖声明时，默认不复用。

旧结果若能从不可变请求/配置完整重建全部身份字段，允许经独立验证生成兼容投影；
缺字段不能用当前机器默认配置补全，只能报告不可复用。不做“任意旧版都可复用”的兼容层。

| 变更 | 首版决策 |
| --- | --- |
| 同策略重跑失败的第 1 集，其他 49 集成功且同指纹 | 第 1 集新命令；复用 SourcePrep + 49 集；新 finalizer 闭合 50 集 |
| 全局提示词/token 上限变更，只想先试第 1 集 | selected_only，只生成 1 集；结果不声称全剧完成 |
| 同上，但要求 full_stage | 其余旧策略成员不兼容；计划列出全部重算缺口，确认后执行 |
| 只改日志/无关 render 配置 | 验证完整旧生产身份后，VLM 指纹不变，可以复用 |
| 只改一个源视频/集序 | 首版换源不走此接口；新 SourcePrep，不能复用旧完整源集合 |
| 只换机器、媒体和模型请求完全相同 | 不因机器路径不同而重跑 VLM；仍验证权限、Blob 可达性和精确输入 |

**下游按 actual dependency refs 失效，而非按阶段序号简单清空数据库**。
新 VLM 输出首先使其 batch aggregate 需要重新闭合，随后使用旧 aggregate 的
Narrative/Portfolio/Blueprint/Recipe 不属于新快照。首版不跨快照复用这些下游阶段。
新的 `selected_only` Result 不能作为完整 batch 输入；旧失败记录不能补成成功成员。
新 full_stage finalizer 校验：唯一且完整集序、统一目标策略、源授权、每个生产者
精确成功 closure、复用证明、新输出与目标 run 归属；成功后才产生新的整批 Receipt。
读取和 finalizer 都拒绝“旧失败 + 新成功重复集号”或“少一集但总数填 50”。

## 6. 事务、幂等、预算与暂停

### 6.1 生成与恢复

1. 接纳事务冻结父快照和源绑定、目标配置、plan/outbox。无 provider 副作用。
2. worker 读取 exact plan，Kernel 复算绑定，Store 校验 hold frontier。
3. execute node 以 target run + plan hash + node identity 生成新 Command key。
   不给旧 key 加随机后缀；同一 node 的重试始终属于同一命令。
4. 遵循 [LLM 共同请求与重试身份](llm-stage-contracts/00-shared-request-envelope.md)：
   新语义生成先取得 lineage 的 exact reservation；每个 GenerationAttempt 至多一次
   provider create。重复投递、超时重入不另计一份语义重算预算。
5. 成功/失败/结果未知分别持久化，不把 HTTP 超时解释为 provider 失败。
   已知 response ID 对账原调用；确认 retryable 才按冻结上限递增 Attempt。
6. 结果 Blob 暂存验 hash 后，Set/Receipt/Attempt 绑定在事务中提交；
   崩溃后先读取原 Attempt/Receipt，不重复付费生成。孤立 Blob 不构成成功。
7. finalizer 重读 plan 和成员 refs，在新 target Job 内闭合结果。任何不足只留下
   可诊断状态，不把部分集合投影成 full_stage success。

手工重算是新请求，不等于自动恢复重试，也不能无限绕过成本预算。首版为父 lineage
绑定明确重算 allowance；没有现成 ledger 时，用 exact-head CAS 初始化一次。
用户可显式增加额度，但新 run/key、换 Runtime 或失败本身不能自动刷新额度。
按已有 immutable Ledger/head 约定补齐最小实现，不为 HTTP 层造可写余额表。
首个切片只实现本功能需要的 initialize/reserve/finalize/exhausted、同集互斥和 exact
head CAS，不以“完成所有通用 Recovery 策略”为前提。Ledger 的 allowance/reservation
是不可变 Artifact 内容；head 只定位当前精确 revision，不是另一个可改余额。

不同 key 的重算在真正派发时，必须在同一个 lineage Ledger exact-head 锁事务中：
检查该 source/episode 是否已有未完成 reservation/Attempt → 检查 hold/frontier →
扣预算并为该 node 预留唯一 reservation。commit 后才允许 provider create。
在途/结果未知的 reservation 不能仅因 lease 超时释放；须由终态对账 finalize。
并发竞争者返回 `reconciliation_required` 或等候既有 reservation，不能先各自读
“没有在途”再分别发请求。旧无 lineage 的父调用也必须纳入初始化检查；首版迁移启用前
停止可创建这些旧调用的 worker，不能靠新代码单方面约定隔离旧派发路径。

事务故障点必须覆盖：plan 已提交但尚未 enqueue、预算已预留但尚未派发、provider 已接收
但客户端未得响应、Blob 已暂存但 Receipt 未提交、Receipt 已提交但 stage 未投影。
都不能靠“重新 run 一遍”解决。

### 6.2 真实输入输出检查的持久化 hold

现有 `STOP_AFTER_PROBE` 是服务进程开关，不是数据库级多 worker 保证。
同 DB 上另一 worker 不带变量可能继续批量执行；`indeterminate` 也不应长期兼任人为暂停。
首版新重算路径必须使用持久化 hold，不继承这个弱保证。

inspect/扩展确认控制必须在任何生成派发前写入。Store 的 Generation 派发许可同时检查
目标 run、node、control version 与 `allowed_frontier`：只允许所选集，其他集不能创建
新 Attempt。重启、另一个进程、重新入队都读同一控制记录。
允许已授权的在途 Attempt 完成/对账，**hold 不等于取消或保证没有已发出的网络请求**。
完成选择集后，状态按结果范围分别投影，不把成功子 Receipt 改成 indeterminate：

| 计划及结果 | run 结果状态 | hold / 后续 |
| --- | --- | --- |
| selected_only 全部所选集成功并提交 SelectionResult | succeeded | 无待执行节点，终态；可以作为下一次 full_stage 计划的父 run |
| full_stage 选择集成功，但扩展/完整闭合尚未确认 | paused_for_inspection（新增非终态） | 保留 hold；只可 resume 此计划，首版不能作为另一次 recompute 的父 run |
| 任一所选集失败/拒绝 | failed/denied | 保留失败/debug 证据；按 Kernel Attempt 核查后可作新重算父 run |
| 任一所选集仍在途/结果未知 | running，节点 running/indeterminate | 仅原 Attempt 对账，不进入成功或可重算终态 |

`succeeded + completion_scope=selected_only` 必须在 status 中并列返回，不能只显示
“全剧完成”。空选择且无 execute 缺口的 full_stage：continue 可直接闭合；inspect
仍先暂停供确认，确认后只执行 finalizer，零模型调用。所选集失败显示真实错误而非暂停。

新增 `POST /v2/pipeline/resume`，封闭请求要求
`run_id + expected_version + expected_plan_hash`；
它仅解除该计划持久化 hold，不改变输入、不重开失败命令。新请求形状须版本化并保留
旧 `/v1/pipeline/resume` 的非终态唤醒与 media-preflight 校准语义。解除与 outbox 写入同事务；两 worker 竞争只允许
一次 control CAS。选择集失败时先改策略/新重算，不能以 resume 忽略失败继续。
`selected_only` 不能 resume 成 full_stage；查看后需要另建完整计划，引用兼容生产者。

## 7. Debug 与时间上下文的澄清

继续按 `<debug-root>/<run_id>/<stage>/` 保存真实请求、响应/终态和错误。
新路径额外镜像 plan、origin refs 和 reuse 原因；reuse node 不伪造一次新模型请求，
只指出原始调用/Artifact ref。debug 文件丢失时从权威记录展示可得内容，不能编造完整上下文。
认证信息不落文件；视频和 debug 不进 Git。

此前真实调用能确认的是 `length` 终止、输出未闭合。**不能据此确定 token 超耗由
frame ID/PTS 引起，也不能保证缩短 ID 就解决问题**。`proxy_pts` 必须结合对应
`time_base` 才能换算秒，不能看到整数范围就擅自假定 90 kHz 或 34 秒。
frame 短 ID 映射若后续实施，必须保留可逆映射并纳入 prompt/input 身份；它只是协议
表示优化，不证明 provider 实际看到了同一帧，不替代 TimelineMap 或精确切点编译。
本任务不改变提示词、锚点密度、时间解释或 ASR/VAD 职责。

## 8. 实施顺序与验收

不要求先完善整套治理系统再跑业务。按两个可单独验证的功能切片交付：

1. **选择集检查闭环**：Kernel bound source reader/新版本 request + 不可变 plan +
   recompute HTTP + 单集真实生成/SelectionResult + durable hold + 最小 lineage Ledger
   initialize/reserve/finalize/CAS 和同集 overlap gate。**预算并发及关键崩溃测试是本切片
   启用前条件**，不能等第二切片才补。可重用已完成的
   SourcePrep，不依赖 49 集成功，不产生完整 Batch 假象。
2. **整批复用闭环**：VLM 指纹推导、compatible sibling bindings、新版 finalizer/reader、
   缺口扩展确认及整批并发/闭合测试。随后以相同接口逐阶段扩展到实际 DAG。

建议代码归属：Kernel 的 pipeline/store/vlm 负责 exact refs/绑定/闭合；
Pipeline runtime 的 models/service/postgres/vlm_stage 负责计划和派发投影；
HTTP 仅解码认证和返回状态，不直接生成 Receipt。Agent 不持有另一套缓存/重算规则。
迁移只加新模型/版本和控制字段，旧入口保持同 Job 约束；无法理解新版本的 worker
不能抢新 outbox。新版本启用时必须验证同 DB 所有可领取该任务的 worker 都支持新门禁。

### 8.1 必须新增的契约测试（以下是验收要求，不是已通过声明）

| ID | 反例 / 预期 |
| --- | --- |
| RC-01 | 同键同 body 二次提交只产生一个目标 run；同键不同 body 为 409 |
| RC-02 | 旧 terminal Command/Receipt 不被改写；父版本过期在派发前拒绝 |
| RC-03 | SourcePrep 复用实际读取生产者 Blob claim；伪造跨 namespace binding 被拒绝 |
| RC-04 | 失败第 1 集重算 + 49 个兼容成功集，只生成 1 集并闭合准确 50 集 |
| RC-05 | 改全局提示词/token 上限后不能复用旧策略 49 集；selected_only 只跑选择集 |
| RC-06 | debug/日志/render 无关变更不破坏 VLM 兼容性；历史完整 profile 仍校验 |
| RC-07 | 隐含全剧上下文、TimelineMap/模型/schema 变化导致对应指纹变化 |
| RC-08 | 集号 0、重复、越界、缺一集、伪造总数、乱序均不能完整闭合 |
| RC-09 | 旧 reader/finalizer 拒绝新 binding；新 reader 对每个原 producer 独立校验 |
| RC-10 | 旧记录缺指纹依赖不从当前默认值补全；缺 Blob/授权撤回不复用 |
| RC-11 | 新 key/不同分支竞争最后预算，真实 PostgreSQL CAS 只授权一份 reservation |
| RC-12 | parent/Attempt 在途或未知时不新建重叠调用；重启只对账既有 response ID |
| RC-13 | §6.1 五个崩溃点均可恢复，无重复 provider create/伪成功 |
| RC-14 | 两 worker 环境不同也共同遵守 hold；旧 worker 不能领取新计划 |
| RC-15 | resume CAS/plan hash 过期为 409；失败子命令不因解除 hold 重启 |
| RC-16 | full_stage 扩大到未选集前必须确认 exact plan；selected_only 不进入故事链 |
| RC-17 | 替换一集导致新 aggregate/downstream refs；旧产物保留但不冒充新 run 输出 |
| RC-18 | debug 删除/重建不影响状态；reuse 没有虚假请求文件，不保存密钥 |
| RC-19 | API/Kernel adapter 同一请求产生同计划和规则结果，不需要启动 Agent |
| RC-20 | 跨 PC/Mac 恢复相同输入不重复生成；目标配置不兼容时明确计划重算 |
| RC-21 | selected_only 成功为可作父节点的终态；full_stage 暂停不是父节点也不是全集成功 |
| RC-22 | 单集剧或全部结果已兼容时空选择 full_stage 零模型调用闭合；有缺口仍需确认 |
| RC-23 | 首次 lineage 初始化竞争只产生一个 Ledger，后续 run/key 不刷新 allowance；首切片包含 RC-11～15 |

验收必须含真实 PostgreSQL 双连接/重启实验；内存 fake 通过不能证明数据库谓词、
FK、CAS 和多 worker dispatch 正确。先用假 provider 做故障注入；经用户批准后，
PC 真实 run 只执行已选一集，交付其 request/terminal/Receipt，再考虑批量。
本次文档审查不宣称这些新增能力已经实现或已经上线验证。
