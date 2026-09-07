# 12 开源参考项目的 fixture runner 约定

2026-09-07 修订：本文为 [13 架构方案](./13-open-source-adoption-architecture.md) §9 的 runner 约定。
当前只有 `runner_skeleton.py`，具体 producer runner 未实现；以下隔离和执行能力为待实施要求。
`fixture_only` 可做固定样本/预录结果对照；`algorithm_candidate` 可进一步评估选取算法。
两种登记均不等于生产依赖准入，也不以整仓可离线启动为研究资格。

## 1. 硬性边界

1. **明确执行模式**：默认 `replay`，零 Provider 调用，仅消费冻结输入/预录响应；缺响应为
   `not_evaluated/replay_miss`，不淘汰在线项目。`live` 按显式预算通过宿主 provider adapter 调用。
   离线断网使用无网络容器/namespace，清代理变量不构成隔离；子进程仅获得白名单环境。
2. **只读输入**：runner 只能读取冻结 fixture 集（§3）与外部项目自身代码；不得写
   `packages/`、`auto_cut_bot/`、`governance/` 下任何文件。
3. **shadow 输出**：所有产物写入 `artifacts/shadow/<producer>/<run_id>/`，与权威
   Store 写路径物理隔离。Kernel/Store 对 shadow 目录零感知。
4. **固定 commit**：外部项目代码使用实验 spec 中登记的 commit；runner 核验 HEAD 及实际
   源码改动。dirty checkout 必须绑定完整修改内容 hash，否则不能标为固定源码实验。
   `governance/reuse.yaml` 当前仅登记 disposition；详细来源放拟新增 `tools/reuse/projects.json`，
   不向旧 closed schema 随意添加字段。外部条目当前未被 import gate 扫描，不能声称门禁已实现。
5. **环境隔离**：外部依赖用独立 uv venv 或容器安装，不进主项目 `pyproject.toml`；
   参考仓库本体放在 `work_ai/reference-projects/`（不进任何 git 仓库）。

## 2. 目录与产物约定

```text
reference-projects/<Project>/            # 外部仓库（固定 commit，git clone）
tools/reuse/<producer>/                  # 本项目的 adapter/runner 代码
  run.py                                 # producer adapter；统一入口拟为 tools/reuse/run.py
  README.md                              # 运行方式、依赖安装、已知限制
docs/llm-stage-contracts/fixtures/<set>/ # 冻结输入（见 §3）
artifacts/shadow/<producer>/<run_id>/    # 输出（UTC + 随机唯一 ID；源码 hash 独立记录）
  metadata.json                          # commit 校验、输入清单 hash、耗时、环境
  raw/                                   # 原始输出（原样保存，不改动）
  calls/                                 # 原请求/响应/usage/状态（live 或录制引用）
  projection/                            # 映射到本项目 DTO 的投影（可复算）
  metrics.json                           # 评估指标（§4）
```

## 3. 冻结输入（fixture manifest）

- 一个 fixture set = 一个 `fixtures/<set>/manifest.yaml`，列出输入相对路径/hash、episode 映射、
  标签种类/来源、视频对齐状态与具体问题。私有媒体根由运行参数映射，不写入内容身份。
  独立剧本/API 仅是参考；核对成片后才成为主评分标签。旧 pipeline 输出不能作独立 gold。
  作为模型 Context 的内容不得同时充当同项独立答案。字幕只用于隔离 baseline/评分，不输入生产 VLM。
- manifest 一经用于某次对照即冻结：修改内容必须新建 set（命名加日期后缀），禁止
  原地修改已引用的 set。
- 现有 Lucifer frozen set 保留，时间标签未核验，不直接评分定位；新增校对 label revision。
  book 42000021919 set 仍 pending；可做运行 smoke，不能报告质量验收成功。

## 4. 指标与判读

每个 producer 的 `metrics.json` 必须包含（可测则测，不可测写 `null` 并注明原因）：

| 指标 | 适用 | 说明 |
|---|---|---|
| `recall@k`（召回率） | 检索类 | 独立标签中被 top K 命中的比例，记录样本数/分母；参见 13 §10 |
| `entity_consistency` | 跨窗一致性 | 同人配对 P/R、异人误合并率、事件重复/丢失率；不只比较名字 |
| `token_usage` | 全部 | 实际 input/output/cached token；replay 本次与原录制成本分开 |
| `wall_time_s` / `gpu_mem_peak` | 全部 | 延迟与资源 |
| `failure_modes` | 全部 | 结构化列出：崩溃/超时/幻觉输出/Schema 不符 |

## 5. 判读与退出

- runner、脱敏 spec 与结论作为可复现工程交付提交 Git；原视频、含私有上下文的 raw 留私有目录。
  环境可按需重建，证据清理遵循明确保留期，不能删到无法复现结论。
- `fixture_only`/`algorithm_candidate` 是用途分类；拟晋升产品的模块走 adapter + 对应阶段验证，
  并补 reader、重放和双 Runtime 一致性。纯评测项目无需强行晋升。
- 相反结论保留并按数据集分层分析；同一冻结样本做配对对照，不以更新日期覆盖旧结果。

## 6. 与 Kernel 的隔离（重申）

shadow 原始输出不能直接成为权威事实、物理切点、Admission 结果或发布许可。
晋升产品后通过相应 Kernel Command 验证并持久化，不授予外部 runner Store 写权限。
具体模块实现及验收顺序见 [13 §11](./13-open-source-adoption-architecture.md#11-实施任务与依赖)。
