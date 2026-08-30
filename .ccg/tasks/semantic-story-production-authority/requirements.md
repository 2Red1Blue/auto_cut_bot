# Requirements

1. `semantic_story` 必须独立运行 SourcePrep、ContextPrepare、VLM、Stage 1、Stage 2、Stage 3。
2. 它不得读取或依赖包含 ASR、VAD、物理剪辑、渲染能力的旧 `local-run` authority。
3. Stage 1-3 使用真实 Doubao 文本生成策略，不得使用 tests 中的 synthetic policy/model/prompt。
4. 安装资源必须以闭合 schema、内容摘要和代码复算绑定策略身份。
5. 运行时持久化的 VLM/Stage 1/2/3 policies 必须与安装 authority 完全相等。
6. 测试必须证明 `semantic_story` 组合时即使 local-run resolver 被禁止调用仍可成功。
7. 代码经定向与数据库测试后提交并推送，PC 通过 Git 拉取并运行一集真实流程。
8. 模型输入不得新增物理剪辑、ASR/VAD 或外部发布参数。
