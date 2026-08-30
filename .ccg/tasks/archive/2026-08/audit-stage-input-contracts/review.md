# 当前各阶段输入与格式校验审查

## 结论

当前实现不是“校验不足”，而是两种状态同时存在：

1. SourcePrep、ContextPack、VLM、MediaPreflight 的内部输入闭合和 provenance 校验总体合理；
2. Stage 1-3 的生产 authority 尚未落地，并且模型输入把审计证明、长哈希引用和业务语义混在一起；Stage 4/5 尚未进入强流水线。

因此不能以测试通过推断整个真实流水线已经具备合理且可执行的输入契约。

## P0

### A. semantic_story 没有独立的 Stage 1-3 生产 authority

`semantic-run.json` 只授权 SourcePrep/VLM，Stage 1-3 capability 为 false；组合器却在 `semantic_story` 下加载旧 `local-run` resource 取得 Stage 1-3 policy。仓库中只有 synthetic test policy，无法作为真实输入权威。

修复：把生产 Stage 1-3 policy 纳入共享 semantic authority；semantic_story 不得依赖 ASR/剪辑用 local-run resource。

### B. 剧集顺序不是显式业务输入

SourcePrep 按相对路径字典序建立 episode_index。它能保证确定性，不能保证 `1,2,3...` 的剧情顺序。真实多集故事链必须由 source catalog/episode manifest 显式提供 `relative_path -> episode_ordinal`，并将其纳入 authorization hash。

### C. Stage 1 缺少明确的时间/集序投影

Stage 1 prompt 只有 window hash、summary、entities/facts/events；没有 episode ordinal，事实/事件也没有 support interval。数组顺序不是足够的叙事顺序证明。

修复：给模型短 `w001/e001/f001` alias，并提供明确的 episode ordinal 与窗口内 temporal ordinal/interval；Kernel 保留 alias 到真实 hash/ref 的映射。

### D. Stage 4/5 不是当前强流水线的活动输入契约

semantic_story 明确不构造 media/render/publication；当前 render Recipe 仍限定 `fixture_ground_truth_v1`。因此真实 Recipe -> Render -> Publication QC 不能宣称已闭合。

## P1

### E. Stage 1-3 让模型回显长哈希和完整 member_ref

Stage 1 同时输入 `allowed_refs` 和 windows 内的全长 sha256；Stage 2/3 输出反复携带 artifact_type/logical_id/revision/scope/content_hash。它们对审计有用，对模型推理没有必要。

修复：审计 envelope 保留完整引用；provider prompt/response 仅使用请求内短 alias，返回后由确定性 compiler 扩展并验证。

### F. Stage 3 把完整审计池直接当 prompt

Stage 3 prompt 包含 Source、全部 VLM request/pack、Stage 1/2 全部 members、未选 proposals 和 diagnostics。完整池应留在 Kernel 做闭包验证，模型只需选中 Story 的语义闭包。

修复：分离 `AuditClosure` 与 `ModelContextProjection`；后者按 Story 构造最小必要上下文。

### G. Stage 2 JSON Schema 没有用已冻结 policy 收紧枚举/范围

genre_tags、editing_profile、teaser_strategy、duration 等在 schema 中仍是泛文本/正整数，随后 admission 才按 StoryPolicy/JobPolicy 拒绝，造成可避免的模型失败。

修复：response schema 从完整 Stage2CommandPolicy 生成；用 enum/const/min/max 尽量前置约束，独立 admission 继续复算。

### H. HTTP JSON ingress 不拒绝重复 key

`request.json()` 使用普通 JSON 解码，后续 closed mapping 只能看到重复 key 的最后一个值。应以严格 UTF-8 JSON、重复键拒绝、有限 body size 进入 `PipelineRunRequest`。

### I. 外部 ContextPack 仍有轻微剧透/选择质量风险

人物 `role`/alias 没有 `known_from_episode`；角色按已排序 ref 截前 8 个，不是按当前集相关性选择。辅助输入不影响物理剪辑，但会影响身份解析和剧情理解。

### J. Stage 1-3 只有 byte budget，没有模型 token budget

字节上限可保护资源，不能证明 provider context window 或成本上限。应同时冻结 deterministic token estimate、provider context ceiling 和超限后的确定性分片/归并策略。

### K. V23 把 continuity 全部固定为 false/empty

这提高了格式稳定性，但会丢失真实的跨集未完事件信号。因果边与 temporal_segments 固定空合理；starts/ends_mid_event 应允许模型观察并由 Stage 1 保守合并。

## 保留项

- HTTP run intent 保持小而封闭，不允许调用者上传执行 policy。
- Source authorization、content hash、immutable Blob、Receipt/CAS 应保留。
- 外部 API 的 Snapshot -> Normalizer -> explicit episode map -> WindowContextPack 边界合理。
- ASR/VAD/字幕不进入 VLM prompt 合理；它们属于物理边界证据。
- V23 的局部短 ID、closed JSON Schema、独立 parser、引用闭合、support overlap 校验应作为 Stage 1-3 的参考实现。
- MediaPreflight 的完整 provenance 虽字段多，但它是确定性内部 Command，不消耗 LLM token，主要字段有必要。
- 物理时间继续使用带 time_base 的整数 tick，不应退回浮点秒。

## 推荐修复顺序

1. semantic authority v2：落入真实 Stage 1-3 policies，删除 semantic_story 对 local-run 的依赖。
2. explicit episode manifest：显式绑定剧情顺序。
3. Stage 1 temporal projection + 全链路 short alias map。
4. Stage 2 policy-derived JSON Schema。
5. Stage 3 AuditClosure/ModelContextProjection 分离。
6. strict HTTP JSON ingress 与 token budgets。
7. 再接 Stage 4/Recipe/Render/Publication QC。
