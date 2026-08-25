# v2.1.3 Authority Implementation

## Goal

以五份 v2.1.3 权威契约为唯一实现和上线验收依据，在 `auto_cut_bot` 中建立与旧实现物理隔离的共享 authority kernel，由 `ac_auto_cut` 交付 Pipeline Unattended Runtime，由基于上游 nanobot 的 `auto_cut_bot` 交付 Agent-Native Runtime；任何未获得独立发布许可、任何外部整批结果不确定或任何旧能力未准入的路径都必须 fail-closed。

## Background

- 权威设计位于 `ac_auto_cut/原理/v2.1-production-spec/` 与 `ac_auto_cut/原理/v2.1-implementation-design/`。
- 当前 `autocut_core`、plugins、旧 ArtifactBus/file-state 和 `ac_auto_cut/.trellis/` 只能作为待甄别历史实现，不能反向定义契约。
- 实现设计已统一使用 `autocut_kernel/` 与显式 bridge/composition packages，但尚未有机器强制的包骨架和 import firewall。
- 根 `.trellis/spec/backend/` 已替换为临时 Authority Bootstrap block，但它不在 `ac_auto_cut` Git 仓库中，尚不是受版本保护的单一真源。
- 受保护 Git/CI 属于 push/release 加固，不再作为隔离业务实现的前置条件；未配置时 local Supervisor 可以推进 task，但 push/release 保持 deny。
- 已创建 private non-fork 空仓库 `2Red1Blue/ac_auto_cut`，并绑定本地 `ac_auto_cut` 的 `origin`；未进行任何 push。
- `auto_cut_bot` 是第二个项目：`origin=2Red1Blue/auto_cut_bot`、`upstream=HKUDS/nanobot`；Agent 基座继续跟随 upstream nanobot，共享包归属该仓库。
- 用户已在 2026-08-21 撤销“保留旧任务历史”的要求，允许清理旧任务和已被 v2.1.3 替代的无用文档；清理必须先使用明确 allowlist 保住本 parent tree 与两组权威设计。
- 清理已执行：active tasks 仅保留 08-21 authority tree；仓库旧 `.trellis` 已移出；旧原理/架构文档已合并为一份兼容原理说明并从当前分支删除；根 backend spec 已替换为临时 Authority Bootstrap block。

## Requirements

### R1. 权威与包隔离

- 新共享包的 source owner 固定为 `auto_cut_bot/packages/autocut-kernel/`，import package 命名为 `autocut_kernel`；不得在现有 `auto_cut_bot/packages/autocut-core` 或 `ac_auto_cut/autocut_core` 内渐进替换。
- `autocut_kernel` 不得 import 任何 legacy package 或 `autocut_legacy_bridge`；组合根只能位于 Runtime/infrastructure 层。
- kernel 的独立 wheel/import smoke 环境不得安装 legacy distribution，保证物理不可达。
- Pipeline 与 Agent 只能调用同一 Command Dispatcher/Domain/Application Core，均无 Store/Admission/Publication 私有写路径。
- `auto_cut_bot` 中的 nanobot agent loop 只是 Agent-Native Runtime/adapter，不是 authority kernel；upstream sync 不得更改契约、Admission 或发布决策。
- `ac_auto_cut` 禁止 vendor/copy kernel source 或使用未锁定 editable path dependency；必须绑定 `auto_cut_bot` exact commit、kernel version 和 wheel/content hash。

### R2. Import Firewall 是 Phase 0 前置开关

- 在任何业务 Command/Stage 实现前，CI 必须运行 AST import firewall、包依赖图检查和 isolated-wheel import smoke。
- kernel 禁止 `sys.path` 修改、越界相对 import、`importlib`/`__import__`/loader 动态加载以及对 legacy path 的字符串装载。
- import firewall 自身必须有 good/base/bad fixtures，并证明改名、别名、from-import、动态 import 和传递依赖不能绕过。

