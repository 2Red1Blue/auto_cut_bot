# v2.1.3 Authority Implementation Design

## 1. Architecture Boundary

```text
auto_cut_bot repository (upstream nanobot fork)
├─ auto_cut_bot/**                  # Agent-Native Runtime / nanobot adapter
├─ packages/autocut-kernel/**      # only shared authority source
├─ autocut_legacy_bridge/**         # optional approved adapters
└─ governance/**                    # contracts, lock, gates, fixtures
                 │ signed/pinned wheel + authority bundle
                 ▼
ac_auto_cut repository
├─ autocut_pipeline_runtime/**       # Pipeline Unattended Runtime
├─ infrastructure/render adapters/**
└─ governance/authority-consumer.lock.yaml # first materialized by child 02
```

Dependency rules:

- Runtime/composition may depend on kernel and installed adapters.
- Bridge may depend on generated kernel contracts/ports and approved legacy modules.
- Kernel must not depend on Runtime, bridge, legacy modules, filesystem paths, environment variables or dynamic loaders.
- Legacy objects terminate inside bridge. Boundary serialization validates a closed generated Schema before any value reaches kernel.
- `auto_cut_bot` owns kernel source and the Agent runtime; `ac_auto_cut` consumes an immutable kernel build and owns only the Pipeline runtime/infrastructure adapters.
- Cross-repository local editable installs are forbidden in CI/release. Development may build from an exact upstream commit into a wheel, but the resulting hash must equal the consumer lock.

## 2. Review Judgment and Corrections

The three proposed guardrails are necessary and compatible with the authority architecture, with two corrections:

1. An `approved_adapter` ledger row does **not** permit `autocut_kernel` to import legacy code. It permits only `autocut_legacy_bridge` to import the exact registered symbols. Kernel-to-legacy is always forbidden.
2. `migrated` does **not** permit importing the old module. It means the capability now lives under a new authority package and callers import the new path; the old path remains forbidden.

Python has no true compile-time module isolation, so “编译期强制” is implemented as three independent gates: AST policy, dependency-graph policy, and an isolated wheel environment without legacy packages. Passing only one is insufficient.

## 3. Import Firewall Contract

Required scans:

- `ast.Import` and `ast.ImportFrom`, including aliases and relative levels.
- writes/calls to `sys.path`, `site.addsitedir`, `PYTHONPATH` mutation helpers.
- `importlib.import_module`, `importlib.util.spec_from_file_location`, `SourceFileLoader`, `__import__`, module `exec/eval` loaders.
- string/path references resolving inside registered legacy roots.
- package metadata and transitive dependency graph.

Policy matrix:

| Importer | Legacy disposition | Result |
|---|---|---|
| `autocut_kernel/**` | any | deny |
| `autocut_legacy_bridge/**` | `approved_adapter` + exact symbol/contract | allow |
| production bridge | banned/fixture_only/algorithm_candidate/migrated/missing | deny |
| tests/fixture tools | fixture_only + declared fixture scope | allow |
| offline evaluation | algorithm_candidate + non-production entrypoint | allow |
| new authority modules | migrated new path | allow new path; deny old path |

The CI check is required before contract/business implementation merges. A test that imports the built `autocut_kernel` wheel in an environment containing no repository checkout prevents accidental transitive reachability.

## 4. Reuse Ledger Contract

Recommended machine source: tracked YAML validated by JSON Schema. Each entry contains:

```yaml
legacy_module: autocut_core.libs.span_compiler
source_hash: sha256:...
disposition: approved_adapter
owner: stable-cut-migration
allowed_importers: [autocut_legacy_bridge.stable_cut]
allowed_symbols: [detect_candidate_regions]
input_schema_hash: sha256:...
output_schema_hash: sha256:...
side_effects: none
determinism: deterministic_for_frozen_inputs
test_ids: [REUSE-SPAN-001, REUSE-SPAN-002]
review_condition: replace_after_exact_compiler_equivalence
```

Missing entry is deny. Broad module globs, `allowed_symbols: ['*']`, unspecified side effects, unbound Schema hashes and expired review conditions are invalid. Ledger update and first permitted import must be in one commit, with ledger validation running before import lint.

## 5. Bridge Contract

Bridge input and output use generated closed DTOs. Allowed outputs:

- immutable scalar/tuple DTOs representing observations or algorithm candidates;
- `ImmutableBlobRef` after bytes/hash/length/media type validation;
- structured diagnostics that do not carry an authority decision.

Forbidden outputs include instances or serialized shapes of legacy ArtifactBus/Stage/Policy, database ORM entities, mutable mappings with undeclared fields, Recipe/Admission/Release objects and status values claiming `pass|ready|allow`.

Bridge errors are `candidate_unavailable|input_rejected|algorithm_failed|indeterminate`; kernel maps them through registered Command/Recovery rules. Bridge cannot silently default, skip or select a business fallback.

## 6. Trellis Authority Model

Phase -1 在 `auto_cut_bot/governance/` 建立唯一 tracked source：

```text
governance/
├── authority-lock.yaml
├── trellis-spec/backend/
├── task-contract.schema.json
├── reuse-ledger.schema.json
├── protected-paths.yaml
└── blocking-fixtures.manifest.yaml
```

工作区根 `.trellis/spec/backend/` 是由 tracked source 单向生成的 operational copy；同步工具禁止反向覆盖真源，CI 对每个文件的 path/hash 做 drift check。

`ac_auto_cut` 只保存 generated consumer lock。child `00` 仅冻结其 closed Schema、generator/verifier interface 与 `not_materialized` readiness receipt，不创建 lock 文件；child `02` 在 exact `auto_cut_bot` commit 上完成 isolated wheel build 后首次物化。该 lock 必须在 Pipeline CI 中从 authority bundle、kernel source/subtree 和 wheel provenance 重建并做 zero diff，不允许 Pipeline 仓库反向修改契约。

