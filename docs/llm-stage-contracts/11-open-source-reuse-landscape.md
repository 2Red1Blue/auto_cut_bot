# 11 开源自动剪辑参考与复用边界

调研日期：2026-09-05。下表的顶层仓库许可证和 commit 都在该日通过 GitHub API 核对；项目链接
固定到该 commit。此文不是依赖清单，也不授权 `autocut_kernel` 直接 import 任一项目。真正引入代码前，
仍必须登记 Reuse Ledger、重新核对许可证/提交版本、写适配器与回归 fixture。

## 1. 结论与架构位置

当前产品的目标是批量短剧素材的 VLM-first 理解、ASR/VAD 物理对齐、精确 span 编译、可恢复
Command/Receipt、渲染与 QC。大多数开源项目解决的是交互式编辑、播客/教程高光，或生成式视频
改造；没有一个同时提供本产品的素材证据、跨集叙事、精确 A/V 切点、原子批次提交和无人运行
准入。

因此采用下列边界：

```text
Agent/UI 试验层 —— 可借鉴意图、工具目录、检索与编辑体验
       ↓ typed Command（无私有绕过写路径）
autocut_kernel —— 本项目唯一的 Artifact / Receipt / Admission / ExactSpan 权威
       ↓
FFmpeg / ASR / VAD / VLM / Renderer 等可替换 producer
```

外部项目输出只能作为：

- 离线评测样本、提示词/任务分类参考，或经 adapter 产生的候选；
- 明确版本化、可复算的非权威输入。

它们的原始观察可以保存为标明 producer 的 shadow evidence，但不能**直接**成为权威事实、物理切点、
Admission 结果、发布许可或数据库写入者。

## 2. 项目账本

