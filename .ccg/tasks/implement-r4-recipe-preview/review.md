# R4A 审查记录

范围：`f672388e..0630bdcf` 的 Recipe timeline/read-only HTTP 变更。

PC WSL 验证：84 passed；目标 Ruff passed。

## 已修复

1. HTTP public mapping 曾包含 committed Recipe ref 和 exact-span proof hash；现仅输出展示字段。
2. Recipe route 曾可能吞掉其它 `/api/pipeline/runs/*` 路径；现只处理明确的 `/recipes/` 路径，未知子路径落空。
3. inspection reader 现重新检查每个 member 的 canonical hash 和整个 ArtifactSet hash。
4. Store 查询与 payload inspection 移到工作线程，避免占用 WebUI event loop。
5. 不存在的 run 通过轻量 `PipelineRecipeNotFoundError` 映射为 404，避免 WebUI 顶层引入 runtime 包造成 Config 初始化顺序问题。

## 外部审查状态

Codex reviewer 报告的 provenance、set hash、重复 key 与路由问题已逐项处理。
Claude backend 实际报告模型为 `glm-5-3-flash`，与任务约定的 Claude 不一致，不能计作独立 Claude 通过；
保留此限制，不把当前 R4A 描述为双模型一致批准。

## R4A 交付时的前置缺口（已由下节闭合）

当时 ProductionRecipe 只保存选中 span，没有安全的 `select_variant` 写入源。R4A 本身仍保持只读；
该缺口随后由 R4B-A 的独立 `SpanVariantSet` 子命令闭合。

## R4B-A SpanVariantSet（2026-09-09）

实现：canonical top-K exact result、closed codec、`BuildSpanVariantSetCommand@1`、专用 Postgres writer、
generic writer/generation-kind deny、完整父 Stage 4 绑定、首次执行与重放的独立重建。

验证：PC WSL 新增及相关 Stage 4 回归 `89 passed`，目标 Ruff passed；独立 PostgreSQL
transaction/writer/replay `1 passed`。旧 `compile_candidate_av_span` 仍保留原单结果接口，新 portable-count
限制只作用于 variant API。

审查：Codex 无 Critical，提出父命令身份、Postgres 事务测试和负数/count 边界，均已修复。
Claude backend 实际仍为 `glm-5-3-flash`，不记录为 Claude 审查通过。预锁前的父重放只读取不可变
Artifact/Blob，锁内再次核对 child slot command/request/Job；重复计算是当前独立验证成本，后续可在不削弱
Store writer 复算的前提下减少首次执行后的第三次 readback 重建。

R4B 后续不再缺 variant 来源；剩余是 `EditProposal`、CAS、选定 variant 后的新 Recipe/Admission、Render/QC。

## R4B-B `select_variant` / CAS（2026-09-10）

实现：`EditProposal/v1` 关闭输入，只允许一个已提交 `SpanVariantSet` 的 `variant_id`；
`ApplyEditProposalCommand@1` 对父 Recipe/variant set 独立重读和重建，生成同 revision 的
report→Recipe→Admission closure。专用 Store writer 对完整父 ArtifactSet 的每个 logical head 做
事务内 CAS；generic writer 显式拒绝该 command。CAS stale 被提交为 terminal denial，不遗留
`running` slot；静态 reader 与 Render reservation 只在固定的 Compile/Apply producer 集合中接受
相同的 admitted layout。

验证：Mac 定向 15 项通过；PC WSL 在一次性 `ac_autocut_verify` PostgreSQL 容器中运行 16 项
既有/相关回归通过，另有新增真实 writer transaction 测试 2 项通过（首次 commit、idempotent replay、
stale parent 无部分写入）。目标 Ruff 通过。容器已停止并删除。

审查：隔离 Codex reviewer 未发现 Critical；其“stale CAS 可能遗留 running slot”和“多 operation 与
声明不符”已修复并在上述测试覆盖。其余 Warning 是后续能力边界：本版本不实现链式二次编辑、revert、
HTTP/UI 或真实 renderer/QC；`Apply` 成功只代表 admitted Recipe revision，绝不代表媒体已产出。
Claude 通道实际运行模型为 `glm-5-3-flash` 且超时，没有形成可计作 Claude 的独立 review。
