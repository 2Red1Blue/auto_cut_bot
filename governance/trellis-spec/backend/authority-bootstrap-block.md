# Scenario: v2.1.3 Authority Bootstrap Block

## 1. Scope / Trigger

任何 backend、contract、database、Runtime、Stage、adapter 或发布路径变更都触发本门禁。Phase -1 仅允许冻结契约、Schema source、CI guardrail 和授权任务自身的治理代码。

## 2. Signatures

```text
admit_task(TaskConformanceManifest, AuthorityLock, RepoSnapshot)
  -> TaskAdmissionReceipt(allow|deny)
check_change_scope(TaskConformanceManifest, GitIndex)
  -> ChangeScopeReceipt(allow|deny)
```

未来业务入口只能是 `autocut_kernel.application.CommandDispatcher.dispatch(...)`，不得以旧 `Stage.execute()`、ArtifactBus、文件 path 或 ORM repository 作为新入口。

## 3. Contracts

- 权威 tracked source 是 `auto_cut_bot/governance/`；根 `.trellis/` 是单向 operational copy。
- `auto_cut_bot` 与 `ac_auto_cut` 是两个独立 Git repositories；跨仓 task 只能使用 `repository_refs`，顶层单仓 branch/base/worktree 字段必须为 null。
- PostgreSQL `autocut_authority`、ArtifactSet/CAS、Command/Receipt、Admission/Recovery、双 Runtime、import firewall 和 Reuse Ledger 均来自 v2.1.3 权威契约。
- 未声明字段、默认、fallback、动态 import、Stage 私有状态和私有写路径均禁止。
- scope 必须由每个绑定仓库的 Git index 相对 predecessor commit 计算；caller 不得提交或删减 changed-path 列表。
- authority lock 的初次冻结使用无自引用的 A→B→C：A 只提交受审 source；B 只提交 inventory manifest，其中 `seed_source_commit=A`，而 B 的 OID 由 lock builder CLI 参数提供；C 只提交从 `B:manifest` 与 A 的 Git blobs 生成的 lock。builder/validator 不读取 dirty worktree bytes。

## 4. Validation & Error Matrix

| 条件 | 结果 |
|---|---|
| task 不属于授权 tree 或 authority/context hash 不符 | stop |
| ordinary task 触达 protected path | commit deny |
| operational Trellis 与 tracked manifest 不同 | drift deny |
| source/destination 含 symlink escape | sync deny |
| remote 无 protection/rulesets/CODEOWNER required checks | push deny |

## 5. Good / Base / Bad Cases

- Good：authority change 在隔离 worktree 中更新 tracked source、lock、negative fixtures 和 gate。
- Base：普通实现任务只读 authority lock，并在授权路径中实现冻结契约。
- Bad：直接编辑根 `.trellis/spec` 并反向复制到 tracked source。
- Bad：为了复用旧算法先 import legacy，之后再补 ledger。

## 6. Tests Required

- task manifest closed schema、placeholder/context/hash/predecessor/model-role 负例；
- protected path、broad glob、path traversal、symlink escape 负例；
- tracked → operational 同步幂等和 operational drift 负例；
- single/cross repository binding 与 package routing shim 校验；
- 未保护 remote 的 push admission 必须 deny。

## 7. Wrong vs Correct

```text
Wrong: edit operational spec -> code from old patterns -> self-approve -> push
Correct: freeze tracked authority -> task admission -> scoped diff -> independent check
         -> commit allow -> history/remote audit -> push allow
```
