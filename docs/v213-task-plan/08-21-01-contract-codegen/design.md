# Contract Codegen Design

> 2026-08-26 范围更正：本设计描述通用完整 Registry，不控制当前本地 profile 编译路径。
> 以下已删除总契约的引用仅为历史依据，不再授予实现或运行权限；当前本地边界见
> `08-25-lock-real-test-authority-profiles/design.md` 的 Local profile compiler scope correction。
> 不修改通用 Registry 的完整性判断，也不将局部 profile identity 冒充其 readiness。

## Authority and package boundary

`v2-production-system-contracts.md` 与四份 Stage 文档是唯一业务权威。它们不能由 parser 在运行时“猜测”结构；每个可执行条目先被人工逐条转录到版本化机器源，转录记录含 `contract_path`、source document SHA-256、section anchor 与 reviewer。编译器只接受这些 machine sources。

实现落在已建立的独立 wheel：

```text
packages/autocut-kernel/
  src/autocut_kernel/contracts/
    source/2_1_3/{common,commands,stage_01,stage_02,stage_03,stage_04,stage_05,publication}/
    generated/2_1_3/
    compiler/
    public.py
```

`source/` 是唯一可编辑输入；`generated/` 完全由 compiler 写出并在 CI 重建比对。Agent/Pipeline Runtime 只能 import `autocut_kernel.contracts.public`，不能读取 source 或任何 legacy contract package。

## Source model and compilation

每一个 schema source 使用 JSON Schema 2020-12 兼容的封闭 JSON/YAML，禁止在 schema 内嵌 Python 表达式。每一个 registry entry 使用封闭 YAML，至少带 version、owner、`contract_path`、schema/trace/test refs。编译固定执行：

```text
validate source meta-schema
→ canonical JCS bytes
→ resolve local refs and reject cycles/dangling refs
→ validate registry uniqueness/ownership/trace closure
→ emit JSON Schema 2020-12 bundle
→ emit typed Pydantic public models
→ emit immutable RegistrySet accessors
→ emit manifest(source hashes, generated hashes, compiler version)
```

生成 Pydantic 时必须以 JSON Schema 为输入或同一 typed IR 为输入；不允许手工模型与 Schema 双写。所有 model configuration 设为 extra-forbid，所有 enum/union 来自 source。时间 primitive 只允许 decimal-string tick 加 `{numerator,denominator}` time base，不定义 float second field。

## Registry closure

RegistrySet 由 `artifacts.yaml`、`commands.yaml`、`rules.yaml`、`strategies.yaml`、`traces.yaml` 组成。编译期必须验证：

- ID 在同类 registry 中唯一，Artifact owner/scope 唯一；
- 每个 schema ref、Rule evaluator、Command request/result、Strategy、test ID 与 rollout gate 存在；
- 每个 required Rule/Command/Artifact/state transition 有 Trace；
- Rule 明确 `rule_class`、`indeterminate_allowed`、`on_fail`、`on_indeterminate`、Recovery/exhaustion；
- 规则、参数、引用和 discriminator 都不是开放字符串或隐式默认。

加载 generated RegistrySet 时重新验证 manifest hash；不完整、hash 不一致或任何闭合失败使 readiness=false。

## Delivery decomposition

每个 source pack 以完整 owner/trace 闭合为最小提交单位，顺序为 foundation → common/system → stage 01/02 → stage 03/04 → stage 05/publication → global conformance。一个 Stage 若尚未具备全量 source/fixture 映射，RegistrySet 必须标为 incomplete，不能被 runtime 选中或被标记 release-ready。

## Verification

确定性 checks：source meta-schema、canonical reproducibility、generated diff、reference scanner、registry closure、negative fixtures、wheel-only import。语义 checks：规范 example/negative case 对同一 schema 的接受/拒绝，以及独立 reviewer 逐条核查 contract_path 转录。任何权威 Markdown 改动、source 改动、compiler 改动或 generated tree 改动均使此前验证失效。