| 项目（固定 commit） | 顶层许可证/定位（调研日） | 可吸收 | 禁止直接复用或替换 | 推荐动作 |
|---|---|---|---|---|
| [HKUDS/VideoAgent@f207987](https://github.com/HKUDS/VideoAgent/tree/f207987e3cffb554aaa6ffdbe733efb30f4b51ed) | MIT；研究型对话式多 Agent 视频理解/编辑/再创作 | 长视频任务分解、工具能力分类、VideoRAG 式语义检索、编辑任务/基准分类 | 运行时 LLM 生成 Agent 图、动态工具发现、路径文件传递、正则提取模型 JSON、它的状态/重试机制 | 只做语义/编排对照实验；若有收益，在 `cut_bot` 写 typed adapter，不进入 Kernel |
| [MMLVE@596ebb2](https://github.com/Wucy0519/MMLVE/tree/596ebb23d5bf15c04259dd71367008c923d3f425) | MIT；研究型多镜头生成式视频编辑 | 全局记忆卡、跨镜头实体连续性、正负反馈式质量诊断 | 生成式 V2V 编辑、把 VLM 反馈直接重试成片 | 把“记忆卡”抽象为冻结的 Context Pack/跨窗 shadow 指标，先做 fixture 评估 |
| [Dawn Cut@7de68fc](https://github.com/kwakseongjae/dawn-cut/tree/7de68fce41505d8092ec227806b8d4bea4127675) | MIT；本地文本编辑器/MCP | serializable EditCommand、dry-run diff、审计日志、同一 command bus 供 UI/Agent 调用 | TypeScript UI 状态作为权威、人工审批当无人发布门禁、其尚未完成的渲染能力 | 借鉴 Recipe/渲染预览 UX；Kernel 仍保留自己的 Command/Receipt 与 Admission |
| [AI Video Editor@68980d1](https://github.com/MartinDelophy/ai-video-editor/tree/68980d142cce421eab86cd4ef26a4475a6affd56) | MIT；local-first 可编辑时间线 | 分轨 timeline、字幕/画面布局、人工检查与可编辑产物体验 | 浏览器编辑器和其模型下载/缓存策略替代批量 Pipeline | 只在未来制作 review/修订 UI 时参考；不是本项目的运行时依赖 |
| [FunClip@2a954d4](https://github.com/ModelScope/FunClip/tree/2a954d4fbad6a57a5271390be4eb43f80d201b60) | MIT；ASR 驱动的高光/字幕剪辑 | ASR 时间轴、粗选片 UX、字幕工作流 | 将文字转录当剧情理解真相，或将 ASR 时间直接当精确 A/V 切点 | 可比较粗召回；生产仍使用 SenseVoiceSmall/FSMN 的时间证据与 ExactSpan |
| [video-highlight-skill@35a81e1](https://github.com/inhai-wiki/video-highlight-skill/tree/35a81e11c02c90289cbca3ca2e9f641e4c050b9d) | MIT；Agent skill，高光分析/FFmpeg/SRT | Agent 调用步骤、结果页/调试产物形态 | skill 脚本绕过 Command、以 LLM 直接拼 FFmpeg 参数 | 作为工具层参考，不安装为生产依赖 |
| [kinocut@999dd40](https://github.com/KyaniteLabs/kinocut/tree/999dd40000749e3a9dc34fb4090e6adabc9213a4) | Apache-2.0；Agent video editing MCP/CLI | typed media tool、Video Receipt 的产品形态 | MCP server 的 receipt 代替本项目 Artifact/Receipt，或将其所有媒体策略视为本项目 Policy | 可评估为可选的非权威工具 adapter；不得给它 Store 写权限 |
| [NarratoAI@9fc6223](https://github.com/linyqh/NarratoAI/tree/9fc6223f0ab816e9a237e7e1d09e16a0ab905312) | MIT；解说/社媒重制自动化 | 叙事脚本、旁白、成片模板、发布流程参考 | 用单次转写摘要取代跨集剧情图，或复用其发布状态作为权威 | 仅在 Stage 3 脚本/模板和未来平台 connector 做竞品对照 |
| [PodPilot@c2b7da8](https://github.com/ayushkumarTomar/PodPilot/tree/c2b7da8a6a8846c8c0f34867a8c06e02ad93ab51) | MIT；播客高光与自动发布 | 高光工作流、发布 connector 的失败处理参考 | 将播客单次摘要/发布状态套用为短剧多集权威状态 | 仅作发布 connector 与高光策略的竞品对照 |

许可证是所列 commit 的**顶层仓库**快照。实际二开前必须重新核对项目根 `LICENSE`、vendored model/数据集许可和服务
条款；不能因为仓库顶层是 permissive license 就假定全部模型、示例媒体和第三方代码可商用。

## 3. 对 VideoAgent 的具体判断

VideoAgent 最接近“全栈视频 Agent”，但并非当前 Pipeline 的基础设施：其入口创建 `MultiAgent`，
自动发现工具；Agent 图由 LLM 生成，流程主要传递本地文件路径。其依赖同时锁定大量音频、TTS、
生成模型、Whisper/FunASR 与 CUDA 组件。这种设计适合研究 demo 和自然语言探索，却不能证明：

1. 一条素材为什么被选中、其 evidence 来自哪个不可变输入；
2. VLM 粗定位如何独立收敛成 video/audio 的精确端点；
3. 失败/retry/replay 是否保持同一 Command identity；
4. 多集任务何时才可原子地进入下一阶段或发布。

所以不 fork 它来替换 `ac_auto_cut`。正确用法是进行有界对照：用同一冻结视频集合比较其
摘要/任务分解或检索方案与本项目的 WindowContextPack、NarrativeGraph、CandidateCatalog 的
语义召回、角色连续性、成本和延迟；它的输出只写 shadow Artifact，不能驱动 Stage 4。上述语义指标
不能替代 VLM 粗区域到确定性 ExactSpan 的物理验证、媒体 QC 或发布准入。

## 4. 引入流程

每个候选模块按下列顺序进入，而非“先 import，出问题再补契约”：

1. 在 Reuse Ledger 建条目：仓库、commit、license、用途、`fixture_only`/`algorithm_candidate`/
   `approved_adapter`/`banned` 处置；
2. 建立无网络、无密钥的 fixture runner，比较质量、token、延迟、GPU/内存和失败模式；
3. 若值得保留，重写最小 adapter。输入/输出为本项目 closed DTO/BlobRef，adapter 没有 Store、
   Admission 或发布写权限；
4. 以 shadow Artifact 运行，定义具体晋升指标和退出条件；
5. 仅在独立 Admission、双 Runtime conformance 和回滚方案通过后，允许它成为可选 producer。

对当前优先级而言，最有价值的验证是 VideoAgent 的长视频检索/记忆与 MMLVE 的跨镜头连续性；
不是迁移它们的 Agent runtime、Whisper 链路或生成式编辑堆栈。

## 5. 一手来源

- VideoAgent [repository](https://github.com/HKUDS/VideoAgent) 与 [paper](https://arxiv.org/abs/2606.23327)：
  项目宣称提供理解、编辑、再创作和多 Agent 编排；论文的实验数字仅作为其基准内结果，不外推为
  本项目的生产保证。
- MMLVE [repository](https://github.com/Wucy0519/MMLVE)：公开说明为分镜 VLM、全局记忆卡和生成式编辑传播。
- Dawn Cut [repository](https://github.com/kwakseongjae/dawn-cut)：公开文档描述 command bus、dry-run/diff
  与 MCP；其渲染/字幕等路线图仍有未完成项。
- 其余项目应在实际评估时固定 commit 并复核，避免链接的 main 分支漂移。
