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

上述描述表示“已实现并经过单元/契约测试”，不表示“已经成功跑完一部真实剧”。

## 尚未完成的发布前闭环

按依赖顺序，剩余工作如下：

1. **台式机基础设施验真**：启动 PostgreSQL 和 FunASR 容器，构建真实模型镜像；
   记录一条真实媒体的受认证 FunASR 请求与健康/资源证据。
2. **CalibrationRecord 与运行 Profile**：用真实 SenseVoice/FSMN 输出完成 shadow
   calibration，独立验证非零误差界，再生成受保护的 local-run Profile。没有该
   Profile，服务应拒绝普通 Media Preflight，这是预期行为。
3. **Stage 1–3 语义链**：把 committed VLM evidence 接入 Narrative Graph、Story
   Proposal 和 Blueprint 的已提交命令链。
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

PostgreSQL 的迁移、Authority bootstrap、shadow calibration 和“单集 HTTP Pipeline”
还没有被封装为一个台式机一键脚本。下一项实现任务就是提供该受控启动器。它必须：

1. 连接新的真实 PostgreSQL 数据库，绝不复用/清空旧库；
2. 应用 Kernel migrations；
3. 执行 shadow calibration 并独立验证；
4. 安装该 CalibrationRecord 对应的 local-run Profile；
5. 仅在上述步骤成功后接受一个本地 HTTP Pipeline run；
6. 默认不配置任何外部发布端点。

在该启动器落地前，可以启动 nanobot gateway/UI，但它不等于“整剧 Pipeline 已运行”。

## 当前最短下一步

在台式机先验证 Podman/FunASR 真模型能启动、记录资源峰值；随后实现并运行“PostgreSQL
bootstrap → shadow calibration → 单集 HTTP Pipeline”的受控启动器。通过一个真实剧集的
完整 Receipt/ArtifactSet 后，才扩展到全剧和 Render/QC。
