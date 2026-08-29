# PC real semantic pipeline run

This run is for the first real pass over an authorized series when VLM semantic
evidence is required before calibrated ASR/VAD cutting.  It uses the normal
HTTP control plane and PostgreSQL; it is not a test fixture or a direct script
call.

## What it does

The explicit plan is `semantic_only`:

```text
authorized 50-episode source -> SourcePrep -> Context Prepare -> Doubao VLM semantic batch -> durable success
```

Every episode VLM request and the whole-batch finalizer write their normal
immutable Artifact/Receipt closure.  The plan cannot instantiate FunASR/VAD,
story stages, physical editing, render/QC, authority bootstrap or publication.
`succeeded` therefore means **semantic evidence complete only**.

The installed semantic-only policy now selects
`vlm-semantic-pack-v15-context-assisted-required-empty-array-core`: the VLM receives the attached video,
the closed semantic-output contract, and only a bounded, immutable
`WindowContextPack` produced by the immediately preceding Context Prepare
stage. It still never receives ASR/VAD/subtitle text, API shot/highlight lists,
frame tables or physical-cut endpoints. V15 retains the V8 core-observation
shape and V4 parser, but binds a stricter response schema (`uncertainty_ms <=
5000`) and a final model-side check for required fact fields, closed event
enums, fact/event support overlap and complete JSON. The optional reciprocal
causal arrays and multi-segment temporal narration must be explicitly empty;
they remain required schema fields because Ark can omit fields whose item limit
is zero. They are redundant model-maintained views, while admitted facts/events
remain intact for later deterministic semantic derivation. It also uses a probe-first,
bounded-parallel-3 batch policy. The earlier
parallel-10 policy was deliberately exercised against Ark and produced real
429 responses; its historical request/profile bytes remain replayable, but it
is not used for new V15 runs. The parser and 32768 output-token budget remain
unchanged.
Historical v3 requests retain their original prompt bytes and hashes on replay.
Changing the prompt is not permission to reopen a failed run. See the
[Mac real-run record](mac-semantic-run-20260828.md) for the observed failure.

The semantic-only authority additionally selects adapter
`doubao-ark-files-responses-stream-v5` with explicit `thinking_type=disabled`.
This maps to Ark `thinking.type`; it is frozen in the request/profile and reuse
identity, not injected from an environment default. v2/v3/v4 request bytes and
their replay behavior remain unchanged. Files uploads can still reuse v4 cache
entries because upload bytes/MIME/purpose are identical; semantic outputs from
different thinking modes are not interchangeable.

> Ark Files/Responses API integration changes are governed by
> [Ark Responses SDK 调用设计](ark-responses-sdk-integration-design.md). In
> particular, do not add guessed tenant/project headers or change SDK polling
> behavior in a local environment file: the current runtime has not yet
> implemented `ArkRequestScope/v1`.

Before enabling V15, stop all older Pipeline workers and apply every pending
Kernel migration in numeric order, including `0040`. The v10 profile has a
closed five-field parameter variant for v5; later VLM prompt registrations
(`0030` onward) are also required for their corresponding authority profile.
Full-pipeline is not widened.
The outbox does not yet partition workers by supported adapter version, so do
not run pre-v5 workers against new v5 work. No historical rows are rewritten.

## Required private configuration

Keep credentials and host paths outside Git.  In the PC private environment
file, set these exact variables:

```text
AUTO_CUT_BOT_PIPELINE_PLAN=semantic_only
AUTO_CUT_BOT_PIPELINE_POSTGRES_DSN=...
AUTO_CUT_BOT_PIPELINE_SOURCE_CATALOG=...
AUTO_CUT_BOT_PIPELINE_ARK_API_KEY=...
AUTO_CUT_BOT_PIPELINE_ARK_TENANT_ID=...
AUTO_CUT_BOT_PIPELINE_ARK_PROJECT_ID=...
AUTO_CUT_BOT_PIPELINE_ARK_MODEL_ID=doubao-seed-2-1-pro-260628
AUTO_CUT_BOT_PIPELINE_ARK_MAX_OUTPUT_TOKENS=32768
AUTO_CUT_BOT_PIPELINE_METADATA_API_BASE_URL=https://<metadata-api-origin>
AUTO_CUT_BOT_PIPELINE_METADATA_API_KEY=...
AUTO_CUT_BOT_PIPELINE_CONTEXT_OWNER_MAPS_JSON={"series_external_id":"42000021919","mappings":[...]}
```

