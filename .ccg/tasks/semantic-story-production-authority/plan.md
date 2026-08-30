# Plan

1. 扩展 semantic authority schema/resource，加入 Stage 1-3 command policies。
2. 将 semantic_story composition 和三个 Stage adapter 改为校验独立 semantic authority。
3. 定义真实生产策略，并针对当前剧集规模设置明确 prompt/response/对象预算。
4. 增加 authority、composition、policy drift 和真实 profile round-trip 测试。
5. 运行 Ruff、定向 pytest、PostgreSQL V11 测试。
6. 审查 diff，提交并推送。
7. PC 拉取，启动一集 semantic_story，检查 debug/Receipt。
8. 用真实失败 Receipt 区分无语义格式差异与语义引用错误；只对前者做确定性规范化并回放真实输出。