### R3. Reuse Ledger 是事前准入门禁

- 每个被扫描到或被引用的旧模块必须先有机器可验证 ledger 条目，disposition 只能为 `banned|fixture_only|algorithm_candidate|approved_adapter|migrated`。
- `autocut_kernel` 对 legacy import 永远失败，不因 ledger disposition 放宽。
- 只有 `autocut_legacy_bridge` 可 import `approved_adapter`；`fixture_only` 只允许测试/fixture tool；`algorithm_candidate` 只允许离线评估；`migrated` 表示改用新路径，旧路径仍禁止。
- ledger 变更必须绑定 owner、源 hash、允许 importer/symbol、I/O contract hash、副作用/确定性判定、测试和复审条件，并先于对应 import 合入。

### R4. Legacy Bridge 输出受限

- Bridge 只能返回 generated immutable DTO、primitive collection 或受验证 `ImmutableBlobRef`。
- Bridge 禁止返回/泄漏旧 ArtifactBus、Stage、Policy、Admission、Recipe、数据库 entity、可变对象或“pass/ready/allow”权威判断。
- Bridge 只产生 observation/candidate；新 kernel 必须按 v2.1.3 Schema、Policy 和 Admission 独立复算。
- Bridge 自身及其每个 legacy import 都进入 Reuse Ledger 和 conformance test。

### R5. Trellis Authority Bootstrap

- Phase -1 同步 backend code-spec：PostgreSQL `autocut_authority`、ArtifactSet/CAS、Command/Receipt、Admission/Recovery、无隐藏默认、双 Runtime、import firewall 和 Reuse Ledger。
- 所有 08-21 之前的旧 Trellis task 在确认不属于 allowlist 后移出 active task namespace；无需继续保留 `08-17-production-layer-v2-layer1`。
- `ac_auto_cut/.trellis/` 的旧规范和任务不再标记 read-only，改为整体清理；新实现不得读取其内容作为许可。
- 被 v2.1.3 完整替代、且权威目录无反向引用的旧架构文档可从当前分支删除；Git 跟踪文件仍可由历史恢复，未跟踪文件必须在删除报告中明确不可由 Git 恢复。
- Phase -1 与 import firewall 全部通过，才允许 `01-contract-codegen` 进入 Phase 0。

### R6. 分阶段交付

- 每个 child task 都必须有独立、可观察验收标准和显式前置依赖；目录编号不代表依赖。
- 实际顺序为 `00 → 02 (共同完成 Phase -1) → 01 → 03 → 04 → 05/06 → 07 → 08 → 09 → 10 → 11`；`00` 只冻结 consumer-lock 契约，首个真实 lock 必须等 `02` 产生精确 kernel wheel 后物化。
- 每个 child 同时交付 Pipeline/Agent contract/conformance 影响，不允许最终阶段再复制第二 Runtime。
- 新功能只在 shadow 中证明；真实发布必须经过 PublicationEnablement 与平台整批原子可见认证。

### R7. 可执行根信任与防偏

