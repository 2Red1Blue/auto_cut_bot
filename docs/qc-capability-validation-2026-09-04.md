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
SQL 的 accepted-row / validator / Receipt / 两成员 ArtifactSet 延迟闭合约束仍待补齐。

## 尚未完成

当前 HTTP registry 尚未接入 Stage 4 / Render / QC；QC runner 尚未消费持久化能力，
Stage 5 完整性事实及本地 release 尚未闭合。不能将上述数据库测试称为真实剪辑 E2E。
下一步修复 SQL 闭合，再接通后半程，用已有兼容的 VLM/ASR 结果续跑真实样本。
本轮没有调用 VLM/ASR，没有修改 prompt，没有进行外部发布。
