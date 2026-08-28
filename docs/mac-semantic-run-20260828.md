# Mac 单集真实运行记录 — 2026-08-28

## 已执行

- 分支：`feat/v213-contract-codegen`。正常 `pipeline-serve` HTTP，非 Agent。
- 素材：book42000021919 第1集，约241.344秒；单独授权的1集数据集，
  **不代表全50集完成**。原始50集文件保留。
- 真实 Podman PostgreSQL `autocut`；原迁移到0012，备份并按序应用0013～0028。
  三条原终态 Pipeline run 的完整行校验和升级前后不变。
- `run_id=pipeline_run_1af0f6ea9de849e5ad4ecda470de300a`。
- SourcePrep成功：Receipt `620e9ac1-0717-4757-a161-086bf9d1373e`。
- Doubao `doubao-seed-2-1-pro-260628`，Ark SDK流式；只执行一次生成Attempt。
  response `resp_021787855875824aad108ac0e3cfe9dca6aef37a454448d1e27ed`。
- VLM失败：`PROVIDER_RESPONSE_INCOMPLETE`，provider原因`length`；失败Receipt
  `f9c0222e-556a-47a0-a707-e08549dee243`。未启动ASR、故事、剪辑或发布。

## 原始输出与原因边界

Provider实际返回了不完整正文（约33095字符），不是只有推理没有正文。
空白占约40.6%，完整SHA256引用89次；JSON尚未闭合。只读获取同一response
补回被旧debug脱敏隐藏的用量：input34175/output32768，其中reasoning19560，
total66943。已达到当前32768输出上限。

这些证据支持优化输出表达及检查推理预算，但不证明“仅缩短ID”或“关闭推理”
必然解决。不得把不完整JSON修成成功或原样无限调用；下一次变更应使用新策略/新run。

## 本机文件（私有，不进Git）

- 启动器：`/Users/liuzx/Downloads/ac-auto-cut-validation/mac-local-run/launch.py`
- 原始阶段debug：`/Users/liuzx/Downloads/ac-auto-cut-validation/mac-local-run/debug/<run_id>/`
- 只读获取补充debug：`/Users/liuzx/Downloads/ac-auto-cut-validation/mac-local-run/retrieved-debug/`
- 数据库升级前备份：`/Users/liuzx/Downloads/ac-auto-cut-validation/mac-local-run/backups/`

启动器读取本机私有凭据并执行正常CLI，不是可移植配置模板。Mac默认端口18769；
API token、Ark凭据和数据库密码不可复制到文档/Git。开发测试另用可丢弃数据库。

## 尚未完成

单集VLM成功、全流程ASR/故事/成片、选择性重算HTTP、跨Job复用、实际PC/Mac
交接均未完成。非终态resume修复与兼容身份基础已通过测试；不等于这些功能上线。
SSH按用户要求暂不使用。后续先修正真实VLM输出问题，再验证单集；重算设计见
[选择性重算](pipeline-selective-recompute-design.md)。

## 已验证的代码修正

- `1c992098`：非终态VLM resume、精确来源兼容身份；真实PostgreSQL测试116通过。
- `58654532`：debug保留明确非负整数token计数，凭证与未知嵌套字段仍脱敏。
- 新的v4紧凑prompt显式注册；v3旧模板、完整请求和profile固定哈希回归通过。
  v3与v4 profile均通过真实数据库只读SQL形状检查，无需改数据库迁移。
- 原run相同幂等键重新提交仍返回原failed run；GenerationAttempt数量仍为1。

## 第二次真实调用：仅紧凑输出仍失败

`pipeline_run_18ac0863c1894ac5ae3c0eebb0804620` 使用compact prompt与同一已上传视频，
SourcePrep Receipt为`b512a49e-a792-4168-9ad5-9e447903787c`。
VLM单次Attempt仍以`length`结束，失败Receipt为
`c8d52444-4a36-4671-80b1-652556937458`。
input34413/output32768/reasoning26780/total67181；正文10973字符、单行。
因此未把“紧凑输出”当作已解决根因，也未增加预算或自动重试不完整JSON。

