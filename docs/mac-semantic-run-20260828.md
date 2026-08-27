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

v4代码/单测通过不代表真实生成成功；真实第二次调用结果需另行追加，不覆盖上述失败。
