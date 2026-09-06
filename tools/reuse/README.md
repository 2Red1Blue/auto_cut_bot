# tools/reuse — 开源参考项目对照 runner

按 `docs/llm-stage-contracts/12-reuse-fixture-runner.md` 的约定运行。
本目录每个子目录对应一个外部 producer 的 runner；`runner_skeleton.py` 是起点。

## 接入一个新 producer（检查单）

1. 确认其在 `governance/reuse.yaml` 的处置为 `algorithm_candidate`（`fixture_only`
   的项目不做 runner 对照）。
2. 仓库已按文档 11 §2 的固定 commit clone 到 `work_ai/reference-projects/<dir>/`。
3. 复制 `runner_skeleton.py` 为 `<producer>/run.py`，填写 PRODUCER block：
   `PRODUCER`、`PRODUCER_REPO`、`PINNED_COMMIT`，然后实现标注的两段
   （producer invocation + projection/metrics）。
4. 外部依赖装在独立环境：`uv venv .venv-reuse-<producer> && uv pip install ...`，
   或容器。绝不加入主 `pyproject.toml`。
5. 冻结 fixture set 放 `docs/llm-stage-contracts/fixtures/<set>/manifest.yaml`
   （输入文件 + sha256 + ground truth + 评估问题）。
6. 运行 `python3 tools/reuse/<producer>/run.py --fixture-manifest ...`，产物落在
   `artifacts/shadow/<producer>/<run_id>/`。
7. 对照结论写入 `docs/llm-stage-contracts/` 评估笔记；runner 环境用后可删。

## 当前 producer 状态

| producer | ledger 处置 | runner | 阻塞 |
|---|---|---|---|
| VideoAgent（HKUDS，f207987） | algorithm_candidate | 未建 | 全量依赖不可行（CUDA pytorch cu121 + CosyVoice/DiffSinger/fish-speech TTS 栈）。**可行路径**：选择性抽取 `tools/videorag/`（语义检索）与 `environment/`（intent→roles 编排）做离线对照；这两块依赖面待 runner 建立时核实 |
| MMLVE（Wucy0519，596ebb2） | algorithm_candidate | 未建 | 需要真实剧集 fixture set（素材 + ground truth），待内容侧提供 |
| 其余 7 个 | fixture_only | 不适用（不做 runner 对照） | — |

## 红线（重申）

runner 与 shadow 产物不得写入 `packages/`、`auto_cut_bot/`、`governance/`；
不得成为权威事实、物理切点、Admission 结果或发布许可。
