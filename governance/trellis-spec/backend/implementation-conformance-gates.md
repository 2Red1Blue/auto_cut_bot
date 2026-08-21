# Implementation Conformance Gates

## 1. Scope / Trigger

适用于 authority tree 下所有设计、实现、检查、提交、upstream merge 与推送。目标是让错误无法穿过 task、commit 和 push 三个独立许可边界。

## 2. Signatures

```text
admit_task(manifest, authority, repositories) -> allow | deny
verify_change(manifest, staged_tree, receipts) -> commit_decision
verify_committed_tree(approved_tree, candidate_commit) -> allow | deny
verify_history(remote_snapshot, candidate_commit, policy) -> push_decision
```

## 3. Contracts

- start 前 PRD/design/implement/context 必须真实、完整、读取到 EOF 并绑定 byte hash；seed、TBD、截断内容均不构成许可。
- 单仓任务使用顶层 repository/branch/base/worktree/predecessor；跨仓使用 `repository_refs` 且顶层字段全为 null。
- `forbidden_runtime_import_roots` 与 purpose-scoped `permitted_legacy_read_roots` 分开；审计读取不授予 import/load 权限。
- 所有 pre-commit receipt 绑定精确 base/staged/index tree；commit 后证明 committed tree 与批准 tree 相同。
- push 前重新 fetch，并绑定 remote URL/ref/OID/TTL/protection attestation；commit allow 不能推出 push allow。
- `unknown|not_run|stale|input_mismatch` 全部 deny；已激活 predicate 不能标记 not_applicable。
- 未激活业务 predicate 只能由 authority-locked activation profile 产生 `not_applicable` receipt；task manifest 自填 N/A 不构成证据。
- authority task 的 protected-path 许可只来自 authority-locked `task-authorizations.yaml`；task 自己的 `authority_change` 字段只描述影响，不授予权限。
- 只有 `RuntimeConformanceReceipt` 在 authority-locked activation predicate 尚未启用时可使用 `not_applicable`；Task/Scope/Candidate/Commit/History/Push 等 receipt 的 decision 封闭为 `allow|deny`。
- `verify-change` 必须重新运行 task、scope、reference、reuse、candidate、validation 与 independent-check leaf gate，并以真实 per-repository Git tree OID 闭合 receipt；禁止把 SHA-256 截断或重编码成 Git OID。
- `verify-push` 必须在 commit-tree 等同性后重新调用 authority-approved live provider collector，再扫描 outgoing history；离线/self-reported remote snapshot 只能产生 deny。
- activation/model/protected/remote policy 均从 authority lock 指向的 Git blob 加载。validation 必须由隔离 command runner 执行，checker 必须来自 approved live checker-run collector；collector 不可用时明确 deny，不能降级为 caller JSON。
- Candidate audit 枚举完整 index tree，按 Unicode NFC + casefold 检测路径碰撞、非普通项和全树冲突标记；secret/privacy 内容只扫描新增或修改 blob。Runtime artifact 路径规则必须按锚定 segment 匹配，不能误杀 `auto_cut_bot/session.py` 或 `tests/session/**` 等合法源码。
- 合成敏感样本只由锁定 `SyntheticSensitiveFixtureManifest` 放行：路径必须位于精确 test-fixture root，blob hash、marker 与 `test_fixture` profile 必须同时匹配；`production` profile 对同一 blob 仍 deny。
- Outgoing history 对每个待公开 commit 的新增或修改普通 blob 复用 candidate content scanner；后续删除不能隐藏早期 commit 中的敏感 blob。
- Aggregate gate 必须证明 `IndependentCheckReceipt.checker_command_results_hash == ValidationReceiptSet.command_results_hash`，并在签发前重新读取 task manifest、context bytes/hash 与 index tree，任何 TOCTOU 漂移都 deny。
- authority bootstrap 固定为 A（reviewed sources）→ B（唯一变更 inventory）→ C（唯一变更 generated lock），且 B/C 都必须是单父提交。
- A 之前先对真实 Git index 执行 `verify-source-candidate`；A 不得混入 inventory 或 generated lock。`verify-change` 与 `verify-push` CLI 是最终许可入口，leaf receipt 不能替代 aggregate decision。

## 4. Validation & Error Matrix

| 条件 | 结果 |
|---|---|
| placeholder/空 context/authority mismatch | task deny |
| allowlist 外 diff、protected overlap、symlink escape | commit deny |
| implementer/checker 同 run 或同结论上下文 | commit deny |
| candidate tree 安全但 outgoing history 含 runtime/session artifact | commit 可 allow；push deny |
| upstream capability 无 mapping/disposition | merge deny |
| baseline 失败不可复算或 changed-scope 新失败 | commit deny |
| remote protection 缺失、可绕过或 attestation stale | push deny |

## 5. Good / Base / Bad Cases

- Good：精确 staging、独立 checker、commit tree 等同，再独立扫描 outgoing history 后走 protected PR。
- Base：文档任务仍运行 authority/reference/path/candidate/history gate。
- Bad：使用全仓旧 lint 噪声豁免本任务新增文件错误。
- Bad：当前树删除了 session log 就直接 push，未扫描待推送旧 commit。

## 6. Tests Required

- closed schema 与 unknown field；
- task completeness/context/hash/model role；
- repository binding/path/protected/symlink；
- sync/drift/package shims；
- commit/push 独立状态和 unprotected remote；
- activation profile not_applicable 合法与越权反例。

## 7. Wrong vs Correct

```text
Wrong: broad read -> reuse old implementation -> happy tests -> git add . -> push
Correct: authority/context freeze -> admission -> scoped implementation -> machine gates
         -> independent check -> exact commit -> history/remote audit -> protected PR
```