这三项 Metadata 配置仅用于**第一次创建** API-assisted Context Pack。对已存在、且
`context_prepare` 已成功提交的 `run_id`，在另一台机器或本机重启时可以不设置它们：
运行时只从 PostgreSQL/Blob 读取相同 Pack，绝不再次请求 Metadata API。不要在“续跑”
机器上为了凑配置而填写新的 API key 或猜测 episode map。

`AUTO_CUT_BOT_PIPELINE_CONTEXT_OWNER_MAPS_JSON` is not a filename/order
matcher. Each entry must explicitly declare `local_relative_path`,
`local_episode_index`, `external_episode_id`, `external_chapter_id` (or null),
and `external_episode_ordinal`. SourcePrep derives the actual source ID and SHA-256;
the next stage writes a verified binding from that identity. The configuration
and API credential stay private and are never placed in the HTTP `/run` body.

### Context Prepare 的实际产物

For every run the stage persists one committed `window_context_pack_set`, which
contains one hash-bound Pack per local source episode. A successful API-assisted
Pack records: Snapshot identity, raw immutable JSON Blob reference and hash,
normalized narrative-context hash, verified explicit binding hash, selection
policy hash, selected non-spoiler refs, and the final compact `rendered_context`.
The raw API body, API key, Authorization header, subtitles, shots and highlights
are not in the Pack or prompt. If fetch/normalization/mapping cannot close, the
same stage still commits a `video_only` Pack with a reason code; VLM can run but
has no external narrative text. Historical VLM replay reads this committed Pack,
never refetches the API.

### 分阶段调试文件（建议真实验证时启用）

若要按运行和阶段查看输入、输出、异常以及实际模型请求/响应，额外设置一个**仓库外**的绝对目录：

```text
AUTO_CUT_BOT_PIPELINE_MODEL_DEBUG_DIR=/mnt/d/code/auto_cut/debug/model-io
```

每个已执行的阶段都会保存到 `<debug-root>/<run_id>/<stage>/`：固定的 `input.json` 与 `output.json`，阶段未捕获异常时的 `error.json`，以及模型调用的 `model/<provider>/<call>/request.json`、`terminal.json`、`raw-output.bin`（有原始输出时）。这覆盖 source prep、VLM、media preflight 与后续 Narrative/Portfolio/Blueprint 阶段。它们是可删除的调试镜像，不是重跑、准入或发布依据；请求中的 API key、Authorization、Token/Cookie 和视频字节会被排除或脱敏。目录必须在仓库外；未设置时完全关闭，不影响正常运行。

若要在真实流程中先查看首个 VLM 请求与响应，再允许批量剧集继续，启动前额外设置 `AUTO_CUT_BOT_PIPELINE_VLM_STOP_AFTER_PROBE=1`。Pipeline 仍会完成真实 SourcePrep，并用正常的 Kernel idempotency key 调用第 1 集 VLM；成功后会保持 VLM 命令为 `indeterminate`，不生成 Batch Receipt。**这是当前服务进程的开关，不是持久化的任务暂停许可**：只有全部可能领取该 run 的 worker 都启用它，才能确保其他集不继续执行；同数据库的另一进程若未设置变量，可以继续该 run。不要把此开关用于未经核对的多服务实例暂停。

