# Pipeline Stage Input Contract Review

## Verdict

当前 SourcePrep、ContextPrepare 和 VLM 的来源绑定、幂等、哈希与严格 JSON 校验整体合理；真实 PC 单集运行也证明这三阶段可以完成。

但完整流水线尚不能继续进入 Story Design：V22 Prompt/Schema 强制 `candidate_hypotheses=[]`，而 Stage 2 的 CandidateCatalog 只投影该数组，随后 Portfolio 又要求非零 proposal/selected story。该矛盾是 P0，不是普通“校验太严”。

## P0

1. **候选来源断路**：V22 禁止 VLM 输出候选；CandidateCatalog 仅遍历 VLM candidate；Stage 2 要求 proposal 数量满足非零 JobPolicy。必须恢复一种权威候选来源：恢复 VLM candidate，或增加显式 CandidateSeed 编译阶段并让 Catalog 改为消费该产物。
2. **异常统一变成 indeterminate**：PipelineStageRunner 捕获所有异常后统一投影 `indeterminate`。静态格式错误、配置错误、不可恢复的契约错误因此可能被无限重试。应建立 `retryable / denied / failed / indeterminate` 分类，只有 provider dispatch 结果未知等情况可进入 indeterminate。

## P1

1. **模型自报 confidence 被直接当作 Admission 输入**：真实 Doubao 输出的所有 entity/fact/event support confidence 均为 `1`；Coverage 直接与阈值比较。模型自信不能等同于已校准证据置信，应降为诊断值，或由独立 evidence/calibration 规则产生 admission confidence。
2. **Stage 1–3 长引用不适合直接给模型**：Stage 1 使用完整 window SHA 引用；Stage 2/3 输入和输出携带完整 Artifact/Member/Scope 引用。应在 provider wire 层使用 `w001/e003/c004` 等 request-local alias，由确定性 compiler 展开为完整引用；持久化层继续保留完整 provenance。
3. **VLM wire 含必填但永远为空的字段**：cause/effect、temporal_segments、candidate_hypotheses 仍占 Prompt/Schema 并制造格式失败面。Provider wire DTO 应只含模型真正负责的字段；确定性 compiler 可补齐内部 Artifact 的固定空字段。
4. **空/无可观察内容没有合法表达**：根 `facts` 至少一项会迫使黑屏、片尾或无有效内容窗口伪造事实或整次失败。应加入 `content_status=no_observable_content` 分支，并要求其他数组为空。
5. **Debug 只记录控制面外壳**：Stage input/output 只有 RunRequest、Command、Profile hash 和 Receipt，不能看到 SourcePrep 的 census/manifest、ContextPrepare 的 Pack/binding、Admission 诊断。应增加受限的业务输入/输出摘要和 Artifact refs；模型请求/终态仍保留完整原始 I/O。
6. **WindowContextPack 的 video_only 不变量未完全关闭**：video_only 禁止 selected refs，但未禁止 source/normalized hash 和 known-through ordinal。持久化解码应禁止任何部分 API provenance 混入 video_only。
7. **HTTP profile 命名不闭合正式运行语义**：入口仅允许 `test|shadow`，但 shadow 会调用真实付费 provider。应将“执行真实 provider”和“是否允许发布”拆成独立字段/策略；生产发布前再引入明确 production/release admission。

## Keep

- HTTP closed body、exactly-one source、Idempotency-Key；生产建议只公开 `source_reference`。
- SourcePrep 的授权 root、不可变 census、内容 hash、BlobRef、PTS/timeline binding。
- Context Snapshot → Normalizer → explicit episode map → WindowContextPack；API 不可用时确定性 video_only。
- VLM 的 `file_id + prompt + strict json_schema`、响应 status/bytes/depth/引用闭合、整数毫秒语义区间。
- Stage 1–3 的完整 predecessor hash/CAS/Artifact scope 校验，但这些应留在 compiler/store 边界，不必原样暴露给 LLM。
- Media Preflight 的全源 ASR/VAD 一次生成与复用；其 capability/calibration/clock/time-base 字段是物理证据所必需，由服务端生成，不应由 HTTP 调用者填写。

## Real-run evidence

- PC run `pipeline_run_cc2196abcdef4645a7fa587c843d0d1a` 已完成 SourcePrep、ContextPrepare(video_only)、Doubao VLM。
- 实际 VLM request：1 个 `input_video(file_id)`、1506 字符 prompt、10855-byte strict schema；provider reported 33631 input tokens and 3509 output tokens。
- 输出包含 6 entities、15 facts、9 events、0 candidates；所有 entity/fact/event confidence 为 `1`。
- 当前无 18770 pipeline worker 进程继续消费旧 50 集任务。

## Verification

- `93 passed`：Ark provider、VLM stage、Stage 2 request targeted checks。
- `251 passed, 4 skipped`：contextual/minimal prompt、Stage 1/2/3 request/compiler、Media Preflight targeted checks。

这些测试证明现有局部契约按代码所写工作，但没有端到端测试证明“V22 空 candidates 仍能产生可发布 Story”；该缺失正是 P0 矛盾未被发现的原因。