lock instance 不存在 `pending|placeholder` 状态。首个实例只能是 `bootstrap_consumable`，只允许 packaging/import smoke；`execution_eligible|shadow_eligible|publication_eligible` 由后续阶段的 closed required receipts 逐级授权。lock 不写 consumer commit 以避免自引用；commit 后另发 `ConsumerLockReceipt` 绑定 lock blob、consumer commit tree与验证结果。

`authority-lock.yaml` 是实现任务的根输入，普通任务只能读取，不能修改。Authority Change 必须使用独立 task type，重算 hash、运行全部 blocking fixtures，并让所有未完成子任务回到 planning 重新绑定。

The user has authorized removal of the old `ac_auto_cut/.trellis/`; it has been removed from the active workspace. Reusable algorithm principles were consolidated into `原理/v2.1-implementation-design/11-compatible-algorithm-principles-and-reuse-boundaries.md`; copying old files wholesale remains forbidden.

## 7. Task Dependency Graph

```text
00 Trellis authority sync
          ↓
02 Import firewall + skeleton ──────> 01 Contract codegen
                                      ↓
03 Store/transaction → 04 Command/Admission/Recovery
                                      ↓
05 Media preflight → 06 Stage 4 exact vertical slice
                                      ↓
07 Stage 1–3 → 08 Render/QC/Release → 09 Dual runtime
                                      ↓
10 Platform certification → 11 Migration/cutover
```

The numeric child names are stable task identifiers, not execution order. `02` depends on `00`; `01` depends on both the `00` AuthorityBootstrapReceipt and the `02` KernelConsumerLockReceipt. Together `00` and `02` complete Phase -1.

## 8. Rollback

- Before enabled publication, rollback means disable the new Runtime deployment and retain immutable authority data.
- Schema/Registry/Reuse Ledger changes are forward revisions, not in-place rollback.
- A failed bridge approval reclassifies its ledger entry to banned and disables dependent capability; it does not rewrite historical observations.
- Publication intents already emitted remain under reconcile ownership even if the application version is rolled back.

## 9. Task Admission and Root-of-Trust Gates

子任务从 planning 进入 implementation 前必须通过：

```text
task completeness
→ authority hash match
→ predecessor commit match
→ allowed-write/forbidden-read policy
→ non-empty implementation/check context
→ protected-path non-overlap
```

实现 diff 每次提交前再检查一次。普通 task 若修改 `production-spec`、authority lock、gate implementation、Schema/Registry source 或 blocking fixtures，无论测试是否通过都必须失败。

单一 AI 同时生成代码和自证测试不属于独立验证。Blocking fixtures、architecture policies 和数据库并发 oracle 位于 protected paths，实现 task 不得改动；check 阶段从独立 manifest 加载它们。

## 10. Runtime Provenance

Authority writer 启动时对比 build 内嵌 authority hash、Schema bundle hash 与数据库 active RegistrySet。任一不同即拒绝写入。RunManifest、CommandReceipt、ArtifactSet 与 Publication evidence 保存这组 provenance，使离线审计可以证明其使用的精确契约。

Pipeline provenance 另外包含 consumer lock blob hash、eligibility profile、`kernel_source_repo/kernel_source_commit/kernel_subtree_hash/kernel_wheel_sha256` 与 build provenance receipt；Agent provenance 包含 `nanobot_upstream_commit/auto_cut_bot_commit`。双 Runtime conformance 只允许在 kernel/authority hashes 相同且两端 eligibility 满足当前操作时比较。`bootstrap_consumable` 永远不能启动 authority writer、提交业务 Command 或触达发布端口。

## 11. Model Assignment Matrix

| Work class | Implement owner | Check owner | Spark allowed |
|---|---|---|---|
| Authority lock, protected gates, contract changes | GPT-5.6 Sol high/xhigh | separate Sol xhigh review + protected CI | no |
| PostgreSQL transaction/CAS/Recovery/publication | GPT-5.6 Sol high/xhigh | separate Sol xhigh + concurrency oracle | no |
| Exact edit compiler/search certificates | GPT-5.6 Sol high | separate Sol/Terra high with blocking fixtures | no for algorithm ownership |
| Frozen-schema adapters, deterministic handlers | GPT-5.6 Terra high | Sol high | only mechanical substeps |
| Fixtures, formatting, links, generated diff triage | Terra or Spark | deterministic CI + owning reviewer | yes |

Model choice is defense in depth, not authority. A Sol implementation still cannot alter its protected oracle, and a Spark leaf change still requires the same path, hash and CI gates.

普通 task 不需要多模型委员会：一个实现 run 与一个只读监督 run 即可。Codex、DeepSeek Harness 或其他 Harness 统一输出 `SupervisorDecision`；authority/high-risk task 才增加一次定向对抗复核。

## 12. Implementation Conformance State Machine

所有 child 统一执行：

```text
planning → ready → implementing → review
review --allow--> accepted → committed
review --repair--> implementing
review --deny--> blocked/replan
```

监督结果绑定 Authority、task context 与 candidate tree，并逐条给出 AC 证据。GitHub/远端 required checks 是可选 push/release 加固，不属于本地 review 状态机。完整契约、错误矩阵与测试在 `ac_auto_cut/原理/v2.1-implementation-design/12-implementation-conformance-and-ai-drift-prevention.md`。

Trellis operational entry 为 `.trellis/spec/backend/implementation-conformance-gates.md`。Phase -1 必须把它同步为 `auto_cut_bot/governance/trellis-spec/backend/` 的 tracked source，并将 gate implementation、receipt schemas 和 negative fixtures 加入 authority lock/protected paths。