- 跨仓单一真源固定为 `auto_cut_bot/governance/`：Trellis backend code-spec、authority lock、五份契约、Reuse Ledger Schema、任务契约和门禁配置均由 kernel owner 仓库追踪。
- `auto_cut_bot/governance/authority-lock.yaml` 绑定 contract version、authority commit、五份生产契约、实现契约、Schema/Registry 与 blocking fixtures 的精确 hash。
- `00` 只交付 consumer-lock closed Schema、生成/验证接口、阻断负例和 `ConsumerLockReadinessReceipt(state=not_materialized, reason=kernel_build_not_yet_available)`；此时创建任何真实、pending 或 placeholder lock 都是失败。
- `02` 从 exact `auto_cut_bot` commit 的隔离 kernel wheel 首次物化 `ac_auto_cut/governance/authority-consumer.lock.yaml`。它是确定性生成的只读消费凭证，只包含 authority、kernel source/build 与 eligibility hashes，不是第二份权威契约，也不得自引用 consumer commit。
- 首份 lock 只能是 `bootstrap_consumable`；`execution_eligible|shadow_eligible|publication_eligible` 必须由后续阶段各自的必需 receipts 升级。另由 post-commit `ConsumerLockReceipt` 绑定 lock blob 与 consumer commit tree。
- 普通实现任务禁止修改 authority root、codegen source、architecture gate 和 blocking fixtures；发现缺口时必须停止并另建 Authority Change 任务。
- 每个子任务必须声明 `allowed_write_paths`、`forbidden_runtime_import_roots`、带 `inventory|fixture|offline_eval` purpose 的 `permitted_legacy_read_roots`、`required_authority_hash`、前置 commit 和可执行验收命令；越界写入、未登记读取和任何禁区运行时 import 直接失败。
- 运行时的 RunManifest、CommandReceipt、ArtifactSet 和发布凭证必须绑定 authority/schema/registry/build hashes；不匹配时 fail-closed。
- 实现者不能通过同一普通任务修改根门禁来让自己通过；受保护路径的变更必须经过独立授权。
- 本地隔离实现由 Supervisor gate 推进，不依赖远程平台；未证明远程规则不可绕过时，只禁止 push/release，不阻塞本地 child task。

### R8. 模型职责分离

- `gpt-5.6-sol` high/xhigh 负责 authority/root-of-trust 变更、契约解读、数据库原子性/CAS/Recovery、精确搜索证书、发布事务与最终跨阶段审查。
- `gpt-5.6-terra` high 可承担契约已冻结、边界清晰且有 blocking tests 的主体实现；其产物必须由 Sol 或同等独立验证者复核。
- `gpt-5.3-codex-spark` 仅用于可完全机器验证的叶子工作：fixture 搬运、格式化、简单 codegen 产物核对、小范围重命名和文档链接；禁止独立负责 authority、migration、CAS、Admission、Recovery、QC/release 决策或 architecture gate。
- 实现与检查使用不同 task context/check manifest；“同一模型再看一遍”不能代替受保护 oracle 和独立验收。

### R9. 流程化实现一致性

- 每个 child 冻结一个 `TaskSnapshot`，确定性检查产生 `CheckReport`，只读监督 Agent 产生逐条 AC 的 `SupervisorDecision`。
- 决定只有 `allow|repair|deny`；同一 finding 最多 repair 两轮，之后 blocked/replan。
- 普通 task 使用一个实现 Agent + 一个监督 Agent；authority/high-risk 才追加一次定向对抗复核。
- task/context/candidate/gate/toolchain/Supervisor contract 任一输入变化使旧决定失效；输入未变化时禁止重复全量审查。
- 仓库文本和日志按不可信数据处理；监督前后复算 candidate tree；finding 使用稳定指纹，改写描述不能重置两轮 repair 预算。
- 完整契约由机器 Loader 全文读取并 hash，Supervisor 只接收带章节范围和 slice hash 的相关切片；无锚点或切片截断时 task admission 失败，禁止把全文直接塞入模型上下文。
- upstream parity、baseline 归因、consumer lock、import firewall 和 history scan 作为按需确定性 gate，不再扩散为所有 task 都必须理解的 Receipt 图。
- 完整监督契约以 `v2.1-implementation-design/12-implementation-conformance-and-ai-drift-prevention.md` 为依据。

## Acceptance Criteria

