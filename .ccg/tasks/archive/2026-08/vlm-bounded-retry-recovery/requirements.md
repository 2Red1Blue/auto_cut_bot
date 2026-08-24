# VLM Bounded Retry Recovery

## 目标

豆包 VLM 单集执行不能因第一次可恢复故障直接终止整批。系统必须在冻结策略内创建有界、持久化的新 Attempt；只有预算耗尽或错误明确不可重试时，才能产生终态失败 Receipt 并停止后续集数。

## 必须满足

- 默认测试策略为 `max_attempts = 3`（首次调用加两次重试），策略必须进入冻结执行 Profile 和请求身份。
- `indeterminate` 表示远端是否接受请求未知：只能用原 `provider_request_id` reconcile，不能创建新调用。
- 明确 `retryable` 的终态 Provider failure 才能创建下一 Attempt；重试由 Kernel/Store 授权，Provider adapter 不自行重试。
- `denied`、认证/配置/请求/身份/媒体限制等确定性错误不得进行相同请求重试。
- JSON/Schema/语义输出失败属于 repairable，不能伪装成网络重试；首个版本可终止，但必须保留可扩展的分类。
- 同一 CommandSlot 可拥有按 `attempt_ordinal` 排序的多个 Attempt；每个 Attempt 的 provider idempotency key 必须唯一并绑定 Command request hash、策略和 ordinal。
- Ark HTTP trace/request ID 与 Responses API `response.id` 必须分离；只有后者可以作为 reconcile 身份。
- 并发 worker 只能创建同一 ordinal 的一条 Attempt；CAS/唯一约束必须防止重复付费调用。
- `dispatched` Attempt 必须受持久化 dispatch lease/token 保护；其他 worker 在租约有效时不得 reconcile 或把它改成 indeterminate，避免与正在流式调用的 owner 竞争 CAS。
- 中间失败仅写 Attempt 证据，不能提前写 Command 的终态失败 Receipt。
- 成功 Receipt 必须绑定实际成功 Attempt；最终失败 Receipt 必须包含所有 Attempt 的有序因果摘要及 `RETRY_BUDGET_EXHAUSTED` 或非重试原因。
- Receipt 与 Attempt 链必须有数据库可验证的精确关系，不能只依赖一段未约束的诊断 JSON。
- VLM batch 只在子 Command 真正终态 denied/failed 后停止。
- 不改变依赖方向：Runtime 调 Kernel Command；Kernel 持久化/决策；Provider adapter 只执行或查询一次远端请求。

## 验收场景

1. 第一次 503、第二次 429、第三次成功：单集成功，三条 Attempt，整批继续。
2. 连续三次 retryable failure：一条终态 failed Receipt，精确列出三条 Attempt，后续集不调用。
3. response.created 后断流：重复执行只 reconcile 同一 request ID，不新增 Attempt。
4. 401/403/无效请求：一次 Attempt 后立即终止。
5. 两个 worker 并发恢复：每个 ordinal 最多一次 provider dispatch。
6. 崩溃发生在失败 Attempt 提交后、新 Attempt 预留前：恢复后只创建一个下一 Attempt。
7. 第二个 worker 在首次调用仍在飞行时进入：不得调用 Provider，也不得改变 Attempt version；租约到期后才允许一个 reconciler 接管。
