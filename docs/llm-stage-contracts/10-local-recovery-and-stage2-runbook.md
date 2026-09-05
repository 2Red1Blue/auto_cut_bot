# 10 局部恢复与 Stage 2 单独运行

分支：`feat/v213-contract-codegen`。PC WSL 验证目录：`/home/laiu/auto_cut_bot-v213-validation`。
代码通过 Git 同步；私有选择器/配置/debug 不进入 Git。不要覆盖 operational checkout 的未知改动。

## 环境

```bash
cd /home/laiu/auto_cut_bot-v213-validation
export PYTHONPATH="$PWD/packages/autocut-kernel/src:$PWD"
export AUTOCUT_AUTHORITY_REPOSITORY="$PWD"
```

私有环境需要 `AUTO_CUT_BOT_PIPELINE_KERNEL_POSTGRES_DSN`，未设置时回退
`AUTO_CUT_BOT_PIPELINE_POSTGRES_DSN`。Stage 2 实际调用另外需要
`AUTO_CUT_BOT_PIPELINE_ARK_API_KEY`，可设置 `AUTO_CUT_BOT_PIPELINE_ARK_BASE_URL`。
不要把 DSN/key 放在命令行、请求 JSON 或提交记录中。确认连接的是目标业务库；pytest 只能使用独立测试库。

## 1. VLM 已付费响应本地再处理：零 Provider 调用

```bash
.venv/bin/python scripts/reprocess_vlm_evidence.py \
  --mode reprocess --request /absolute/private/reprocess-request.json --dry-run
.venv/bin/python scripts/reprocess_vlm_evidence.py \
  --mode reprocess --request /absolute/private/reprocess-request.json --execute
```

选择器必须由数据库中的确切父记录构造，使用 `ReprocessVlmEvidenceRequest.to_mapping()`：

- 原 Job、command slot、terminal receipt、具体 attempt；
- 原 command request hash、request Blob hash、raw response Blob hash；
- 原 source ArtifactSet、episode_index（从零开始）、原 artifact revision；
- 已安装的完整目标 parser hash；不能自己填写任意 hash 放行；
- v2 明确 `projection_version=2`，strategy 为 `reprocess-vlm-evidence-v2`。

旧 v1 JSON/构造调用继续保持 v1 身份。v2 为 Stage 3 额外保存原请求中已核验的
request_identity、parse_policy、proxy_blob。它产生新的 Receipt，不覆盖已提交 v1。
三个无序 enum 集合可以排序；未知引用、缺事实、截断 JSON、虚构关系都不在自动修复范围。
非法父选择器在 claim 前拒绝；合法父响应的真实语义错误可以产生新的拒绝 Receipt。

默认不传 `--execute` 就是只读。dry-run 输出候选 hash，不给假的 Receipt。
成功执行输出完整 `members[]` 引用，第一项为派生 provenance，第二项为 semantic pack。
同一个 request/key 重复执行只重放确定性结果，不调用模型。

## 2. 完整批次进入普通语义链

将每一集的确切 generation/derivation provenance 放入 `FinalizeDerivedVlmBatchRequest.children`，
用 `to_mapping()` 保存独立 batch-request.json：

```bash
.venv/bin/python scripts/reprocess_vlm_evidence.py \
  --mode finalize-batch --request /absolute/private/batch-request.json --dry-run
.venv/bin/python scripts/reprocess_vlm_evidence.py \
  --mode finalize-batch --request /absolute/private/batch-request.json --execute
```

顺序必须与原 SourceManifest episode census 完全一致。少集、重复、跨源/跨 Job、混用不兼容冻结策略都拒绝。
不能把恢复一集解释为整批完成。批次成功后返回的 member 引用可作为普通
`CommittedSemanticInputsRequest.vlm_semantic_pack_set`，由 Store 独立重读后供 Stage 1 使用。
派生 v2 的 Stage 3 reader 解码完整 V4，不伪造 generation，也不丢弃视频支持字段。

## 3. 只运行 Stage 2，不重跑 VLM/Stage 1

```bash
.venv/bin/python scripts/run_stage2_request.py \
  --request /absolute/private/stage2-request.json \
  --debug-root /absolute/private/debug/stage2_portfolio --dry-run
.venv/bin/python scripts/run_stage2_request.py \
  --request /absolute/private/stage2-request.json \
  --debug-root /absolute/private/debug/stage2_portfolio --execute
```

