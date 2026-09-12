# 15 富观察 VLM：完整运行设计与实施验收

日期：2026-09-10。用户指定实现模型：gpt-5.6-terra。
状态：实施中；各验收项必须用实际代码/测试更新，设计不等于部署。
上位边界：[强制规范](../../.ccg/spec/backend/vlm-observation-boundary.md)、[14 输入输出](14-vlm-observation-only-contract.md)。

## 1. 目标与职责

主调用充分描述画面、字幕、动作顺序、表情和跨镜头连续性，同时保留模型对动机、关系、
冲突和悬念的解释及未知。数据产品是可长期重用的观察报告，不能压缩成稀疏标签。
可选任务只允许从程序已准备的极短别名选择既有对象。映射/来源/引用/准入留在程序侧。
真正影响下游的问题可以触发一次显式局部复看；格式、分类、候选闭包错误不能重跑整集。

沿用 Kernel Command/GenerationAttempt/Blob/ArtifactSet/Receipt 与现有 Pipeline 执行机制。
不新建替代业务库、文件状态机或第二套 checkpoint。旧 V23/V4 的字节、解析和历史回放保持不变。

## 2. 合同分层

| 层 | 内容与所有者 | 不具备的权力 |
|---|---|---|
| provider 可见 | 视频、浅 Prompt、自由描述 Schema、可选短别名目录 | 不接收私有 ID 映射/凭据/举证表 |
| 模型原响应 | description、moments、interpretations、uncertainties；可选选择 | 不自造事实图、业务枚举、候选或证明 |
| 程序 envelope | 精确 SourcePrep 引用、episode/window、原 bytes、policy、alias map、attempt | 来源绑定不证明视觉内容真实 |
| 可读报告 | 不可变原文与字段级状态、程序身份、选择解析、待定位项 | 接收报告不等于 verified fact 或物理可剪 |
| 下游语义提案 | 从报告与可选外部背景形成叙事、候选意图、蓝图 | 文本提案不能自证存在/可剪/发布 |
| 物理验证 | 后续独立媒体定位和时序证据 | 不能改写模型曾经观察到的内容 |

### 2.1 基础报告与执行层上限

基础 Schema 采用 14 §3，四根字段；自由字符串、不强制对象配额/枚举/ID。
模型不接受领域预算：不限制 moments、解释、未知项或单段描述的语义数量，
也不要求模型为了适配数量而合并/删减观察。输出丰富度由媒体内容决定。

执行层仍有不可取消的技术上限：provider 的最大输出 tokens、请求/流响应字节、
JSON 嵌套/容器安全阈值、媒体上传大小、采样配置、超时、并发与同一远端请求的恢复策略。
它们属于 harness/adapter 的 `ExecutionLimits`，不属于 Observation schema、业务 Prompt 或叙事策略。
解码器遇到执行层截断必须报告 `incomplete` 并保存原始 bytes；不能把它解释成“模型没有观察到”。
启动值按 provider 的实测能力配置和记录，运行时不能偷偷调大重试预算。
根 JSON 必须完整，无重复键、非有限数字/非法 Unicode/过深嵌套。原文总是按原 bytes 保存。
时间提示缺失、不可解析或字段类型错误只降级该可选提示，合法描述保留；不生成精确时序。
缺主描述、主干结构损坏、截断或拒答不得算报告成功。

### 2.2 短 ID 选择

冻结 alias -> existing target identity 的一一映射，模型只看到 a/b/c（必要时 aa/ab）和描述。
完整映射在私有 envelope；请求身份包含映射摘要，重放使用原映射而非当前目录。
程序先检查目录唯一性、可访问 scope 以及执行层可承载的输入大小，再调用模型。
选择字段用独立 variant 的 membership enum，允许 null；unknown/冲突只使选择未解决。
原始未知值保留；不能静默改为 null，也不宣称整份 raw 通过完整 variant Schema。
基础观察请求没有选择目录时不得出现选择字段。选择结果仅为建议，不能提升身份或举证权力。

## 3. 持久化生成与读回

