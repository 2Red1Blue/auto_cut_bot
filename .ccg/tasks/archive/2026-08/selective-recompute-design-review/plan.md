# Plan

1. 主 Agent 核对现有说明和测试；独立 explorer 只读检查 Store/Command/resume/finalizer。
2. 将最小可行选择性重算写入 docs/pipeline-selective-recompute-design.md，修正文档误导。
3. 另一独立 reviewer 对初稿做反例审查；主 Agent 修订并保留逐项闭合证据。
4. 执行相关现有单元/API 测试、文档链接/结构检查；新增契约测试只标记为待实现。
5. 归档审查任务，精确暂存文档并提交，不提交私有配置，不部署。

已应用 software-architecture-design 的字段必要性审计和定点文档修订。
不采用其与本项目不符的旧八表结构、VLM 数据库直写或 ASR 语义主导模式。
独立审查使用 Codex 子 Agent，不调用已被用户停用的 Claude Code。