输入为 `CompileStoryPortfolioRequest.to_mapping()`，必须绑定完整冻结 Stage 1 request 与成功 outcome，
不是从 debug 拼凑的裸 NarrativeGraph。新 compact 请求选择完整新生成策略与明确新 key；旧 v1不悄悄改写。
本入口实际执行要求 `retry_policy.max_attempts=1`，只调用现有 Command 一次；未知远端结果保持 running，
再次检查必须使用同一 request/key，不因超时换 key。不存在成功的旧 Stage 1 就不能跳过它的验证。

每次 CLI 调用在 debug-root 下新建私有 invocation UUID 目录，按 stage/operation/model 保存输入输出；
dry-run 不调用 Provider。操作入口复用同一 Kernel，不是新的强流水线，也不表示新增了 HTTP recovery endpoint。

## 4. 退出码与后续动作

| 返回 | 动作 |
|---|---|
| 0 | dry-run 验证通过，或执行 succeeded；二者看 mode/status 区分 |
| 1 | 明确 denied/failed 或可识别解析拒绝；查看具体原因，不重跑上游掩盖 |
| 2 | 输入/配置/实现不可用；先修原因，不盲发模型 |
| 3 | pending/running、执行异常或成功后报告不完整；查询相同 request/Receipt，不能按“没写入”处理 |

恢复 CLI 的 `succeeded_reporting_incomplete` 明确保留 command_state=succeeded 与原 Receipt，
表示后续报告/引用读取失败，不表示数据库回滚。所有异常输出只带安全类别，不回显密钥或原始异常全文。

## 5. 真实证据与仍需完成的验收

- 原三次失败响应已在 PC 真实库核对。前两次排序后仍有语义闭包问题；第三次两处集合重排后完整解析通过，0 新 Provider 调用。
- v1 派生 Receipt `17c42cee-03d3-4410-9b3b-f6e07a050be0` 已重放确认，原 Attempt 不变。
- 显式 v2 派生 Receipt `be57f6b7-2cc5-44df-9744-2b0a3a92f010`，ArtifactSet `a4e6bc21-fb51-4855-9945-8a55e3e0be56` 已提交。
- 该旧 run 的 SourceManifest 包含多集；只提交恢复的一集进行批次封装时被正确拒绝，未写半批成功。
- PC 已有的成功 Stage 1 使用 VLM parser hash `sha256:9b285e4344ab1838573eae26f041b9553308510413fd8cca3722072ec9248630`；
  当前安装的 strict V4 hash 是 `sha256:62da60351553645f011240cbe77e19e445b1136cae0ee5c6844a92e32531a420`。
  exact Store reader 返回 `PARSER_IMPLEMENTATION_UNAVAILABLE`，因此没有执行新的付费 Stage 2。
- 已静态定位精确旧 bundle：提交 `40bde03392f4842ef6c0ca1bf96e05f1dbc467f2`（`eb2358b7^`），
  九成员按原算法算得上述 `9b285e...`。相对当前只有 `vlm/parser.py` 不同：`_canonical_enums`
  从拒绝无序改为自动排序，且 V4 真实调用此 helper。旧 blob 为 `94510c85dc651006f3a268811ade9dace09ebdd3`，
  源码 SHA256 为 `45e634be1432258f10ccdd115316b611318e1ac5eecd6a9b60fd31588e659dd7`。
  可用 `git show 40bde03392f4842ef6c0ca1bf96e05f1dbc467f2:packages/autocut-kernel/src/autocut_kernel/vlm/parser.py`
  查看；这是静态恢复依据，不代表旧执行环境已经安装或验收。
- 下一步隔离恢复并验证该历史 parser 的精确实现依赖，或设计独立、可审计的已提交上游迁移；
  不能只把旧 hash 加入白名单调用新 parser，也不能为了通过验证重写旧成功 Receipt。
- Stage 2 的 v1→v2 纯草案迁移已实现；持久化 Stage 2 派生命令仍待实现，不以纯函数结果冒充新准入。
  HTTP 默认切换、单集真实 Stage 2 输出质量验收及视频语义人工对照尚未完成。详见 [09](./09-model-boundary-refactor.md)。