启用开关的 worker 轮询只读取该探针的持久化结果，不会重复调用模型。查看 `<debug-root>/<run_id>/vlm/model/.../request.json`、`terminal.json` 与 `raw-output.bin`（存在时）后，重启服务时移除此变量（或设为 `0`）；**仅当原 run 仍是可恢复的非终态且探针成功时**，启动重建才会 reconcile 同一 run，复用首个调用后进入并发批处理。探针已失败/拒绝的 run 不会因此重开。

The closed source catalog must contain exactly one authorized entry for book
`42000021919`, with `expected_source_count: 50` and
`authorized_purposes: ["semantic_analysis"]`.  Its policy hash must be the
hash of that exact entry; the server rejects a caller-selected path or a count
that differs from the catalog.

Set `AUTO_CUT_BOT_PIPELINE_API_KEY` in the private environment. It authenticates
only this Pipeline HTTP control plane; it is not an Agent/chat-model credential.

## Start and submit

From the PC v2.1.3 worktree in WSL, install the API extra once, load the
private environment, then start the Pipeline-only server on loopback:

```bash
uv sync --extra api
export PYTHONPATH="$PWD/packages/autocut-kernel/src${PYTHONPATH:+:$PYTHONPATH}"
uv run auto_cut_bot pipeline-serve --host 127.0.0.1 --port 18769
```

The root wheel bundles `autocut_kernel` for deployment.  A worktree development
run must prepend its authoritative Kernel source as above, otherwise an older
bundled copy in an existing virtual environment can shadow the checked-out
Kernel code.  A built wheel does not need this override.

On a native Windows host, run the semantic-only service through its installed
console script (rather than `uv run auto_cut_bot`, which can resolve the
same-named local module incorrectly in Git Bash):

```bash
.venv/Scripts/auto_cut_bot.exe pipeline-serve --host 127.0.0.1 --port 18769
```

This is intentionally limited to semantic SourcePrep/Context/VLM execution.
Physical media materialization uses a cross-process POSIX advisory-lock ledger,
and local rendered-output promotion additionally depends on descriptor-relative
no-follow filesystem operations. Native Windows rejects either later physical
operation with a clear infrastructure failure rather than replacing it with an
unsafe process-local lock or path-based promotion. Run calibrated ASR/VAD,
media-preflight materialization and local output promotion in the verified
Linux/WSL/container runtime until equivalent Windows mechanisms are separately
designed and verified.

Submit one run with a new idempotency key and the source reference from the
catalog:

```bash
curl -sS -X POST http://127.0.0.1:18769/v1/pipeline/run \
  -H "Authorization: Bearer <server-api-key>" \
  -H "Idempotency-Key: semantic-42000021919-001" \
  -H "Content-Type: application/json" \
  --data '{"profile":"shadow","source_reference":"<catalog-authorization-id>"}'
```

Poll the returned `run_id` through `GET /v1/pipeline/status?run_id=<run_id>`.
Do not reuse an idempotency key with a different request.  A VLM provider
failure creates the existing precise receipt/indeterminate state; it does not
silently skip an episode.

## Re-run, restart, and move to another machine

The PostgreSQL database is the execution authority.  It holds the run request,
frozen execution profile, source-preparation ArtifactSet, immutable proxy
Blobs, per-episode VLM request identity, provider attempt identity, and every
terminal Receipt.  The HTTP process and the machine that happens to run it are
not success authority.

- **Repeat a submission:** submit exactly the same body with the same
  `Idempotency-Key`.  It returns the original `run_id` and re-enqueues durable
  work; it never creates another run.  A changed request must use a new key and
  creates a deliberate new run, leaving the earlier Receipt history intact.
- **Restart on the same machine:** start `pipeline-serve` again with the same
  PostgreSQL DSN.  Startup reconstructs accepted/running/indeterminate runs
  from the durable outbox.  A stale command lease becomes indeterminate and is
  reconciled from the existing Command/Provider identity rather than blindly
  submitting a second VLM request.
