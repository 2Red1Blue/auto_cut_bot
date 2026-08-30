# Plan

1. 从真实 HTTP 入口和执行计划还原实际 Stage DAG。
2. 定位每个 Stage 的 Pydantic/JSON Schema、构造器与 Admission 校验。
3. 建立字段必要性、生产者、消费者和失败策略矩阵。
4. 对抗性检查过严校验、欠校验、重复输入、动态引用和 token 膨胀。
5. 结合真实失败 Receipt，形成分级结论与最小修复顺序。
