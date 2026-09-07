# R1：叙事规划与素材匹配策略

## 目标与完成定义

吸收 NarratoAI 的叙事承接、片段职责和局部修复方法，比较当前 Stage 2/3 输出是否更完整。
本阶段复用已提交 VLM 与 Stage 1，初次实验不调用视频模型、不读取 ASR/字幕作为语义输入。

完成需要至少一组调参剧、一组留出剧；同一上游输入生成 baseline/candidate；结构校验、独立盲评、
故事完整性和下游素材可行性均有结果。只有 candidate 通过冻结门槛才注册新的生产策略。

## 设计边界

可吸收：人物困境、冲突触发、关系变化、阻力、悬念等叙事职责；跨片段承接；针对具体错误的局部反馈。
不吸收：从字幕时间直接选最终片段、固定 OST 交替、强制旁白、用解说文本定义剧情事实。

现有接点：

- Stage 2：`semantic_chain/story_design_compact_context.py`、`story_design_compact.py`、
  `pipeline/compile_story_portfolio_request.py`；
- Stage 3：`pipeline/build_editorial_blueprint_request.py` 和相应 compiler/evaluator/command；
- 单阶段入口：`scripts/run_stage2_request.py`；分阶段 debug 沿用 `pipeline/debug/model_io.py`。

## 两步落地

### R1A：shadow A/B

新增 `tools/reuse/narrative_strategy/`，读取精确的已提交 Stage 1 inputs/request identity，生成两份未准入草案：

- baseline：当前冻结 prompt/schema；
- candidate：在相同 compact context 上增加 `NarrativeDutyPolicy/v1`。

`NarrativeDutyPolicy/v1` 只定义可选职责枚举 `hook/setup/escalation/relationship_turn/reveal/payoff/bridge/cliffhanger`
及适用约束。模型为每个 proposal/beat 选择职责并解释；程序恢复 proposal、fact、event、obligation 引用。
没有某种职责不自动失败，必选事实和素材可行性仍由现有 Kernel 计算。

实验输出保存 raw draft、严格 decode 结果、现有 evaluator diagnostics 和评分报告，不写生产 Store。
同一请求只改变目标 prompt strategy；provider/model/context/预算保持相同并写入 spec。

### R1B：验证后注册

若 A/B 通过，新增明确的 prompt/strategy 版本，不原地改 compact-v2：

```text
stage2-proposal-compact-narrative-v1
stage3-editorial-blueprint-narrative-v1
```

版本同时绑定 prompt、response schema、decoder、projection 和业务 evaluator。HTTP profile 与 Agent capability
在 R5 才启用。旧 request/response 继续按原策略重放。

## 模型与程序字段

模型输出可以新增 `narrative_duties[]`：`duty_kind`、`target_ref`（proposal/beat 短引用）、
`reason`、`setup_ref/payoff_ref`（适用时）。程序检查引用存在、同一 Story、setup 早于 payoff、
职责不替代必选 obligation。全局 ID、input hash、required facts、source grants、physical checks 仍由程序生成。

模型修复请求使用 `NarrativeRepairRequest/v1`：原 attempt/raw hash、错误路径、合法短引用、
必要局部上下文和剩余预算。只修失败 proposal/partition，返回后整体重建本阶段 ArtifactSet。
相同输入+相同错误指纹不重复调用；enum 排序、ID 恢复等机械问题走本地 reprocess。

## 实现任务

1. 用 R0 固定两个真实上游输入，补盲评表和必须保留事件/转折标签。
2. 将 NarratoAI 可用规则改写成 `NarrativeDutyPolicy`，不得复制字幕/旁白专属约束。
3. 实现 shadow prompt adapter 和严格 decoder；保存 baseline/candidate 完整 debug。
4. 计算结构失败率、必选事件覆盖、setup/payoff 完整、素材可行、人工修订量及 token/延迟。
5. 对失败样本执行一次有预算局部修复，验证没有重跑 VLM/Stage 1。
6. 达标后再实现 R1B 的 Kernel 注册、三个独立重建点、Stage 3 reader 和 golden replay。

## 验证和退出门槛

- 单元：未知职责/引用、未来事件引用、跨 Story setup/payoff、重复职责、空 proposal、预算耗尽。
- 集成：已保存 Stage 1 → baseline/candidate Stage 2 → Stage 3 dry-run；provider 调用次数与 debug 一致。
- 回归：旧 Stage 2/3 request bytes、旧策略 hash 和旧 Store reader 不漂移。
- 质量：留出剧的叙事完整性或人工修订时间有可复核改善，素材可行率和关键事实覆盖不低于冻结容忍度。

退出时明确选择：`accept_for_R5`、`continue_shadow` 或 `reject`。reject 保留报告，不留下半注册生产策略。