- **Move after Context Prepare succeeds:** another machine can start the same Git
  revision with the same semantic authority resource, PostgreSQL DSN, and Ark
  credentials.  The VLM stage re-reads the committed source ArtifactSet,
  Context PackSet and proxy Blobs from PostgreSQL; it does not need the PC's
  original video path, Metadata credential or episode map.
  The persisted execution-profile hash and request hashes must match.  A code
  or policy mismatch is rejected instead of silently changing the request.
- **Move while SourcePrep is unfinished:** the new host must have the same
  authorized 50-episode source directory mounted and named in its private
  source catalog.  SourcePrep deliberately has no partially committed media
  result, so without that source access it cannot be safely continued.  Do not
  claim that an uncommitted frame scan is portable.

HTTP resume wakes an accepted/running run when it already has a pending or
indeterminate command, including VLM in a `semantic_only` run. It re-enqueues
the existing work without replacing its request, profile or Receipt. A run
awaiting calibration still only wakes the media-preflight command. It is
**not** a terminal-stage or per-episode recompute API. Use the current `version`
returned by the status endpoint as a concurrency precondition:

```bash
curl -sS -X POST http://127.0.0.1:18769/v1/pipeline/resume \
  -H "Authorization: Bearer <server-api-key>" \
  -H "Content-Type: application/json" \
  --data '{"run_id":"pipeline_run_...","expected_version":<status-version>}'
```

This does not overwrite a terminal success, denial, or failure.  A stale
`expected_version` is rejected, which prevents two hosts from both claiming
the same recovery transition.

### 完整 VLM 阶段重跑（已实现）

`POST /v1/pipeline/recompute` 现在支持一个保守的首切片：从一个**终态**
`semantic_only` run 创建新的完整 VLM run。新 run 先由 Kernel 的
`BindWholeSeriesSourcesCommand` 绑定旧 SourcePrep 的精确成功 Receipt，再入队；它不读取
原机器视频路径、不复制视频字节，也不放宽按 Job 的 Blob 读取检查。新 Run 使用新的 VLM
Command/Attempt/Receipt，因此它是真正重跑，绝不改写父 Run。

首切片的限制是有意的：当前运行时的冻结 execution profile 必须与父 Run **完全相同**，且
该 semantic-only runtime 不得配置外部 Metadata Context（只允许 `video_only` Context Pack）。
这避免动态 API 快照或模型策略变化被错误地称为“兼容重跑”。它也只接受 `full_stage`，不会
把单集结果伪装成整剧 VLM Batch。

```bash
curl -sS -X POST http://127.0.0.1:18769/v1/pipeline/recompute \
  -H "Authorization: Bearer <server-api-key>" \
  -H "Idempotency-Key: recompute-42000021919-001" \
  -H "Content-Type: application/json" \
  --data '{
    "base_run_id":"pipeline_run_...",
    "expected_version":<base-status-version>,
    "stage":"vlm",
    "completion_scope":"full_stage"
  }'
```

同一请求与同一幂等键返回同一目标 Run；同一键换父 Run/version 会返回冲突。若版本、策略、
父 Run 状态或 SourcePrep Receipt 不闭合，服务拒绝创建可调度的目标 Run。不要复制旧
Receipts、清空终态行或改写父 profile 作为替代方案。

### 计划中的逐集重算

`selected_only`、改变 VLM 策略后的局部试跑、完整批次补齐、可持久化 inspection hold 以及
lineage 预算 CAS 仍未实现。它们需要独立的 partial-result Aggregate，不能复用当前完整
Batch finalizer。详见 [selective recompute design](pipeline-selective-recompute-design.md)。

## Moving to calibrated media evidence

After independent ASR/VAD timing anchors are accepted, create a separate full
run with the normal plan (leave `AUTO_CUT_BOT_PIPELINE_PLAN` unset).  That run
is the only plan allowed to enter MediaPreflight. Cross-run semantic reuse still
requires the explicit binding/reader changes in the design above; it is not an
implemented option of the current `/run` endpoint. Semantic success never means
a cut, render, QC pass or publication permission.
