# R5：验证能力的产品接入与真实本地成片

## 目标与完成定义

只将 R1/R2 中有质量证据的模块接入正式 Stage，将 R3/R4 的可选入口连接到同一 Runtime，
在 PC WSL 以真实剧完成 HTTP 本地成片，并验证 Agent 可读取/续跑同一业务状态。

本阶段完成不表示外部平台发布。完成结果是一条通过本地 QC、原子晋升到本地输出目录的影片，
以及可追溯的 Artifact/Receipt/debug/运行说明。

## 进入条件

- 当前本地成片主线已具备目标 Source 授权、跨 run VLM 复用、Stage 1–3、Media Preflight、Recipe、Render/QC。
- R0 可复现；候选 R1/R2 有 accept_for_R5 结论，没有结论的模块不接入。
- 对应历史 Artifact reader 保留；数据库 migration 有向前/向后兼容方案。
- R3 capability 只能暴露实际完成的能力；R4A 可选，R4B 不作为无人 Pipeline 前置。

## 产品配置与版本

新增一个显式 Pipeline execution profile，而非改动运行中 profile：

```text
semantic_story_local_render_v1
source_prep → context_prepare → vlm/reuse → stage1 → stage2 → stage3
→ media_preflight → exact_span/recipe → render_local → local_release_qc
```

`local_release_qc` 只决定本地产物是否可见，不代表外部平台发布许可。
未来 Publication QC/平台 connector 另有策略和发布决定，不能复用本地 success 自动发布。

具体 stage 名以现有 `PipelineStageRegistry` 注册结果为准；实施时不能仅靠文档名称接线。
profile 绑定各 stage strategy/policy hashes、并发/重试预算、输出根和本地 QC policy。
R1/R2 若未被接受，profile 使用现有策略，不留空占位。

## 接入顺序

1. 在 Kernel 注册已接受的新 DTO/策略、Artifact types、writer/reader/replay；先部署读者。
2. Pipeline Runtime 增加 stage adapter 与依赖快照；补精确 idempotency key 和 reconcile。
3. HTTP 创建 run 时冻结 profile、source census、上下文和 reuse plan；返回 run/plan refs。
4. Worker 按依赖调度。episode 级阶段允许有界并发和独立重试；batch finalizer 负责完整性。
5. Agent R3 从同一 service port inspect/execute，不直接操作 Store 或文件。
6. Recipe 渲染到 staging，本地四层 QC 通过后原子晋升；失败产物不进入可见目录。
7. 可选 R4 timeline 读取同一 committed Recipe/Render/QC。

## 重试、续跑和局部失效

每个外部调用只有 Command 层持有预算。429/暂态错误按策略退避；连续失败耗尽后该 episode blocked，
其他独立集可完成。结果不明先 reconcile。同一失败 episode 可用相同输入重放或修改输入后产生新 revision。

依赖失效按 hash 传播：VLM prompt/memory 变化影响对应窗口及语义后继；Stage 2 prompt 只影响 Stage 2 以后；
媒体参数只影响 media/Recipe/Render/QC；报告、UI 和 debug 展示不使模型结果失效。
系统先返回 recompute plan 和预计 provider 调用数；不会因代码无关改动自动全量重跑。

## 数据库与文件

继续使用当前 PostgreSQL 物理 schema 和 Blob/Artifact Store。优先增加注册类型与 JSON payload；
只有现有索引无法保证 claim/CAS/查询语义时才迁移表。migration 先在独立测试库执行，重复执行幂等。

模型输入输出按 stage/attempt 保存到私有 debug 根；DB 保留请求/响应 Blob refs、usage、终态和因果链。
本地视频输出：staging、quarantine、visible 分离；晋升采用原子 rename/目录交换，旧 revision 可查询。

## 真实验收矩阵

| 场景 | 证据 |
|---|---|
| 全新单集 | VLM→Stage1–3→media→Recipe→Render/QC 全链，debug 完整 |
| 兼容旧 VLM | provider_calls=0，复用来源/目标授权/兼容 hash 闭合，后继可重建 |
| Stage 2 提示词变化 | 只调用 Stage 2 及后继，VLM/Stage1 refs 保持 |
| 单集 Provider 连续失败 | attempt/退避/最终状态明确，其他 episode 不被撤销，batch 未伪成功 |
| Worker 中断 | 同 Command key 恢复，无重复外部副作用 |
| 白屏/截字/字幕残留 | ExactSpan/QC 拒绝或选安全 variant，非法产物不可见 |
| HTTP 与 Agent | 相同请求映射到相同 Artifact hashes 和等价业务事实 |
| 配置/策略变更 | 返回精确 recompute plan，不因审计 hash 的无关变化全量重跑 |

## 发布步骤与回退

先 shadow 保存新策略结果，再对指定测试 profile 启用；不修改已有 run。观察结构拒绝率、语义质量、
provider 调用/成本、各阶段耗时、重试与 quarantine。通过后才将新 profile 暴露为可选默认。

回退只改变新 run 的 profile 选择，旧 Artifact reader 和策略实现继续保留。出现 P0 数据错绑、重复外部调用、
非法产物可见或双 Runtime 业务差异时停止创建新 profile run；已提交证据不删除。

最终退出产物：固定 commit、数据库 migration（若有）、PC WSL 启动命令、真实 run ID、各阶段 Receipt、
debug 路径、本地影片/QC 报告、失败重跑证据和回退命令。任何一项缺少都应标为部分完成。