新增 observation 专属 Command/request/result，不扩展旧 SemanticPackValue 或 V4 parser。
沿用 GenerationStore 的 claim/reserve/dispatch/provider-id callback/reconcile/raw-store/commit/replay。
当前 migration 0018 的最终触发器以 execution_kind=generation 校验，不需要仅为新命令改表。
若增加 run profile、窗口执行配置等新数据约束，使用追加 migration；禁止改写旧 migration。

一个成功生成至少绑定：request record、response record、observation report。
受保护 writer 必须从精确 Attempt 的 request/raw 重算报告；拒绝调用者任意提供的已解析成功内容。
reader 必须校验原 Request、完整 Attempt chain、receipt/set/members 和 raw hash，且可由新 Store 实例读回。
有限 Schema 正常化/字段降级要记录程序结果，不覆盖原 response Blob。
terminal 语义/结构失败不能自动用新三次视频重试掩盖；transport unknown 保持原 provider identity 查询。
同一操作重复提交只重放；已提交报告的下游操作不得触发 provider。

主报告按 episode source census 聚合。批次完整性依据唯一 episode+scope 覆盖和 committed child，
不是 debug 文件计数。单集/局部复看不能填充未观察的全集成功。

## 4. Provider 与采样

用独立 Observation provider adapter/policy 复用 Ark Files cache 和 Responses transport。
实际 body 必须只包含新 Prompt/schema；不调用 V23 自动前缀或旧 semantic payload 校验器。
Files cache 复用必须保持真实视频 bytes、fps、endpoint/account 等已定义缓存身份。
音轨能力默认未知/不可用，只有已验证的明确 profile 才能授权音频描述；字幕来自画面。
主观察保留连贯上下文；过长/过密的窗口使用程序定义的有限重叠分段。
采样密度、分辨率、裁剪范围和处理方式均冻结到请求身份；局部提高采样/清晰度是新输入操作。
不凭 schema pass 宣称采样足够，需人工对照动作/字幕密集片段。

## 5. 显式局部复看

复看请求绑定已有报告、精确媒体、程序批准的局部范围、问题和可选 alias map。
VLM 可以用自然语言说明哪里不清楚；程序/操作者选择是否复看及范围，不能自动把猜测的时间当精确端点。
局部媒体必须由可信媒体操作得到且绑定实际 bytes；不能只把整集传入后靠一句 Prompt 声称局部复看。

复看不在父报告中维护“最多 N 次”或累计语义预算。它是一个新的显式操作，
由调用方提交 material question，并受 harness 的调用次数、超时、并发、媒体/流大小和账户策略约束。
相同请求/key 只执行一次；unknown dispatch 保持原 provider identity 查询，不能被再次 create。
调用方如需项目或账户费用治理，应在执行控制面使用独立 SpendPolicy/UsageLedger；
其记录实际 usage，不能把未知 usage 伪装成拒绝观察报告的理由。
新的复看产物绑定父报告但不覆盖它；补充意见仍是模型观察/判断，不能建立自证链。
只解决当前疑问，历史报告仍可读。语义关联使用程序保存的父引用，不让模型抄写 hash/Receipt。

## 6. 下游 Stage1–3 的替换合同

不能把观察报告强行转换成 V4 facts/events/support 或把自然语言自动认证为事实。
新增语义输入投影从 committed report 提取原文/局部细节/模型解释/未知，程序附极短引用。
外部 Context Pack 只在下游作为背景，保留与观察不同的来源。

- Stage1：叙事理解提案，按报告内容组织发生顺序、人物对应假设、冲突、未解决问题；
  输出用预映射引用选择已有报告片段，文本表达关系，不制造事实/证据图。
- 程序：分配叙事单元身份、校验报告成员引用与来源；未知人物/关系保持未知，
  不靠合法类别填充或文本模型同意升级为 verified fact。
- Stage2：基于程序已编号的叙事单元提出故事意图及素材选择建议；Candidate 实例由程序编译，
  对无法可靠定位的素材标为待定位，不可声称物理可行。
