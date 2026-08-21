# Backend Authority Bootstrap

> 当前 backend code-spec 处于 v2.1.3 Authority Bootstrap。旧 v5、ArtifactBus、file-state 和旧 Stage 规范已撤销，不得作为实现许可。

## Pre-Development Checklist

- 读取 [Authority Bootstrap Block](./authority-bootstrap-block.md)。
- 读取 [Implementation Conformance Gates](./implementation-conformance-gates.md)。
- 读取 `ac_auto_cut/原理/v2.1-production-spec/` 五份权威契约。
- 读取当前 Trellis child 的 PRD、design、implement、implement/check context 到 EOF。
- 验证 task admission receipt 绑定当前 authority hash、repository refs 与 predecessor commits。
- Phase -1/02 完成前不得创建业务实现代码。

## Quality Check

- 新代码没有从旧实现、旧 task 或被删除文档推断默认行为。
- 普通 task 没有修改 protected path、gate、Schema、Registry 或自己的 oracle。
- `commit_decision` 与 `push_decision` 分别有完整且未过期的机器凭证。
