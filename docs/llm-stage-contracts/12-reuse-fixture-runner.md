# 12 开源参考项目的 fixture runner 约定

本文是 `11-open-source-reuse-landscape.md` §4 引入流程第 2 步的落地规范。任何外部项目
（VideoAgent、MMLVE、FunClip 等）在进入 Reuse Ledger 的 `algorithm_candidate` 评估前，
必须以本约定的 fixture runner 形态运行；不允许绕过本约定直接 import 或长期 fork。

## 1. 硬性边界

1. **离线、无网络、无密钥**：runner 及其子进程不得访问网络，不得读取任何 API key。
   外部项目若强依赖在线模型/服务，视为不可评估，处置降级为 `fixture_only`。
2. **只读输入**：runner 只能读取冻结 fixture 集（§3）与外部项目自身代码；不得写
   `packages/`、`auto_cut_bot/`、`governance/` 下任何文件。
3. **shadow 输出**：所有产物写入 `artifacts/shadow/<producer>/<run_id>/`，与权威
   Store 写路径物理隔离。Kernel/Store 对 shadow 目录零感知。
4. **固定 commit**：外部项目代码必须使用 Reuse Ledger（`governance/reuse.yaml` +
   文档 11 §2）登记的 commit；runner 启动时校验 `git -C <repo> rev-parse HEAD` 并把
   结果写进 run metadata，不一致即拒绝运行。
5. **环境隔离**：外部依赖用独立 uv venv 或容器安装，不进主项目 `pyproject.toml`；
   参考仓库本体放在 `work_ai/reference-projects/`（不进任何 git 仓库）。

## 2. 目录与产物约定

```text
reference-projects/<Project>/            # 外部仓库（固定 commit，git clone）
tools/reuse/<producer>/                  # 本项目的 adapter/runner 代码
  run.py                                 # 入口：--fixture-manifest --out-dir
  README.md                              # 运行方式、依赖安装、已知限制
docs/llm-stage-contracts/fixtures/<set>/ # 冻结输入（见 §3）
artifacts/shadow/<producer>/<run_id>/    # 输出（run_id = UTC 时间戳 + git short sha）
  metadata.json                          # commit 校验、输入清单 hash、耗时、环境
  <producer>_output/                     # 该项目的原始输出（原样保存，不改动）
  projection/                            # 映射到本项目 DTO 的投影（可复算）
  metrics.json                           # 评估指标（§4）
```

## 3. 冻结输入（fixture manifest）

- 一个 fixture set = 一个 `fixtures/<set>/manifest.yaml`，列出：输入视频/字幕文件
  （相对路径 + sha256）、对应的人类可读剧情摘要（ground truth）、评估问题清单。
- manifest 一经用于某次对照即冻结：修改内容必须新建 set（命名加日期后缀），禁止
  原地修改已引用的 set。
- 首个 set 用当前 `V23` 已冻结的单集/多集样本（复用 pipeline 已验证的输入）。

## 4. 指标与判读

每个 producer 的 `metrics.json` 必须包含（可测则测，不可测写 `null` 并注明原因）：

| 指标 | 适用 | 说明 |
|---|---|---|
| `recall@k`（候选命中率） | 检索类 | 对照本项目 CandidateCatalog 语义召回 |
| `entity_consistency` | 跨窗一致性 | 角色命名一致率、事件重复/丢失率（MMLVE 式） |
| `token_cost` | 全部 | 输入+输出 token（如可得） |
| `wall_time_s` / `gpu_mem_peak` | 全部 | 延迟与资源 |
| `failure_modes` | 全部 | 结构化列出：崩溃/超时/幻觉输出/Schema 不符 |

## 5. 判读与退出

- runner 代码与 shadow 产物都不是交付物：对照结论（连同 metrics）写入
  `docs/llm-stage-contracts/` 对应评估笔记后，runner 环境可整体删除，零遗留。
- 结论晋级路径仍按文档 11 §4：`fixture_only → algorithm_candidate → adapter
  （shadow Artifact 验证）→ 独立 Admission + 双 Runtime conformance + 回滚方案`
  才可成为可选 producer。
- 同一 producer 两次评估结论相反时，以较新 fixture set 为准，并在笔记中记录分歧。

## 6. 与 Kernel 的隔离（重申）

shadow 产物永远不能成为：权威事实、物理切点、Admission 结果、发布许可或任何
Store 写入者。adapter 化的唯一入口是文档 11 §4 的五步流程。
