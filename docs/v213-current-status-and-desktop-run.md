# v2.1.3 当前状态与台式机运行手册

更新日期：2026-08-26。本文是当前实现状态的交接入口；它不替代受保护
Authority、Profile 或 CalibrationRecord。

台式机切分支、安装依赖、启动 PostgreSQL/FunASR 和查看当前可运行范围，见
[v2.1.3 台式机快速启动手册](./v213-desktop-e2e-runbook.md)。

## 两个仓库的边界

| 仓库 | 用途 | 台式机是否运行 |
| --- | --- | --- |
| `auto_cut_bot` | 新架构的唯一可执行仓库：共享 `autocut_kernel`、HTTP Pipeline Runtime、Doubao Ark VLM、PostgreSQL Store、FunASR 服务和后续 Agent Runtime | 是 |
| `ac_auto_cut` | 历史项目和原理/设计文档的归档来源 | 否；只同步 `原理/` 文档，不同步 `legacy` 代码、`jobs/`、视频、代理窗口或临时产物 |

源视频、VLM 窗口代理、渲染结果和模型权重都不是 Git 源码。它们应放在台式机的
数据盘或对象存储，并由 SourceManifest/BlobRef 引用。

## 已完成且已提交的实现基础

- Kernel 的 Artifact、Command、Receipt、PostgreSQL 持久化、不可变 Blob 引用与
  Pipeline Runtime 基础已经存在。
- Doubao Ark 的 Files API + Responses SSE 流式 VLM 适配器已经存在；固定目标模型为
  `doubao-seed-2-1-pro-260628`。
- FunASR 服务使用一个进程同时加载 SenseVoiceSmall（带词级时间戳）和 FSMN-VAD；
  不使用 Whisper，也没有独立 VAD 服务。
- FunASR 的 Podman/Docker Compose 部署已提交：模型只读挂载、宿主机仅回环暴露
  `127.0.0.1:18765`、与 nanobot gateway 的 `8765` 不冲突。
- Stage 4 的整数 tick、A/V 端点、边界证据与候选选择的 Kernel 基础已实现并有
  针对性契约测试。
- Pipeline HTTP 的共享 Kernel 边界已经建立；Agent Runtime 的历史 MVP 不在当前
  候选分支，必须重新引入受限 adapter 并完成双 Runtime conformance。
- Stage 1 的真实八成员 Command、独立 Admission、有限重试/结果不明时对账、exact
  committed reader 已实现。HTTP 新任务按 `source_prep → vlm → stage1_narrative →
  stage2_portfolio → media_preflight` 调度；Stage 3 尚未接通，五阶段通过不代表完整 run 成功。
- execution profile v7 冻结完整 Stage 1/2 提示词与策略；narrative/shadow source 仍是 v2，
  local-run source 为 v3。其必填 Stage 2 策略必须来自实际安装资源；不得安装测试 fixture。
- Stage 2 已实现候选投影、素材支持、确定性故事组合、目标冻结，以及持久化生成
  Command、19 项独立校验、五成员原子提交与精确重放。Stage 1/2 共用流式生成、
  有限重试、超时对账和因果 Receipt 流程。Stage 2 HTTP 接线使用同一 Command，
  不执行或重建替代 Stage 1 产物。真实模型/数据库验收仍留给台式机。
- Stage 3 已交付精确前驱读取、蓝图草案解析、完整去重证据上下文、蓝图编译、
  每故事三成员组装/严格读取，以及有理数时长一致性检查。
  这还不是获准 Blueprint；素材组合可行性、独立准入、持久化命令和 Runtime 尚待接入。
  当前设计与进度见 [Stage 3 实现波次](./v213-task-plan/08-21-07-stage1-3-semantic-chain/stage3-production-wave.md)。

上述描述表示“已实现并经过单元/契约测试”，不表示“已经成功跑完一部真实剧”。

## 尚未完成的发布前闭环

按依赖顺序，剩余工作如下：

1. **台式机基础设施验真**：启动 PostgreSQL 和 FunASR 容器，构建真实模型镜像；
   记录一条真实媒体的受认证 FunASR 请求与健康/资源证据。
2. **CalibrationRecord 与运行 Profile**：用真实 SenseVoice/FSMN 输出完成 shadow
   calibration，独立验证非零误差界，再生成受保护的 local-run Profile。没有该
   Profile，服务应拒绝普通 Media Preflight，这是预期行为。
3. **Stage 1–3 语义链**：Stage 1 的本地实现已交付，仍需真实模型/数据库验收；下一项
   开发是 Stage 3 Blueprint 的已提交命令链；Stage 2 HTTP 接线还需台式机验真。
