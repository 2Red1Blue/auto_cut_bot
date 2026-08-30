# Plan

1. 阅读 VLM Prompt/Schema 版本注册、CandidateCatalog 投影与 Story Portfolio 输入契约。
2. 先写跨层失败测试，证明当前 V22 空候选无法形成 Story 输入。
3. 新增向后兼容的语义候选 Prompt/Schema 版本并接入权威资源。
4. 增加异常分类的最小闭环及测试，不伪造成功 Receipt。
5. 运行定向测试、lint 和独立对抗审查；修复发现。
6. 提交并推送 Git，在 PC 干净工作树拉取并跑真实单集到 Stage 1–3。
