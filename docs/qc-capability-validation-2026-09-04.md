# QC 能力验收与续跑修复（2026-09-04）

分支：`feat/v213-contract-codegen`。这是开发验证记录，不是整剧 E2E 完成声明。

## 当前改动

- `ab4394be`：QC 能力接受命令、不可变记录及精确读取。
- `e426ed69`：成功重放重新检查来源绑定；中断的纯数据库命令可续跑；
  提交结果不明时不写失败 Receipt，下一次用原身份恢复。
- `3501646a`：回归断言使用 Store 现有的 ArtifactSet 冲突异常类型。

PC 使用 `tailscale-laiu` 上 Ubuntu-24.04 的独立验证 clone。
代码以 Git fast-forward 同步；GitHub fetch 超时时改用 Git bundle，未复制源码补丁。
测试只连接独立 PostgreSQL 16 的 `ac_autocut_verify`，没有触碰真实剧集数据库。
512 MiB 限制导致历史迁移的 PostgreSQL 后端 OOM；2 GiB 后基线 32 项通过。
最终 `3501646a` 在 PC 上 54 项通过（含 16 项真实 PostgreSQL 测试），Ruff 与
命令模块 BasedPyright 通过；JUnit：PC `/tmp/autocut-qc-3501646a-postgres.xml`。

Codex 独立审查通过两项重放修复；Claude 超时，不能宣称双模型审查通过。
该检查点尚未补齐 SQL 的 accepted-row / validator / Receipt / 两成员集合约束；
后续 0060 修复与验证见下文。

## 尚未完成

当前 HTTP registry 尚未接入 Stage 4 / Render / QC；QC runner 尚未消费持久化能力，
Stage 5 完整性事实及本地 release 尚未闭合。不能将上述数据库测试称为真实剪辑 E2E。
0060 验证后应接通后半程，用已有兼容的 VLM/ASR 结果续跑真实样本。
本轮没有调用 VLM/ASR，没有修改 prompt，没有进行外部发布。

## PC FFprobe 兼容修复与 SQL 闭合后续

- `61384849`：修复 PC FFprobe 6.1.1 不输出 `stream_groups` 而被解析器拒绝的问题。
  仅接受实际验证的三键/四键根对象；核心段、未知键、重复键及非空附加段检查保留。
  没有把缺少 stream-group 段解释成“已检测且不存在”。
- PC 原有真实媒体工具测试 18 项中 6 项失败；修复后连同解析器边界回归
  **43 项通过**，Ruff 通过。JUnit：PC `/tmp/autocut-qc-ffprobe-61384849.xml`。
- `d99e0086`：新增 0060 迁移，独立复算 accepted row、validator Job、命令、
  Receipt、顺序固定的两成员集合、payload/hash 和来源绑定；拒绝不一致的历史，
  不重写 0059，不取消原有不可变约束。PC 上 **67 项真实 PostgreSQL 测试通过**，
  包括完整 0059 历史回放与迁移失败回滚；用时 524.69 秒。
  JUnit：PC `/tmp/autocut-qc-closure-d99e0086.xml`。
- `418545fa`、`41a70d92`：共享 pytest fixture 显式重导出并按 Ruff 规则排序；
  PC 目标 Ruff 与 collector 模块 BasedPyright 已通过；45 项 PostgreSQL 用例
  再次通过（6 项未改动的历史回放未重复执行）。JUnit：
  PC `/tmp/autocut-qc-closure-41a70d92.xml`。
- `d99e0086` 的相邻模型/静态迁移/输入/求值器测试 **38 项通过**。
  JUnit：PC `/tmp/autocut-qc-adjacent-d99e0086.xml`。
- 两次独立 Codex 审查均未发现需修复问题；SQL 审查仅覆盖提供的 patch，
  未独立核对完整旧迁移。Claude 两次超时，不能声明双模型审查通过。

PC 真实工具探测已匹配已安装静态资源，request hash 为
`sha256:88950e4c546bea776f308d898be8d9bab9d857e3ac305544a955b04aca691a05`。
初次探测仅为 `measured_not_accepted`。随后在 `41a70d92` 上使用正式 Command/Store
代码完成 **真实工具探测 → 独立验证库接受 → 精确读取 → 同身份重放**，结果成功：

- 数据库：独立 `ac_autocut_verify`（不是剧集运行库）。
- Receipt：`01b58a7e-55d0-4b76-9679-5e5abc572f25`。
- ArtifactSet：`4e2af94d-c6bd-40d5-bd8b-3a62acf247c7`。
- Command slot：`880c5f4e-c4fe-4f31-b7df-b2e4acb87d98`。
- 重放保留同一 Receipt、set、slot，读取的 request 与真实探测 request 完全一致。

此验证没有伪造工具身份，但也不代表 deployed named-role 权限验收、生产库部署、
真实剧集 E2E、完整 Stage 5 或本地成片交付已完成。

下一处运行接线必须区分两个已有哈希：0059 的 `qc_runner_identity_sha256`
存放完整 LiveProfile hash；runner 自身同名属性是工具、环境与 registry 的紧凑
身份。应从 Store 精确读取的同一份 accepted capability 显式派生 runner profile
及 evaluator identity，不应直接将这两个不同投影的哈希判为相等或改写历史值。