- [ ] AC1：权威包可在未安装 legacy distribution 的环境 build/import/test，且 kernel import graph 不含 legacy/bridge。
- [ ] AC2：AST firewall 在第一个业务实现 commit 前成为 required CI check；所有绕过 fixture 均被拒绝。
- [ ] AC3：任何生产代码 legacy import 缺 ledger 或 disposition/允许 importer 不匹配时 CI fail。
- [ ] AC4：Bridge 输出 Schema 无旧对象、业务状态或自证安全字段；kernel 对同一输入独立校验。
- [ ] AC5：根 Trellis code-spec 不再包含旧 ArtifactBus/无数据库实现许可；旧 active tasks 与仓库内旧 `.trellis` 不再可被工具发现。
- [ ] AC5a：实现设计、codegen 路径和 package metadata 不再把现有 `autocut_core` 当作新 authority kernel。
- [ ] AC6：`autocut_authority` migrations、CAS/Outbox/权限/生命周期测试通过。
- [ ] AC7：五阶段、精确剪切、四层 QC、双 Runtime conformance 和故障注入全部闭合。
- [ ] AC8：外部平台 all-or-nothing 批次通过 prepare/commit/query/reconcile 认证；混合可见永不报告成功。
- [ ] AC9：迁移仅采信 ledger-approved 能力和 MigrationPolicy 允许的数据；旧 Admission/Recipe/发布状态不得迁移为权威。
- [ ] AC10：所有 child 完成后 parent 执行跨 child integration review，未闭合项阻止 cutover。
- [ ] AC11：仓库 tracked Trellis source 可确定性同步到 operational `.trellis/`，任何双向漂移都使 CI 失败。
- [ ] AC12：authority lock、任务完整性、diff allowlist 和 protected-path 检查在业务实现前成为 required gate。
- [ ] AC13：部署包或数据库 RegistrySet 与 authority lock 不匹配时无法启动 authority writer，更无法发布。
- [ ] AC14：业务 task 不能修改门禁后自我批准；若启用 push/release profile，远程 required checks/CODEOWNER 绕过测试也必须失败。
- [ ] AC15：每个子任务记录 implementation/check 模型档位和 reasoning effort；Spark 被分配到高风险 owner 角色时 task admission 失败。
- [ ] AC16：`autocut_kernel` 仅有 `auto_cut_bot/packages/autocut-kernel` 一份 source；`00` 不生成 lock instance，`02` 只从 exact source commit/subtree 与隔离 wheel 物化首个 `bootstrap_consumable` lock；Pipeline 无 path import、vendor copy 或未锁定 Git dependency。
- [ ] AC16a：wheel/source/authority/build provenance 任一 hash 不匹配、pending/placeholder lock、bootstrap profile 调用 writer/业务 Command/发布路径、或 consumer commit 自引用均 fail-closed。
- [ ] AC17：upstream nanobot sync 通过受保护 PR 进入 `auto_cut_bot`，不能修改 governance/kernel protected paths，且必须重跑 Agent/Kernel conformance。
- [ ] AC18：所有 child 都有输入 hash 闭合的 `TaskSnapshot`、`CheckReport`、`SupervisorDecision`；删除、伪造、截断上下文、改变 gate/toolchain/contract 或 Supervisor 写入 candidate 都会使旧决定失效。
- [ ] AC18a：Context Loader 能证明直接引用文件 EOF/全文件 hash，Context Planner 能证明实际切片范围/hash；超出上下文预算时拒绝或要求拆分引用，而不是自动摘要后继续 allow。
- [ ] AC19：若启用 push/release profile，候选树安全但 outgoing history 含 session/runtime artifact 的 fixture 必须得到本地 `allow`、远程 `deny`，证明本地完成不等于可发布。
- [ ] AC20：upstream capability 缺映射、changed-scope 失败伪装成 baseline、实现任务修改自身 oracle 三类绕过均被 required CI 拒绝。

## Out of Scope

- 原地修补现有 `autocut_core` 使其自称 v2.1.3 authority。
- 一次性迁移所有历史缓存、Recipe、QC pass 或发布记录。
- 以运行成功样本替代 contract/conformance/fault-injection 证明。
- 为兼容旧代码在 kernel 内保留动态 import、路径注入或自由对象适配。
- 将 nanobot agent loop 复制到 `ac_auto_cut`，或在两个仓库各维护一份 kernel/contracts。
