# Requirements

1. 同一 HTTP Run 依次执行 SourcePrep、ContextPrepare、V23 VLM、Stage 1、Stage 2、Stage 3。
2. 使用已安装 V23 semantic authority 的精确 Prompt/Schema/Parser，不回退 V3。
3. Stage 1-3 继续使用已安装 local-run resource 中冻结的命令策略；不得授予 media/render/publication 权限。
4. 新增不可歧义的持久化 execution profile，历史 v9/v10 行保持可读且不被改写。
5. PostgreSQL 命令 DAG 必须与 profile 一致，不能靠进程内临时添加阶段。
6. 通过模型 round-trip、数据库迁移、composition 与 stage registry 测试。
