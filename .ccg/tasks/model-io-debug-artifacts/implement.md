# 实施与验证

1. 实现无状态、可注入的 `ModelIoDebugSink` 与严格脱敏/原子写入规则。
2. 在共享 Ark transport 接入 request、stream terminal 与 retrieve terminal 镜像；覆盖 VLM 和 Draft。
3. 在 FunASR/FSMN HTTP 边界接入同一 sink。
4. 通过 composition 的显式环境变量创建 sink；更新真实运行文档。
5. 编写单元测试：成功、incomplete、HTTP 异常、reconcile、脱敏、写入失败不改变 ProviderResult、默认禁用。
6. 跑相关 pytest、ruff、basedpyright；再提交前做只读审查。
