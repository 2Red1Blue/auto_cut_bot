# Design — Ordered Local E2E Parent

## Parent/child admission model

本目录不是实现 allowlist。JSONL 仅为 Context Loader 的 source list，不能充当 admission、
check 或 pass oracle。每 wave 先创建一个 child task；Context Loader 完整读取 child JSONL
指定的每个直接文件与每个 reference-closure member，并为每个 member 记录 canonical path、
byte_length、UTF-8 EOF completion、full-file SHA-256、实际注入段的 stable anchor/range 与
slice SHA-256。生成的 TaskSnapshot 还必须绑定 authority、context、base、repository、
allowed-write-paths、candidate tree、gate-bundle、toolchain 和 Supervisor-contract hashes。
任一输入变化均使 Snapshot/decision stale；无 Snapshot、slice/reference closure、exact
owner mapping 或 active-owner conflict 时 deny，不派 agent。closure 超预算时拆分 child，
不得以摘要或省略错误规则继续。

Child worktree 是独立的，且仅绑定一个 repository。跨仓工作拆成相互引用 commit 的 child；
不能用 parent task 的 broad package scope 掩盖跨仓写入。所有 task start/review/finish 都
重算 Snapshot；任何 source/context/candidate/toolchain 改变都会使旧 decision 失效。

## Waves and ownership

| Wave | Child boundary | Exact repo-relative allowlist | Dependency | Review |
| --- | --- | --- | --- | --- |
| 0 | authority bootstrap closure | `governance/**`, `packages/autocut-kernel/src/autocut_kernel/registry/**`, matching tests; only if 08-25 owner authorizes | 08-25 | authority/bootstrap adversarial |
| 1 | SourcePrep partition | `packages/autocut-kernel/src/autocut_kernel/pipeline/source_prep/**`, `tests/pipeline/source_prep/**` | Wave 0 accepted | leaf deterministic |
| 2A | Ark semantic evidence | `auto_cut_bot/pipeline/providers/ark/**`, `tests/pipeline/ark/**` | Wave 1 | leaf deterministic |
| 2B | timed physical evidence | `packages/autocut-kernel/src/autocut_kernel/media/**`, `packages/autocut-kernel/src/autocut_kernel/pipeline/timed/**`, matching tests | Wave 1 | leaf deterministic + indeterminate adversarial |
| 3 | Stage 1–3 Blueprint | `packages/autocut-kernel/src/autocut_kernel/stages/stage1_3/**`, `tests/stages/stage1_3/**` | 2A and 2B committed | leaf deterministic |
| 4 | Stage4 exact edit | `packages/autocut-kernel/src/autocut_kernel/stages/stage4/**`, `tests/stages/stage4/**` | Wave 3 | leaf deterministic |
| 5 | Render/QC/output | `packages/autocut-kernel/src/autocut_kernel/render/**`, `auto_cut_bot/pipeline/output/**`, matching tests | Wave 4 | leaf deterministic |
| 6 | Task09 authority/conformance | child allowlist owned by Task09 only | Wave 5 | Task09 Supervisor |
| 7 | one-episode E2E integration | `tests/e2e/local_one_episode/**` only; shared files only by integration owner after prior tasks inactive | Wave 6 | integrated Supervisor + final E2E adversarial |
| 8 | 45-episode scheduler | `packages/autocut-kernel/src/autocut_kernel/pipeline/rollout/**`, `tests/e2e/rollout/**` | Wave 7 | integrated Supervisor |

Paths are proposals, not permission to invent missing modules: a child snapshots actual paths before start; a predecessor with active ownership wins and forces wait/replan. `composition/**`, `models/**`, PostgreSQL migrations/store, package exports and stage-plan files are never parallel ownership; the serial integration owner edits them only after all related child tasks are inactive and their commits are accepted.

## Effect and recovery state machine

```text
unclaimed -> durable intent -> dispatch -> observation/reconcile -> complete set + Receipt
                 |                |                 |
                 |                |                 +-- unknown -> original Attempt indeterminate
                 |                |                                      -> Admission-authorized Recovery -> reconcile
                 |                +-- no dispatch proof -> retryable only
                 +-- Store/CAS exception -> propagate to Store recovery
```

`no dispatch proof` includes no emitted request/body, no external identity and no committed effect intent.
After dispatch, every retry/reconcile retains the original request/transaction identity. A successor cannot act
without a durable RecoveryCatalog/Admission authorization and exact ledger reservation. Neither provider result,
control-plane state nor local pointer can promote unknown to success.

## Execution partitions and resource contract

An `ExecutionPartition` is immutable and committed before provider invocation. It contains ordered episode refs,
partition kind (`baseline_1`, `rollout_3`, `rollout_9`, `rollout_32`), profile/policy/authority/build hashes,
predecessor receipt refs, exact provider budget and declared workset. Scheduler validates:

```text
baseline_1 cardinality = 1
baseline_1 ∩ rollout_3 ∩ rollout_9 ∩ rollout_32 = empty pairwise
baseline_1 ∪ rollout_3 ∪ rollout_9 ∪ rollout_32 = frozen 45-member set
```

Before rollout, a baseline receipt proves all other member provider counters equal zero. FunASR identity records
one service, one loaded AutoModel, one inference permit and three queue permits; profile budgets freeze max
instances=1, RSS, swap, staging disk, request/response bytes and queue capacity. Higher throughput is a new
calibration/profile task, not a runtime knob.

## Trust and isolation matrix

| Surface | Allowed | Denied |
| --- | --- | --- |
| Network | locked Ark endpoint; loopback FunASR | every other egress, including publication |
| Runtime | typed Gateway/Query/Recovery | Store write, provider/renderer/platform ports |
| Legacy | declared read-only Reuse Ledger root/purpose/hash | any Kernel import or implicit reuse |
| Output | receipt/set/hash verified reader | directory scan, orphan blob/pointer, partial set |
| Secrets | ignored local environment/secret broker | Git, task docs, receipts, artifacts, traces/logs |
| Publication | deny-on-call port, zero outbox | source, credential, target, transaction/effect |

## Review matrix

| Scope | Deterministic checks | Human/agent review |
| --- | --- | --- |
| Each leaf | schema, diff scope, import firewall, unit/PostgreSQL focused tests, ruff, basedpyright | none beyond child owner before local commit |
| Authority/bootstrap | leaf checks | one targeted adversarial review |
| Timed unknown/recovery | leaf checks + crash/ack-loss matrix | one targeted adversarial review |
| Integrated one-episode candidate | all required leaf evidence + receipt walk | one read-only Supervisor, then final E2E adversarial |
| 45 rollout | partition algebra, provider counters, restart/replay matrix | same integrated Supervisor delta review |

No task self-certifies: `CheckReport` is runner-bound; the read-only Supervisor emits AC pass/fail/NA only for the
exact candidate. NA needs contract evidence. A repair modifies one owner child, makes a new commit and invalidates
the integration decision; two repairs per fingerprint, then deny/replan.
