# tools/reuse — 开源能力吸收实验 runner

统一实验入口，实现
[R0 实验底座](../../docs/open-source-adoption-phases/00-r0-experiment-foundation.md)
与 [13 方案](../../docs/llm-stage-contracts/13-open-source-adoption-architecture.md) §9 的合同：
冻结 spec、replay/live 双模式、预算账本、append-only attempt 证据、原子 finalize。
旧 `runner_skeleton.py` 已删除；本 README 是唯一迁移说明——一切以 `run.py` 为入口。

## 使用

```bash
# live：显式预算下真实调用（R0 只有 fake provider；真实 producer adapter 在后续阶段接入）
uv run python tools/reuse/run.py --spec <spec.json> --mode live \
    --media-root <私有媒体目录> [--resume]

# replay：按精确 request hash 命中预录响应，零 provider 调用；缺录制记 not_evaluated/REPLAY_MISS
uv run python tools/reuse/run.py --spec <spec.json> --mode replay \
    --media-root <私有媒体目录>
```

- `--mode` 必须等于 `spec.mode`，禁止自动切换。
- `--resume` 复用最近一个非终态 attempt（核对同一 spec hash）；否则新增 attempt 子目录。
- 产物落在 `artifacts/shadow/reuse/<experiment_id>/attempt-NNNN/`
  （metadata.json、calls/、projection/、metrics.json、report.md、.finalized.json）。
- 预录响应按 producer + request hash 存于 `artifacts/shadow/reuse/recordings/<producer>/`，
  live 写入、replay 命中；改动 prompt/model 后旧录制不会命中（REPLAY_MISS）。

## 冒烟记录（2026-09-07，macOS）

用真实 frozen fixture `when-lucifer-kneels-ep01-ep02` 走通 fake 协议：

- live 两次 → attempt-0000/0001 追加，互不覆盖；
- replay 一次 → 命中 recordings，`budget: null`（本次零 token）。
- 此 smoke 只证明 runner 协议，不声称任何真实开源方法有效。

PC WSL 上的隔离 replay（`unshare --net` / 容器 `--network=none`）待在 WSL 环境执行；
macOS 只做静态/协议验证，metadata 中 `isolation_passed` 恒为 false。

## 模块

| 文件 | 职责 |
|---|---|
| `run.py` | 唯一 CLI；编排 spec→registry→fixture→adapter→provider→storage |
| `models.py` | closed DTO（ExperimentSpec/v1 等）、strict JSON codec、错误码 |
| `project_registry.py` | `projects.json` loader；producer/commit/adapter 精确匹配 |
| `fixtures.py` | fixture manifest v1(YAML) 兼容读取 + FixtureManifest/v2；输入 hash 核验 |
| `sandbox.py` | Linux netns 隔离计划、子进程环境白名单 |
| `budget.py` | 调用/token 预算账本（所有调用共享，含 IPC） |
| `providers.py` | ProviderPort + FakeProvider + 录制索引 |
| `ipc.py` | 外部子进程 JSONL IPC（子进程不接触密钥/网络） |
| `metrics.py` | 指标协议：无分母即 null + 原因 |
| `storage.py` | attempt 目录、原子写、崩溃恢复、finalize 门禁 |
| `adapters/fake.py` | 协议验收 producer adapter |
| `adapters/narrative_shadow.py` | R1A 叙事策略 shadow A/B producer（baseline/candidate 两臂） |
| `narrative_strategy/` | R1A 模块：NarrativeDutyPolicy/v1、候选 wire 严格解码、局部修复协议、context 快照导出、A/B 对比报告 |

## R1A：叙事策略 shadow A/B（2026-09-07 实现状态）

`tools/reuse/narrative_strategy/` 已实现并在合成 Stage 1 fixture 上通过端到端测试：

- `policy.py`：`NarrativeDutyPolicy/v1`（hook/setup/escalation/relationship_turn/reveal/payoff/bridge/cliffhanger）；
  候选 prompt 增量不吸收字幕选时/强制旁白；schema 扩展保持 closed。
- `wire.py`：candidate wire `stage2-story-design-compact-narrative-v1`。剥除 duties 后交给
  kernel 原版 `decode_story_design_compact` 严格解码（未知字段/引用/素材约束不重实现），
  再校验 duties：未知职责、target_ref 不符、引用不存在/类型错、setup==payoff、重复职责、
  可证明的 payoff 早于 setup 全部拒绝；同集/事实引用记为 order indeterminate，不静默接受。
- `repair.py`：`NarrativeRepairRequest/v1` + 错误指纹去重（同输入+同错误不重复调用）、
  机械错误（JSON/预算/schema 版本）走本地 reprocess、剩余预算检查。
- `context_io.py`：把 compact context + draft policy 冻结为 shadow payload（真实运行时由
  已提交 Stage 1 产物导出；接入真实 Store 读取是后续工作）。
- `adapters/narrative_shadow.py`：R0 runner 接口的 builtin producer `narrative-shadow`，
  baseline/candidate 各一次调用，请求身份绑定 prompt/schema/context 哈希。
- `compare.py`：读取两臂 attempt 目录输出 A/B Markdown 报告（仅结构/协议指标）。

**未完成（不能宣称）**：真实上游两剧（调参剧+留出剧）的 shadow A/B 质量数据、盲评、
setup/payoff 完整性与人工修订量对比、R1B 生产策略注册。这些需要 PC WSL 环境的真实
Stage 1 输入与真实 provider；当前合成 fixture 结果不构成叙事质量证据。

## 接入一个新 producer

1. 在 `projects.json` 登记：repo_url、pinned commit、license_path、adapter_version、
   `adapter_module`（必须位于 `tools/reuse/adapters/`）。spec 与登记不一致 → `SOURCE_MISMATCH`。
2. 实现 adapter 五方法：`validate / build_calls / accept_response / finalize / project`，
   外加 `bind(spec, fixture)` 与 `compute_metrics(...)`（宿主约定，见 `adapters/__init__.py`）。
   adapter 必须确定性；崩溃恢复由宿主重放 accept_response 重建状态。
3. 外部依赖装独立环境/容器，绝不加入主 `pyproject.toml`。
4. 冻结 fixture set 放 `docs/llm-stage-contracts/fixtures/<set>/`；
   私有媒体经 `--media-root` 映射，路径逃逸一律拒绝。
5. producer 未实现前不得宣称实验通过；对照结论写入 `docs/llm-stage-contracts/` 评估笔记。

## 红线（重申）

runner 与 shadow 产物不得写入 `packages/`、`auto_cut_bot/`、`governance/`；
不得成为权威事实、物理切点、Admission 结果或发布许可。实验阶段不写 Kernel DB。
