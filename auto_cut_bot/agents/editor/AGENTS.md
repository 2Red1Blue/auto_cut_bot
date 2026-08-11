# Agent Rules — Editor

## Language Style
- 使用中文
- 专业克制，不废话
- 每步输出当前 milestone 和进度

## Execution Rules
1. 上下文累积：每个 stage 的结果留在上下文中，下一步直接使用
2. 不 spawn 子 Agent：23 个 stage 全部自己做
3. 只在进入新数据源时查 DB（其余时间上下文就是数据库）
4. 调用外部 LLM 时记录调用次数和 token 消耗

## Review Gate
1. 完成 story_plans 后，必须委派 reviewer 进行独立审核
2. 审核通过 → 继续 production_agent
3. 审核拒绝 → 根据 reasons 修改 → 重新委派 reviewer
4. 不能绕过 reviewer，不能修改 reviewer 的审核结果

## Error Handling
1. LLM 调用失败 → 重试 3 次，仍然失败则标记 degraded
2. DB 不可用 → 使用 ArtifactCache 降级
3. VLM/ASR 不可用 → 跳过该 stage，标记 *_with_degradations

## Self-Review
1. 完成每个 milestone 后做 failure-seeking review
2. 检查：结构完整性、角色一致性、素材覆盖
3. 发现问题 → 立即修复，不等到 reviewer 发现
