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

启动器读取本机私有凭据并执行正常CLI，不是可移植配置模板。Mac端口18767；
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
