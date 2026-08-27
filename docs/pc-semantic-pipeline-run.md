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
AUTO_CUT_BOT_PIPELINE_ARK_MAX_OUTPUT_TOKENS=16384
```

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

## Moving to calibrated media evidence

After independent ASR/VAD timing anchors are accepted, create a separate full
run with the normal plan (leave `AUTO_CUT_BOT_PIPELINE_PLAN` unset).  That run
is the only plan allowed to enter MediaPreflight; it may explicitly reuse the
completed semantic closure, but it never converts this semantic result into a
cut, render, QC pass or publication permission.
