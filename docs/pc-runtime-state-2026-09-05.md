# PC 运行状态与真实续跑入口（2026-09-05）

这是一份现场核对记录，不是整剧 E2E 完成声明。

## 连接和数据

- SSH：`tailscale-laiu`，进入 `Ubuntu-24.04`，用户 `laiu`。
- 本轮恢复启动原容器 `autocut-postgres-v213`，回环端口 `5433`。
  原卷 `autocut-postgres-data-v213` 保留，没有重建或清空。
- 原业务库 `autocut` 已通过只读连接核对；私有连接配置仍在
  `/mnt/d/code/auto_cut/private-config-import-20260826/autocut-v213-postgres.env`。
  不把配置内容或 DSN 放进 Git。
- 独立验证容器 `autocut-qc-ab4394be-validation`、端口 `55459`、
  数据库 `ac_autocut_verify` 与业务库完全分开。会重建 schema 的 pytest
  **只能连接验证库**，不得对 `autocut` 执行。

## 已有真实产物

业务库查到 137 份 `vlm_semantic_pack`，各有对应 request/response record，
以及一份 `vlm_semantic_pack_set`。137 是历史产物数量，不是137集。

唯一 `succeeded` 的 HTTP run：
`pipeline_run_cc2196abcdef4645a7fa587c843d0d1a`。

- profile：`shadow`。
- 成功阶段：`source_prep → context_prepare → vlm`。
- 已提交批次声明1集、包含1个child；不是全剧成功。
- prompt：`vlm-semantic-pack-v22-context-assisted-minimal-core-observations`。
- parser：`strict-semantic-pack-v4`，batch schema为`4`。
- model：`doubao-seed-2-1-pro-260628`；Ark streaming adapter v5。

当前库未查到 Stage1–3、精确剪辑或媒体证据对应的 artifact。
其余21个 HTTP run 为 failed；不能把 Kernel Job 的 running 状态当成进程仍在执行。

## 代码与启动边界

- 开发分支：`feat/v213-contract-codegen`。
- PC 验证 clone：`/home/laiu/auto_cut_bot-v213-validation`，用于同步与测试。
- 原运行 worktree：`/home/laiu/auto_cut_bot-v213-recompute-wsl`，核对时仍为
  `7649e3d8`，没有被覆盖。不要直接运行旧私有 launcher 并假定它加载最新 Kernel。
- 核对时 WSL 的18769、18770、18771、8765端口未监听；FunASR容器停止。
  本轮只启动原数据库，没有启动旧 Pipeline 或调用模型。

真实续跑前先校验上述单集 V22 产物与目标阶段输入的兼容性，并明确终态 run
如何绑定到新阶段/新运行。不能原地改写成功 run 的冻结计划，也不能仅因版本号
不同就重跑全剧。优先完成后半程接线和兼容结果复用，再对缺失阶段执行真实任务。

QC 工具和数据库验证记录见
[QC能力验证](qc-capability-validation-2026-09-04.md)。数据库验收、媒体fixture成功
都不能替代上述单集通过真实Stage4、Render、QC并产出本地视频。
