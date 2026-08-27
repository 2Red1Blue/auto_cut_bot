# VLM 重算共享内核实现

用户将总目标更新为 Mac 本地单集真实运行、不同平台结果兼容及重跑/续跑（SSH 暂不可用）。本任务实现后续持久化重算
入口使用的兼容身份基础，并修复现有非终态续跑。选择规划与重算 HTTP 另行实现，
本切片不冒充已支持跨 Job 复用、终态重算或跨物理平台验真。

验收：
- 实际有效语义输入变更改变身份；无关运行配置不污染身份；旧完整 request/profile 不改。
- 始终保留原 producer Job/Receipt/Set/source provenance，不把新 run 标识套给旧结果。
- 封闭字段/合法hash/可逆序列化和负例测试，Kernel不import Runtime/legacy。
- 无新增provider调用、数据库迁移或付费执行入口；持久化授权和HTTP接入是下一项，不能绕过。
- 修复已有 `/resume` 对 semantic_only 非终态错误拒绝：只唤醒 pending/indeterminate，
  不改 frozen profile/命令，不重开终态。awaiting_calibration 仍只处理 media_preflight。

未完成范围继续由 selective recompute 设计追踪：选择集规划、持久化绑定/预算/hold、
跨 Job reader/finalizer、重算 HTTP、完整覆盖与扩展确认。不得将本切片标记为这些能力完成。

设计依据：docs/pipeline-selective-recompute-design.md（尤其 §3～6、RC-01～10、16、21～22）。
旧全局 Trellis Bootstrap 的已删除总契约/提交许可约束不恢复；遵守用户后来批准的精简流程。
本地真实运行作为单独操作记录保存；使用已有 autocut 库，DB 测试只能指向新建可丢弃库。
