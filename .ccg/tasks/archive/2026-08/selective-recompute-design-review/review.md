# 阶段/单集重算对抗审查

日期：2026-08-28；代码基线：`17530231`。
范围：设计及现状验证，不修改业务代码、不调用模型、不操作 PC/数据库。
角色：主 Agent 设计与验证、独立 `recompute_code_evidence` 代码核查、
独立 `recompute_design_review` 初稿反例审查；没有调用 Claude Code，不宣称跨模型审核。

## 1. 现状判断

原先“新建 run，直接保留旧的 SourcePrep/其他集 Receipt，再 resume”方案不可直接实施。
问题不是现有严格绑定应该删除，而是缺少显式的跨生产者复用契约和读取路径。
可用的同请求 replay/reconcile 不等于选择性重算。

| 发现 | 证据（仓库相对路径与行号） | 修订设计 |
| --- | --- | --- |
| P0：旧 BlobRef/Receipt 不是新 Job 的读取授权 | `auto_cut_bot/pipeline/vlm/request_factory.py:273` 拒绝不同 source_job；`packages/autocut-kernel/src/autocut_kernel/store/postgres.py:3037` 检查 Job blob claim | BoundVlmInputs + Kernel 验证 exact producer closure；按原 Job claim 读取，新输出归目标 Job；旧入口不放宽 |
| P1：finalizer/readback 不支持混生产者 | `packages/autocut-kernel/src/autocut_kernel/pipeline/finalize_vlm_batch_command.py:183` 按 aggregate Job 读取 child；`store/postgres.py:5759` 回读重算 aggregate request | 新版本 finalizer 与 reader 同时扩展；不复制/改名旧 Receipt |
| P1：全局 policy 改变后拼旧 siblings 不成立 | `finalize_vlm_batch_command.py:325` 检查全集及同 policy；`auto_cut_bot/pipeline/runtime/vlm_stage.py:101` 绑定完整 profile | 显式有效策略与逐集指纹；selected_only 检查新策略，full_stage 扩展缺口且执行前确认 |
| P1：换 SourcePrep 改变 provenance，即使视频 bytes 相同 | `auto_cut_bot/pipeline/source_prep/command.py:200` provenance 含 producer Job/Receipt/Set | 首版固定完整原 SourcePrep；不承诺增量换源 |
| P1：/resume 被文档描述得过宽 | `auto_cut_bot/pipeline/runtime/postgres.py:329` 仅受支持 run 状态且匹配 media_preflight；`worker.py:211` 跳过 terminal run | 修正 PC 说明；重算用独立新接口，v2 resume 只解除新计划 hold |
| P1：进程环境开关不能保证跨 worker 暂停 | `auto_cut_bot/pipeline/runtime/vlm_stage.py` 的 stop_after_probe 为实例参数；已有测试会创建不带开关的实例继续原 run | 说明现有开关限制；新计划要求 DB 持久化 frontier 与 Store dispatch 校验 |
| P1：runtime failed 不能证明所有并发调用结束 | `auto_cut_bot/pipeline/runtime/vlm_stage.py:323` chunk 结果可同时包含 terminal/indeterminate | 查 Kernel Attempt 链；同集 overlap + hold + reservation 在 lineage 锁事务中处理 |
| P2：PTS 换算和 token 根因的断言证据不足 | 实际已知 length 终止；仅整数 proxy_pts 范围无 time_base 不能推出秒数 | 设计 §7 撤回未证实因果/秒数；短 ID 优化不替代时间与来源证据 |

现有代码的严格同 Job/readback/完整批次校验保留。修复当前功能缺口是新增明确的
binding/命令/控制路径，不是去除安全检查，也不需要整体重写两个 Runtime。

## 2. 独立初稿审查

独立 reviewer 在初稿中发现 2 个 P1，无 P0；另补充一个避免重复生成的边界案例。

| 发现 | 修订与验收 |
| --- | --- |
| P1：检查结果显示 paused，但下一次计划只接受终态父，inspect→full_stage 不可达 | §6.2 增加状态表：selected_only 成功是 succeeded + 明确局部范围；full_stage 等确认才是非终态 paused。RC-21 验证 |
| P1：将尚未实现的 RecoveryLedger/head 写作既有能力，且把预算并发测试排到第二切片 | §4/§6/§8 明确当前只有 per-Command Attempts；首切片补最小 initialize/reserve/finalize/CAS 和同集 gate，测试不能后置。RC-11～15、23 验证 |
| 边界：单集剧或所有集已检查成功，强制非空选择会无谓再生成 | 只允许 full_stage 空选择，零缺口只 finalizer；有缺口仍按完整计划确认。RC-22 验证 |

另由主 Agent 补充：继承映射只从指定父快照取得、失败新选择不能退回祖先成功；
区分不同集输入身份与统一 semantic-policy 身份；未知父调用需 Kernel 对账而非 HTTP
resume terminal run；旧 worker 未隔离前不得启用新计划派发。
这些是设计闭合，不是相应运行代码已修复。
同一独立 reviewer 对修订位置限定复核后确认上述 2 个 P1 和空选择边界均已关闭，
未发现新增阻断矛盾。结论：修订设计可用于功能切片实施；不能据此批准当前代码
已经支持选择性重算，更不能声称新增数据库并发机制已经通过验收。

## 3. 已实际执行的验证

```text
.venv/bin/python -m pytest tests/pipeline/test_pipeline_vlm_stage.py tests/pipeline/test_run_service.py tests/test_pipeline_api.py -q
82 passed in 1.69s

.venv/bin/python -m pytest tests/pipeline/test_doubao_vlm_request_factory.py tests/pipeline/test_finalize_vlm_batch_command_postgres.py -q
39 passed in 0.60s
```

合计 121 项现有测试通过。虽然文件名包含 postgres，finalizer 的此套测试使用 fake
Store，不能作为真实 PostgreSQL 验收证据。未运行数据库 migration/双连接/崩溃实验，
也未重跑真实 VLM。新增 RC-01～RC-23 是明确的待实现验收，不是已通过测试。

文档检查：`git diff --check`；三个修改文档中所有 Markdown 链接目标存在；
请求 JSON 示例和任务 JSON 校验通过，章节编号 1～8 完整。
私有 `auto_cut_bot.config.json` 保持不动，未纳入本任务提交。

## 4. 交付边界

主设计见 `docs/pipeline-selective-recompute-design.md`，现状限制同步到
`docs/pc-semantic-pipeline-run.md` 和 Task04 设计。
后续先做一个功能切片：复用 SourcePrep → 新 run 指定集真实 VLM → 保存输入/输出和
独立 SelectionResult；然后补兼容 sibling 复用与完整 batch closure。没有外部发布步骤。