新adapter v5把`thinking_type`明确纳入请求与profile，当前semantic-only选择disabled。
先通过独立审查、旧哈希回归与临时数据库迁移测试，再启动新的单集run；
旧失败run不会更改profile或重新标记为成功。

## 第三次真实调用：完整JSON，但时间证据被拒绝

代码`70d90288`，run `pipeline_run_00cce9541d5546638ea69f3a9f8f86b9`。
SourcePrep成功Receipt：`98fc6a4c-4ddc-48b5-a557-42a6272046d5`。
Provider返回completed，完整原文39142bytes，input34412/output16808/reasoning0，
total51220。已实际确认thinkingdisabled与上传文件缓存复用生效；不再length。

但Kernel拒绝`OUT_OF_BOUNDS_INTERVAL`，Receipt
`fdd59f37-a52f-45f6-b5ed-862248277ba8`。79处support中34处区间非法，
60处没有引用帧落在声明区间内（两类可以重叠）。模型给出的最大时间超过735秒，
实际代理只有241.32秒；还有起止倒置。未裁剪、补写或重新标记这些结果。

完整原始输入/输出：上文debug根目录加本run的
`vlm/model/doubao-ark-responses-stream/vlm_semantic_evidence-188407afe91bcb104f97/`。
`raw-output.bin` 是完整UTF-8 JSON；`terminal.json` 保存真实completed与用量。
这证明预算问题本次已解决，不证明VLM阶段成功或语义内容已验收。

当前还需修正模型时间表达与稀疏帧证据之间的接口设计；下一次调用前先离线验证。
恢复回归另有5项真实PostgreSQL测试通过，覆盖不可达旧Windows路径时读取持久化
Blob/精确请求及缺claim/bytes拒绝，不等于真正PC/Mac迁移验证。

## 第四次真实调用：V7 ContextPack 已持久化，发现 Schema/Prompt 绑定缺陷

`pipeline_run_051c83ec84de4c30a90b2f7529301c2d` 通过本机正常 HTTP
`pipeline-serve` 提交真实第1集。该运行没有配置外部剧情 API；`context_prepare`
仍成功提交了不可变 `video_only` `WindowContextPack`（原因
`EXTERNAL_CONTEXT_NOT_CONFIGURED`），随后真实 Ark 流式调用完成。由此已验证：
本机首次运行不需要外部 API 才能进入 VLM，已提交 Pack 也可成为重跑/换机读取的输入。

该 VLM Attempt 没有被伪装为成功。完整响应被 Kernel 拒绝，原因是 V7 指令要求
`video_observation`（不含 frame/PTS 引用），但请求工厂错误地为 V7 选择了旧的
frame-anchor Schema；模型因此合法地产生了该分支，而 parser 正确拒绝其不满足
毫秒区间的帧引用。该问题是 Schema 与 prompt 的绑定错误，不是 API、数据库或模型服务
故障。修复为：V7 与 V6 共用仅视频观察的 Schema，且更新受保护 authority digest。

同次修复还将“完成但不合约的结构化 VLM 响应”接入已有的三次 Generation retry
预算：每次重试拥有新的 provider idempotency key，保留同一不可变输入和失败的结构化
原因；耗尽才写终态失败 Receipt。不会修改或掩盖本次被拒绝的历史 run。

## V8 后续运行策略：核心观察与候选生成分离

该 run 的三个真实 Attempt 均已保留。前两次模型已成功返回主语义图，但分别在
`candidate_hypotheses` 的 `tags` canonical 顺序和未注册 `narrative_functions` 上失败；
第三次不是严格 JSON。它们证明候选的编辑性枚举会拖累核心观察采集，而不证明视频事实
本身不可用。

因此新增 immutable prompt/schema `v8-context-assisted-core-observations`：仍输出实体、
事实、事件、连续性和摘要，但 Schema 强制 `candidate_hypotheses=[]`。它不是“丢弃高光”，
而是把高光/钩子假设交给后续只读取已验证语义图的候选阶段。V8 是新 profile、请求 hash
和新 run；V7 及所有历史 Receipt 均不修改。迁移 `0033` 已在本机 PostgreSQL 应用，升级
前后 pipeline run/command 的计数和校验值相同。