- Stage3：按既有故事/素材短引用提出顺序、衔接、节奏、描述；程序创建蓝图草案，
  精确时间、素材覆盖证明和物理准入仍由后续证据与 compiler 完成。

每个文本生成操作拥有独立 request/attempt/receipt；同样不向模型提供长 hash、数据库 ID。
观察 reader 与下游投影必须可先验证，消费者尚未迁移时明确 unavailable，不偷走旧强校验路径。
新 Stage1–3 成功意味着受来源约束的语义提案完成；输出质量、物理 Recipe/Render/QC 分别验证。

### 6.1 新消费者的具体协议

为避免实现时继续沿用旧 V4，本次采用三种独立草案 schema；模型均不生成新 ID。
程序在每次调用前从精确已提交输入派生短 alias 目录，输出引用只能选择目录成员；
目录以外的正文描述保留，但不能伪装成受支持的素材引用。

| 草案 | 模型输出字段 | 程序编译 |
|---|---|---|
| observation narrative v1 | units: summary、report_refs（极短 alias 列表）、interpretations、uncertainties | 按原响应顺序分配叙事单元 ID；记录选中报告片段的程序来源与模型性质 |
| observation story v1 | stories: title、premise、unit_refs、editorial_intent、uncertainties | 分配故事与素材需求身份；引用未定位报告时保持待定位，不产生物理 Candidate 证明 |
| observation blueprint v1 | blueprints: story_ref、beats（material_ref、description、transition_note）、uncertainties | 以数组顺序生成草案单元；绑定已选故事与素材，不生成 source tick/Recipe/Admission |

所有语义槽位都是自由文本；没有 fact_kind、beat_kind、质量等级或模型分数。
`report_refs/unit_refs/story_ref/material_ref` 的 enum 只覆盖程序已映射对象。
模型不返回任意真实 ID、support、proof、coverage、hash 或调用身份。
输入文本本身即使含编号也不能改变 alias 目录。无支持的草案项标为未解决，不能靠查找同名对象补齐。
完整批次必须逐个覆盖其被请求的故事，但不能以强行编造内容满足覆盖；缺项明示未完成。
必要引用非法时草案项不晋升；原始观察与成功生成仍可读，且不会因此发起另一视频调用。

这些草案家族不会交给旧 CompileNarrative/Portfolio/Blueprint 的强类型 reader 冒充原版本；
必须有各自 versioned command/reader 与输入绑定。进入最终物理编辑前还需独立的材料定位与准入。
这是完整设计的消费者实现目标，不是仅新增几个内存 DTO 就宣称运行时链路已接通。

## 7. Runtime 和操作入口

使用新显式 plan/profile 注册 observation stage 与新消费者，沿用现有 outbox/runner/reconciler。
不修改历史 run.execution_profile；新 run 采用新计划。启动前检查 producer/consumer/profile 一致。
操作入口只接受已授权 source reference 或已提交 SourcePrep run+episode 选择，重建媒体出处。
不得在 HTTP body 允许任意 raw/proof/路径/alias-to-真实ID；受控选择目录由程序的已授权对象投影生成。
局部复看需要单独请求身份和精确 parent/outcome，status 分别报告 accepted/running/unknown/terminal。
读回、状态与 dry-run 不调用 provider；所有实际发起均有显式 execute/HTTP command 语义。

## 8. 回归与部署门槛

1. 纯合同：丰富中文、可见编号、解释/未知保留；不含业务 enum、ID 自造或举证 schema；
   alias null/unknown/scope/reorder，时间提示降级和严格根 JSON 拒绝。
2. wire：检查实际发往 provider 的 media/text/schema，证明无 V23 规则或私有映射/密钥泄漏。
3. durable：并发同 key 一次派发、unknown 恢复不再 create、raw/read/hash/attempt-chain、
   不同映射产生不同身份、历史 V4 bytes 与 reader 仍能重放。
