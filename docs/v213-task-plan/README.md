# v2.1.3 当前设计任务快照

更新日期：2026-08-26。

这里是当前新架构实现任务的 Git 版本快照，供台式机上的 AI 或开发者直接读取。它不是
旧 `ac_auto_cut` 的任务，也不是可以绕过 authority/Registry/Admission 的实现许可。

## 使用范围

- 代码仓库：`auto_cut_bot`；目标分支：`feat/v213-contract-codegen`。
- 共享 Kernel、Pipeline Runtime、Agent-Native Runtime 必须遵守当前仓库代码和这些任务
  文档的边界。
- 每个任务目录只保留 `task.json`、`prd.md`、`design.md`、`implement.md`、
  `implement.jsonl`、`check.jsonl` 等主计划文件；历史 `research/` 不复制，避免把旧
  结论误当作当前实现依据。
- 任务状态仍以工作区全局 `.trellis/tasks/` 为调度真源；本目录是可提交、可回滚、可在
  台式机读取的设计快照。修改任务后必须同步更新两处，并在任务入口重新确认 hash。

## 当前主线

### 最新代码状态（2026-08-26）

安装包资源准备、固定配置读取和 HTTP 启动接线已实现；启动先核对已接受的校准记录
及完整配置，再恢复任务。VLM/ASR/VAD 的实际运行参数和恢复任务保存的参数均须匹配，
不通过时不调用模型，也不改写旧任务。共享包与主项目两种安装方式均已做隔离测试。

当前 HTTP 注册的仍是 `source_prep → vlm → media_preflight`，不代表完整
Stage 1–4 / Render / QC 已接通。没有安装真实校准配置，也没有完成整剧真实运行。
本机只负责开发、自动化测试和审查；模型校准及完整实际运行留在远程台式机。
下列较早检查点的“待接线”是历史状态，以本段和当前任务最新章节为准。

```text
authority implementation
  → contract codegen / Artifact Store
  → command admission & recovery
  → media preflight + calibration
  → Stage 4 exact A/V vertical slice
  → Stage 1–3 semantic chain
  → Render / Publication QC
  → dual-runtime conformance
  → platform publication certification
  → migration cutover
```

`08-25-lock-real-test-authority-profiles` 是当前真实运行的前置任务。Shadow/local-run
Profile 的源语法和 decoder 已实现；它们仍是 unresolved grammar，不是运行许可。
`08-21-05-media-preflight-calibration` 已补齐测量 v3、CalibrationRecord、数据库原子
写入、独立验证命令和 local-run 可直接消费的记录读取接口。相关合并测试为
324 passed、零 skipped（包括显式启用的数据库用例）；
详见该任务的 [分块审查记录](08-21-05-media-preflight-calibration/review-calibration-phase2-slices.md)。
真实 raw 校准 HTTP 适配器现已实现，含原 source owner/BlobRef 校验、流式传输、
禁用环境代理/重定向，以及不重复调用的回放测试。本轮相关回归 242 passed、零 skipped，
并通过独立审查。实际 HTTP 服务往返已测，但模型输出是 fixture，不是真实 native 校准。
committed-source/独立标注输入装配现已完成：精确核对来源引用与原始字节，
使用已提交音频时钟，并验证独立标注的锁定哈希；服务配置生成器也已提交。
测量→成功结果引用读取→独立验证的部署侧执行函数已实现，未新增 HTTP/CLI
发布入口。当前合并回归 **440 passed、零 skipped**，包含真实隔离 PostgreSQL
事务测试；模型输出仍为 fixture，不是实际 SenseVoice/FSMN 校准。
尚须完成真实独立标注/模型校准、权威源/lock 部署、打包配置加载、
运行时接线和真实 HTTP 单集运行。当前 typed 参数注入不等于部署权威加载已完成。

Git 锁定 Registry 编译与 shadow 配置加载已实现：只读取明确提交的 Git 字节，
核对 A/B/C、完整文件覆盖和 schema/profile 来源，不读取工作区配置。
相关合并回归 **296 passed、零 skipped**，两模块均通过独立只读审查。
详见 [来源加载审查](08-25-lock-real-test-authority-profiles/review-locked-source-context.md)。
local-run 前驱来源及 accepted anchor 绑定函数也已实现并通过独立审查：
**69 个新增测试通过**，相关合并回归 **128 passed、零 skipped**。
详见 [正式配置接线审查](08-25-lock-real-test-authority-profiles/review-local-run-binding.md)。
这仍不代表真实校准或正式运行配置已加载。完整通用八包 Registry 仍未完成，
但不再是本地运行的前置；当前本地来源使用独立身份，不冒充通用 Registry readiness。
还须补齐真实来源、安装资源打包和 HTTP 接线。
timed-speech Registry 契约哈希已改为从锁定 Schema 的可达定义闭包推导，
拒绝错用整个 Profile/Registry 哈希及过期定义。新增投影测试 96 项通过，
相关来源/绑定回归 115 项通过、零 skipped；独立审查无缺陷。
详见 [契约哈希绑定审查](08-25-lock-real-test-authority-profiles/review-timed-speech-contract-binding.md)。

2026-08-26 已修正本地加载工具误依赖旧 19-command 通用 Registry 的问题，
并在 Task 01 和当前任务中同步范围：不恢复用户已删除的总契约。
本地三来源编译、受审校准核验后的资源字节生成、固定安装资源读取现已实现；
校准比较归一到 Kernel，未修改数据库。读取器尚未接入 HTTP 启动，
真实资源未安装；双 wheel 打包、provider/policy 匹配及启动前两类 anchor 核验仍待完成。
相关联合回归 272 项通过；来源/资源补充回归 109 项通过，最终冻结后来源与校准回归
116 项通过（包含重复覆盖，不累加）；独立审查通过，详见
[本地配置编译与资源传输审查](08-25-lock-real-test-authority-profiles/review-local-profile-resource.md)。

当前优先级是继续开发；完整真实运行在远程台式机完成。测试产物、typed Profile 或
旧 admission 文件不能代替上述未完成项，也不能据此启用外部发布。

`08-26-real-e2e-multi-agent-closure` 是真实一集到全剧验收的集成计划；它不能替代各个
阶段的实现，也不能把 HTTP 503 或 fixture 测试称为真实 Pipeline 成功。

## AI 接手规则

1. 先读取本目录对应任务的 `prd.md`、`design.md`、`implement.md`。
2. 再读取当前代码和 `.trellis/spec/`；若两者冲突，停止并报告，不自行选择旧实现。
3. 一个 Agent 只拥有一个明确文件集合；共享导出、Runtime composition、migration 和
   authority lock 必须指定唯一 integration owner。
4. 先完成最小实现、测试和独立审查，再进入下一个任务；失败要保留 Receipt/诊断，不得
   静默跳过或用默认值伪造成功。
5. 不得导入 `ac_auto_cut` legacy 代码，不得让 Runtime 直接写 Store/Admission，也不得
   从普通环境变量伪造 authority snapshot。

## 当前不代表已完成

这些文件是计划和验收依据，不表示 Stage 1–4、Render/QC 或真实整剧 HTTP Pipeline 已经
跑通。真实状态以代码、测试结果、数据库 Receipt 和对应任务提交共同确认。