## V9 后续运行策略：保留严格校验，强化时间线自检

V8 的三个真实 Attempt 均完成并留下原始输出，但分别因跨时段 `event.fact_refs`、未知引用、
再次跨时段引用被拒绝。拒绝是正确的：模型不能把同一人物在远处时间发生的事实接到当前事件，
也不能凭空引用未声明的事实。没有通过删除这些引用、裁剪结果或放松 Kernel 校验来“修复”。

V9 `v9-context-assisted-timeline-core-observations` 保持 V8 的核心观察 Schema、
`candidate_hypotheses=[]` 与所有 parser 不变量，仅增加明确的生成顺序：先确定事件区间，
再只写入和该区间严格相交的事实；跨不连续动作拆为多个事件，无法满足时只保留事实不输出事件。
它有独立 Prompt/Profile/Request hash 和迁移 `0034`；V8 run、失败原因与 Receipt 均不改写。

## V10 后续运行策略：对抗 Ark 的枚举漂移

V9 的前两次真实输出仍把 `visible_reaction` 作为 `fact_kind`。这不是已注册的值，且 Ark 在
本次实际调用中没有完全执行它已收到的 JSON Schema enum；Kernel 继续以
`UNKNOWN_ENUM_VALUE` 拒绝。V10 因而在保持完全相同的 Schema 和 parser 的前提下，向模型
显式列出所有 entity/fact/event/time 枚举，并说明表情类反应必须归入 `visible_action` 或
`visible_state`。这是新的不可变 Prompt/Profile/Request hash 与迁移 `0035`，不是把未知值
自动映射为合法值。

## V11 后续运行策略：一次性约束紧凑且规范的 JSON

V10 的真实返回还显示 Schema 可能被部分忽略：`object_ref:p012` 缺少 JSON 字符串引号，
因而不能解析。V11 仍不宽容解析，而是合并此前全部模型侧约束，并限制每窗口的核心观察
规模（12实体、18事实、10事件、4时间段）。它显式要求所有本地 ID 与引用是带双引号的
JSON 字符串、必填字段恰好一次、空值为 `null`、引用数组排序且不重复。该紧凑限制仅影响
新 V11 请求；之后可用更多小窗口补充覆盖，不能拿未验证的大 JSON 冒充完整语义图。

## V12 真实运行：单集 Source → Context → VLM 已成功持久化

`pipeline_run_11815ba2eac4489f8cd40066e361764a` 是本机正常 HTTP
`pipeline-serve` 的真实单集运行。SourcePrep、`video_only` ContextPack 与 VLM 均已写入
不可变 Artifact/Receipt，最终 run 状态为 `succeeded`。Ark 只生成了一次；随后 HTTP
resume 仅重读已提交的 GenerationAttempt 并完成本地聚合，没有产生第二次 provider 调用。

本次先前出现的 `indeterminate` 不是模型失败：VLM 子调用已经提交，但 V4 聚合验证器仍将
含 `context_pack` 与其 hash 的 V7+ 冻结请求当作旧 V4 的非法字段集。修复后，验证器只接受
两种精确闭合形状（旧请求，或旧请求加完整 WindowContextPack 和匹配 hash），并将 ContextPack
hash 纳入重算后的请求身份；不接受缺失、伪造或不匹配的 Pack。专门的真实 PostgreSQL
重启/聚合测试覆盖此路径。

V12 `vlm-semantic-pack-v12-context-assisted-reciprocal-causal-core` 在 V11 的紧凑、时间线和
封闭词表规则上增加事件因果边的双向引用要求，解决了此前“因果只写一端”的真实拒绝。
它仍是新 Prompt/Profile/Request hash，未修改任何历史失败结果。原始 request、terminal
状态和响应只保存在私有 debug 目录的该 run 下；默认 HTTP 端口为 `18769`，避开本机已有
服务的 `18767` 和 `18768` 冲突。
