# PC real semantic pipeline run

This run is for the first real pass over an authorized series when VLM semantic
evidence is required before calibrated ASR/VAD cutting.  It uses the normal
HTTP control plane and PostgreSQL; it is not a test fixture or a direct script
call.

## What it does

The explicit plan is `semantic_only`:

```text
authorized 50-episode source -> SourcePrep -> Doubao VLM semantic batch -> durable success
```

Every episode VLM request and the whole-batch finalizer write their normal
immutable Artifact/Receipt closure.  The plan cannot instantiate FunASR/VAD,
story stages, physical editing, render/QC, authority bootstrap or publication.
`succeeded` therefore means **semantic evidence complete only**.

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
```

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
uv run auto_cut_bot pipeline-serve --host 127.0.0.1 --port 18766
```

Submit one run with a new idempotency key and the source reference from the
catalog:

```bash
curl -sS -X POST http://127.0.0.1:18766/v1/pipeline/run \
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
- **Move after SourcePrep succeeds:** another machine can start the same Git
  revision with the same semantic authority resource, PostgreSQL DSN, and Ark
  credentials.  The VLM stage re-reads the committed source ArtifactSet and
  proxy Blobs from PostgreSQL; it does not need the PC's original video path.
  The persisted execution-profile hash and request hashes must match.  A code
  or policy mismatch is rejected instead of silently changing the request.
- **Move while SourcePrep is unfinished:** the new host must have the same
  authorized 50-episode source directory mounted and named in its private
  source catalog.  SourcePrep deliberately has no partially committed media
  result, so without that source access it cannot be safely continued.  Do not
  claim that an uncommitted frame scan is portable.

The current HTTP resume implementation only wakes an eligible nonterminal run
with a `media_preflight` command in pending/indeterminate/awaiting-calibration
state. It is **not** a generic VLM or per-episode rerun API; `semantic_only`
runs have no such command. For the supported media-preflight case, use the
current `version` returned by the status endpoint as a concurrency precondition:

```bash
curl -sS -X POST http://127.0.0.1:18766/v1/pipeline/resume \
  -H "Authorization: Bearer <server-api-key>" \
  -H "Content-Type: application/json" \
  --data '{"run_id":"pipeline_run_...","expected_version":<status-version>}'
```

This does not overwrite a terminal success, denial, or failure.  A stale
`expected_version` is rejected, which prevents two hosts from both claiming
the same recovery transition.

### Planned stage / episode recompute (not implemented)

Repeated submit and restart recovery above keep the original frozen command
identity. They do not intentionally regenerate a failed episode under changed
parameters. A new run currently cannot simply consume old source/child Receipts:
the request factory, Blob access and batch readback enforce producer Job ownership.

The [selective recompute design](pipeline-selective-recompute-design.md) adds
explicit producer bindings, compatible-result reuse and a durable inspection
hold. Its proposed `/v1/pipeline/recompute` route is not available yet. Do not
copy old Receipts, clear terminal rows or change the original run's profile as
a workaround.

## Moving to calibrated media evidence

After independent ASR/VAD timing anchors are accepted, create a separate full
run with the normal plan (leave `AUTO_CUT_BOT_PIPELINE_PLAN` unset).  That run
is the only plan allowed to enter MediaPreflight. Cross-run semantic reuse still
requires the explicit binding/reader changes in the design above; it is not an
implemented option of the current `/run` endpoint. Semantic success never means
a cut, render, QC pass or publication permission.