4. **真实一集 Stage 4**：以 admitted Blueprint 与完整 ASR/VAD MediaEvidence 编译
   ExactSpan/Recipe；只输出本地 Artifact/Receipt，不发布。
5. **本地 Render 与 Publication QC**：由已提交 Recipe 渲染，完成结构、媒体、编辑
   与发布前 QC；仍只保存本地文件。
6. **Agent Runtime**：在 Pipeline 已跑通后，以相同 Artifact/Policy/Command 做
   conformance；Agent 不引入第二套剪辑或发布逻辑。

因此，目前没有一个诚实的“对整部剧一键成功”的启动命令。若在第 2 步之前声称可
完整运行，得到的只会是绕过校准/Authority 的假验证，不能接受。

## 台式机启动顺序

### 1. 取得可执行仓库

```sh
git clone --branch feat/v213-contract-codegen https://github.com/2Red1Blue/auto_cut_bot.git
cd auto_cut_bot
```

不要 clone `ac_auto_cut` 的旧运行分支来启动 Pipeline；需要原理资料时，只取其中的
`原理/` 文档分支。

### 2. 安装应用依赖

```sh
uv sync --extra dev
```

应用的 Ark 凭据、Pipeline 私有配置和 FunASR token 均放在仓库外权限 `0600` 的
本地配置中，不提交到 Git；仓库根 tracked `auto_cut_bot.config.json` 不能保存 secret。

### 3. 准备 SenseVoiceSmall 与 FSMN 模型

在台式机下载或复制两个**完整、不可变**的模型快照目录：

- `iic--SenseVoiceSmall/snapshots/master`
- `iic--speech_fsmn_vad_zh-cn-16k-common-pytorch/snapshots/v2.0.4`

### 4. 启动本地 FunASR 容器

```sh
install -d -m 700 /absolute/private/autocut-config
install -m 600 deploy/funasr/.env.example \
  /absolute/private/autocut-config/funasr.env
# 编辑外部 funasr.env，填写模型目录、随机本地 token 和 Profile。
cd deploy/funasr
rm -f .env
ln -s /absolute/private/autocut-config/funasr.env .env
podman compose --env-file .env -f compose.yml up --build -d
curl --fail http://127.0.0.1:18765/health/ready
```

空或非法 Profile 会使容器在 health endpoint 建立前退出；合法 shadow Profile 只允许
校准 endpoint，独立验证后生成的 measured run Profile 才允许 timed evidence。这是
fail-closed，不是 Podman 故障。

### 5. 启动 PostgreSQL 与执行真实一集

PostgreSQL 的迁移、真实 shadow calibration/Profile 打包与“单集 HTTP Pipeline”
尚没有一键入口。HTTP composition 和 worker 已存在，但仅安装代码不等于已经具备有效
校准/Profile。台式机按以下顺序准备，具体命令见快速启动手册：

1. 连接新的真实 PostgreSQL 数据库，绝不复用/清空旧库；
2. 应用 Kernel migrations；
3. 执行 shadow calibration 并独立验证；
4. 安装该 CalibrationRecord 对应的 local-run Profile；
5. 仅在上述步骤成功后接受一个本地 HTTP Pipeline run；
6. 默认不配置任何外部发布端点。

`0020` 迁移保留旧终态记录，只允许 v7 新任务；旧未结束任务须先明确处理，不得自动
改写。只在空库按顺序运行全部迁移。数据库 pytest 使用独立、可丢弃的验收库，不得
把 `AUTOCUT_TEST_POSTGRES_DSN` 指向保存真实运行数据的库。

只有 nanobot gateway/UI 启动，不等于“整剧 Pipeline 已运行”。当前 whole-runtime
启动仍要求校准资源，尽管 Stage 1 本身不读取 ASR/VAD。

## 当前最短下一步

代码侧下一步是 Stage 3 Blueprint；不再把已完成的 Stage 1/2 Command 当作待设计任务。
台式机侧准备真实模型、校准、narrative/shadow v2、local-run v3 及 reviewed Stage 1/2 策略，运行单集并
检查真实 Receipt/ArtifactSet；数据库重启/并发与真实模型输出没有在本机验证。
之后接 Stage 4/Render/QC，再扩到全剧。当前目标仍只产出本地文件。

新增语义策略不要求重做有效 ASR/VAD 校准，但已 bootstrap 的旧 local-run key 不能绑定
新的 registry hash。发布新的 local-run profile_version 和对应 timed-speech entry 版本，
保留原 narrative/shadow 与 accepted CalibrationRecord，再 bootstrap 新 key。详见
[Stage 2 Runtime wave](./v213-task-plan/08-21-07-stage1-3-semantic-chain/stage2-runtime-wave.md)。
