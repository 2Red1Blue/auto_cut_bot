# 首个跨 Job 源绑定切片

## 结论

首个实现不让 VLM、finalizer 或普通 Runtime 以 `producer_job_id` 跨 Job
读取 Blob。这样会把一条很窄的重跑授权变成任意旧 Job 的读取后门。

改为由一个专用的、确定性 `BindWholeSeriesSourcesCommand` 在一次数据库事务内：

1. 验证 origin 是精确成功的 SourcePrep 产物、源策略与目标授权完全相同；
2. 给**同一不可变 Blob object**增加目标 Job 的只读 claim（不复制字节、不转移 origin
   owner）；
3. 在目标 Job 写入等价的 `whole_series_source_manifest` 和 `source_reuse_binding/v1`；
4. 将 source manifest、binding、全部 target claims 和 Receipt 原子提交。

所以所有既有 VLM/ASR/Stage 1–4 读取器仍然只调用
`read_immutable_blob(target_job, ref)`；它们不需要知道另一个 Job，也不能绕过此绑定。
`source_reuse_binding/v1` 保留 origin 的 Job、Receipt、ArtifactSet、command slot 和
manifest hash，供独立读者审计。

## 实施顺序

1. 在 Kernel 定义封闭的 Binding 值与 Store 专用提交口；扩展 SourceManifest 读取器，
   仅接受严格的原生 SourcePrep 单成员集或严格的 rebind 双成员集。
2. 在 Pipeline source-prep 包实现重绑定命令：不接触本地视频、不得 probe、不得写新的
   Blob bytes；仅基于已持久化 origin manifest 和显式等价的 `SourceOperationPolicy`。
3. 用真实 PostgreSQL 测试 origin/target 两 Job：目标可读相同 Blob；无 binding、策略不等、
   origin Receipt 伪造、目标 claim 缺失均拒绝；重复执行不创建新 Receipt。
4. 已接入 Pipeline 的显式 `recompute` HTTP Run（第一切片只支持完整 `full_stage`）：新
   control-plane run 先调该 binding，再调 context/VLM；父 Run 与 Receipt 不可写。
   逐集 `selected_only`、预算/hold 和策略变更计划仍留待下一切片。

## 不在本切片中

- 不实现任意 Job 的 Blob 读取 API；
- 不复用旧 VLM SemanticPack 或把 selected-only 结果冒充全剧成功；
- 不自动猜测 API/章节映射，也不重扫 origin 文件路径；
- 不改变旧 `PrepareWholeSeriesSourcesCommand` 的重放语义。
