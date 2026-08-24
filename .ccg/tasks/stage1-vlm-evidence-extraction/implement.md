# Implementation

## Layer 1（并行、文件互斥）

1. `media/root_evidence.py`：定义 coverage、音频 sample、Transcript、VAD、视觉、字幕 timing 和完整 root bundle；保持 `media/preflight.py` 现有 fixture 行为不变。
2. `vlm/models.py`、`vlm/parser.py`、`vlm/window.py`：定义 Kernel-owned WindowManifest、ProxyTimelineMap、请求身份、严格 observation parser 和纯函数时间映射；不调用 provider、不写数据库。

## Layer 2（Layer 1 后顺序集成）

3. 扩展现有 Store 的 Blob/GenerationAttempt 能力并加入篡改、幂等、ambiguous timeout 测试。
4. 使用相同 Command/Receipt/ArtifactSet/CAS 实现 `GenerateVlmEvidenceCommand`。
5. 将 committed VLM observations 投影到现有 `SemanticChainInput`，禁止 PTS、路径和 provider raw text 穿透。
6. 将 `physical_edit/exact_span.py` 演进为视频/音频四端点 exact pairing；保留旧 fixture API 作为测试兼容投影，禁止第二套 optimizer。
7. Pipeline Runtime 与 Agent Runtime 只调用上述 Kernel Commands。

## 校验顺序

1. 新增模块单元测试与拒绝路径。
2. 现有 Kernel 全量测试、类型/导入防火墙。
3. 独立只读审查 Agent 对照 `requirements.md`、`design.md` 和 changed diff。
4. Critical/Warning 修复后重跑测试并再次审查。
5. 每个可独立回滚的层及时提交到 `feat/v213-contract-codegen`。
