# tools/reuse — 开源参考项目对照 runner

按 `docs/llm-stage-contracts/12-reuse-fixture-runner.md` 的约定运行。
本目录每个子目录对应一个外部 producer 的 runner；`runner_skeleton.py` 是起点。

## 接入一个新 producer（检查单）

1. `fixture_only` 可对比样本/预录结果；`algorithm_candidate` 可评估选取算法。
   当前 ledger 外部条目是信息登记，不是已实现的生产 import 准入。
2. 仓库按实验 spec 和文档 13 的参考 commit clone 到 `work_ai/reference-projects/<dir>/`。
3. 复制 `runner_skeleton.py` 为 `<producer>/run.py`，填写 PRODUCER block：
   `PRODUCER`、`PRODUCER_REPO`、`PINNED_COMMIT`，然后实现标注的两段
   （producer invocation + projection/metrics）。
4. 外部依赖装在独立环境：`uv venv .venv-reuse-<producer> && uv pip install ...`，
   或容器。绝不加入主 `pyproject.toml`。
5. 冻结 fixture set 放 `docs/llm-stage-contracts/fixtures/<set>/manifest.yaml`
   （输入文件 + sha256 + ground truth + 评估问题）。
6. 当前骨架仍使用 `--fixture-manifest`；R0 将提供 13 §9 定义的统一 spec/mode 入口。
   producer 尚未实现时不能按骨架命令宣称实验通过，产物统一落在 `artifacts/shadow/<producer>/<run_id>/`。
7. 对照结论写入 `docs/llm-stage-contracts/` 评估笔记；runner 环境用后可删。

## 当前 producer 状态

2026-09-07：现有骨架尚未实现 producer 调用、实际网络隔离、完整输入校验和评分。
清代理/HF offline 环境变量不等于断网。新版 `replay/live` 实现顺序见
[13 方案](../../docs/llm-stage-contracts/13-open-source-adoption-architecture.md) §9–11。
下面是调查记录，不能用“fixture 已就绪”推断独立视频标签已验收。

| producer | ledger 处置 | runner | 阻塞 |
|---|---|---|---|
| VideoAgent（HKUDS，f207987） | algorithm_candidate | 未建 | 全量依赖不可行（CUDA pytorch cu121 + CosyVoice/DiffSinger/fish-speech TTS 栈）。**可行路径**：选择性抽取 `tools/videorag/`（语义检索）与 `environment/`（intent→roles 编排）做离线对照；这两块依赖面待 runner 建立时核实 |
| MMLVE（Wucy0519，596ebb2） | algorithm_candidate | 未建 | fixture set 已就绪：`fixtures/when-lucifer-kneels-ep01-ep02/`（含独立剧本 ground truth）；待建 runner |
| 其余 7 个 | fixture_only | 未建；可做固定样本对照 | 按具体问题选择，不统一安装 |

## 红线（重申）

runner 与 shadow 产物不得写入 `packages/`、`auto_cut_bot/`、`governance/`；
不得成为权威事实、物理切点、Admission 结果或发布许可。
