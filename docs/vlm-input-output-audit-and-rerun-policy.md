# VLM 输入输出审计与增量重算策略

## 结论

本轮审查没有启动新的 VLM 调用。此前为验证代码而创建的新 run 会按 run-scoped
command key 重新调度语义阶段，因此会造成全量重算；这不是模型输入发生变化，而是
当前跨 run 复用尚未接入生产调度器。后续诊断不得再用新建全量 run 验证。

## 真实调用的成本构成

PC 已保存的真实 debug 表明：固定文本 prompt 约 2.8K 字符、response schema 约
13K 字符；一次窗口曾返回约 48 个 facts、8 个实体和 12 个事件，provider 记录的
输入/输出/总 token 约为 34.6K/28.9K/63.6K。输出 JSON 的引用数组和视频本身是
主要成本，长 ID 不是主要成本来源。因此优化优先级是减少无效窗口/重复调用和输出
冗余，而不是只缩短 ID。

## 身份边界

`VlmSemanticReuseIdentity` 只包含模型可见且影响语义的值：源媒体与时间线、窗口
manifest、WindowContextPack、完整渲染 prompt/template、response schema、模型与
provider、采样/请求参数、parser contract 和请求 payload。以下变化不得使 VLM
结果失效：日志、debug 路径、端口、worker 并发、进程重启和 transport 重试。

同一 run 的 durable succeeded child 可直接重放。跨 run 不能读取别的 Job 的 Blob
冒充本次结果；必须由 Kernel 校验 origin 的完整 identity，并在目标 run 写入自己的
不可变 `Receipt`/`ArtifactSet` 投影。找不到唯一匹配、旧数据缺 identity 或绑定校验
失败时，只生成 `reuse_unavailable(reason)`，不得静默扩展成全量 provider 调用。

## Prompt 版本兼容

V19、V20、V21、V22 是四个不同的模型可见模板，历史版本的 continuity 和枚举约束
并不相同。恢复历史请求必须按其 `prompt_version` 解析部署前的精确 UTF-8 字节；
只有 V22 可作为新请求默认版本。模板哈希回归测试是启动/恢复门禁，禁止把旧版本别名
到当前模板。

## 后续实现顺序

1. 先实现逐集 `reuse/execute` 计划并持久化 plan hash，默认只执行明确列出的缺口集。
2. 增加与 source binding 对称的 VLM reuse binding command；目标 run 仍生成自己的
   Receipt/ArtifactSet，finalizer 重新闭合整批。
3. 为 prompt、schema、source、context、model/provider、parser policy 各维度增加
   不相等矩阵测试；运行器变化测试必须证明零 provider 调用。
4. 只有上述实现和测试完成后，才允许单集 selected-only 真实验证；不得用 50 集新 run
   作为回归测试。

## 本轮双模型审查结论

Claude Code 发现并已修复 V19/V20 历史模板别名问题；同时指出跨 run 语义结果目前
尚未接入生产调度、Store 重建失败可能形成 poison loop、Ark `failed` 媒体缓存需要
区分可重试故障与终态拒绝。Codex 独立确认核心 request identity 覆盖完整，并提醒
未来 reuse binding 必须先做 intent reservation，避免重复绑定产生孤儿对象；`raw-output.bin`
需要权限、大小和保留策略。上述未实现项保持为后续 Kernel/Runtime slice，不通过临时
补丁绕过权威 Receipt/ArtifactSet。
