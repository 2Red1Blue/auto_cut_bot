# Plan

1. 新增 execution profile v11：V23 VLM + Stage 1/2/3 policies，无 media fields。
2. 新增 `semantic_story` 显式运行计划及六阶段命令 DAG。
3. 新增 migration 0049，复用 v10 与 Stage policy shape validators。
4. 增加 profile/composition/store/migration 回归测试。
5. 独立审查、提交推送、PC 拉取并跑真实单集。