4. reinspection：真实局部 bytes、同 key 幂等、unknown 不重复 create、父报告不变。
5. consumer：新报告进入新语义提案；歧义与不可定位状态传播，不能通过旧 fact/physical Admission。
6. 独立 PostgreSQL migration/restart/replay；业务库不运行 schema-reset pytest。
7. 单集真实观察+人工内容对照+新 Stage1–3，记录实际 provider/模型和运行 revision；
   通过后才扩大到全剧。测试成功、报告成功、语义成功和物理成功必须分别报告。

## 9. 实施分工与状态

实现代码由 gpt-5.6-terra 承担，主代理设计/集成/审查与记录。
先实现独立纯合同，再落持久化与实际 provider，随后接 runtime/下游与显式局部复看。
文件归属、依赖及当前结果由现有任务 `.ccg/tasks/rich-vlm-runtime-redesign` 记录。
未通过的任何一项都保留未完成，不能用局部绿色测试结束整个目标。

### 9.1 本轮实际实施检查点

实现由三个 gpt-5.6-terra 子代理承担；主代理完成集成测试、机械格式化与必要的类型/边界修正。
下面是本轮真实完成程度，不代表 §1–8 的完整目标已实现。

| 项目 | 当前证据与状态 |
|---|---|
| rich report / optional alias / decoder identity | 新纯合同与反例测试通过；观察与解释原文保留。当前代码仍含 canary `ObservationLimits` 条目上限，须按本节移为 generic decoder ceiling，未可激活 |
| SourcePrep -> observation Command -> PostgreSQL artifacts | 实际生成测试视频、SourcePrep、临时 PostgreSQL，成功提交与新实例重读通过；模型由计数假 Provider 代替 |
| unknown / replay / tampered source or writer | 同一个 provider identity 恢复、重复操作不再派发、来源/提交篡改拒绝通过 |
| Ark media cache + clean body | 真实 PostgreSQL cache + 测试 Files/Responses client；同媒体不同操作只上传一次，最终 body 无私有来源字段，thinking 明确 disabled |
| invalid JSON / unknown short ID | 前者终止且不盲目重生成；后者保留观察与私有原映射，通过真实数据库测试 |
| standalone composition/CLI | 新 `scripts/run_observation_request.py` 支持 dry-run/status/execute；status 纯读，未接新 HTTP plan |
| narrative input | 保留全部四类观察文本、程序短 alias 和执行层输入上限拒绝；仅纯输入投影，尚无持久化 narrative command |
| 局部复看 | 范围/派生媒体/显式操作链路仍待实现 |
| 整剧聚合与新 Stage1–3 | batch、消费者 Command/reader、Runtime profile/HTTP 接线仍待实现；story/blueprint 输入目前显式拒绝未实现路径 |
| 真实 VLM 质量与部署 | 未调用付费 VLM，未同步 PC 新运行时，未启动新整剧 Run |

验证：新合同、Provider/composition、消费者投影与既有 V12 定向回归 26 项通过；
独立 PostgreSQL 验收 9 项通过（另含后续集号与丢失远端 ID 不重复派发）；
11 个新生产模块的定向 BasedPyright 0 errors。
数据库是独立临时 `ac_autocut_verify`，业务库不受本轮测试影响。

独立外部分析 run `d75d088e-0cd6-485c-95e3-3e737353d6d7` 与代码审查均走既有 supervisor；
Claude CLI 实际路由为 GLM，不能记为 Codex+Claude 模型验证通过。测试通过也不补足该审查身份门槛。
审查 run `506de1e9-c90a-402c-b140-ab7c01e84ff1` 的 Codex leaf 要求修改；其指出的后续集号
误用单集窗口数问题已修正并增加数据库回归。关于添加 Provider 幂等 Header 的建议未采纳：
没有已验证的 endpoint 合同，丢失 response ID 时保持未知且不再 create，回归证明一次派发。
该 snapshot 后已有修正，最终双模型复审仍未通过；Claude leaf 本次超时且实际模型不符。
共享 postgres.py 全文件类型检查另有六条诊断落在未修改的既有函数区域，未以新模块检查冒充全仓通过。
当前任务保持进行中，不归档为完整实现，不进行生产默认切换。
