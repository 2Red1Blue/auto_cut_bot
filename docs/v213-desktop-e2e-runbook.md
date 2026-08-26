# v2.1.3 台式机快速启动手册

更新日期：2026-08-26。

这份文档给接手项目的 AI 或开发者使用：先进入正确分支，再启动当前已经具备的本地
能力。它是操作入口，不是生产验收规范。

## 1. 只使用这个仓库和分支

```text
repository: https://github.com/2Red1Blue/auto_cut_bot.git
branch: feat/v213-contract-codegen
```

`ac_auto_cut` 仅保存原理文档和历史参考，不用于启动新版 Pipeline，也不要运行其中的
legacy Stage、数据库或任务脚本。

新机器首次拉取：

```sh
git clone --branch feat/v213-contract-codegen --single-branch \
  https://github.com/2Red1Blue/auto_cut_bot.git
cd auto_cut_bot
```

已有仓库切换：

```sh
set -eu
cd /path/to/auto_cut_bot
test -z "$(git status --porcelain=v1)" || {
  echo >&2 '工作区不干净，请先提交到自己的备份分支'
  exit 1
}
git fetch origin --prune
git switch feat/v213-contract-codegen
git merge --ff-only origin/feat/v213-contract-codegen
git status --short --branch
git log -1 --oneline
```

## 2. 安装基础依赖

要求 Python 3.11+、`uv`、FFmpeg、Podman 和 Compose provider：

```sh
python3 --version
uv --version
ffmpeg -version
podman version
podman compose version
uv sync --extra dev
uv run python -c 'import auto_cut_bot, autocut_kernel; print("imports ok")'
```

密钥、媒体、模型、数据库密码和输出目录都放在仓库外。不要把 secret 写入已跟踪的
`auto_cut_bot.config.json`：

```sh
install -d -m 700 /absolute/private/autocut-config
# 按本机实际内容创建 config.json，随后：
chmod 600 /absolute/private/autocut-config/config.json
```

### 台式机需要带过去什么

最小启动只需安全复制下面这些私有内容，Git 仓库本身不要手工复制：

| 内容 | 台式机建议位置 | 是否必需 |
|---|---|---|
| nanobot/Agent 私有配置 | `/absolute/private/autocut-config/config.json` | 只启动 UI/Agent 时需要 |
| FunASR 环境配置 | `/absolute/private/autocut-config/funasr.env` | 启动 ASR/VAD 时需要 |
| SenseVoiceSmall 模型目录 | 台式机任意仓库外绝对路径 | 启动 ASR/VAD 时需要 |
| FSMN-VAD 模型目录 | 台式机任意仓库外绝对路径 | 启动 ASR/VAD 时需要 |
| Doubao Ark API key、tenant/project 信息 | 仓库外私有环境文件或 secret store | 运行真实 VLM 时需要 |
| 原始剧集文件 | 仓库外媒体目录 | 运行真实 Pipeline 时需要 |

`funasr.env` 需要填写 `.env.example` 中列出的模型路径、端口、资源上限、随机本地 token
和 Profile。模型目录可重新下载，不一定要从笔记本复制。

以下内容不要直接复制：

- 仓库内 `.env`、缓存、临时文件、渲染输出；
- 已跟踪的 `auto_cut_bot.config.json` 本机修改；
- Podman 容器本身。

PostgreSQL 如果还没有真实 v2.1.3 运行数据，直接在台式机创建新库即可。只有确实要复用
已上传的 Doubao `file_id` 时，才需要迁移对应 PostgreSQL 数据；`file_id` 位于
`storage.provider_media_objects`，并绑定 Ark tenant/project、源文件身份和有效期，不能
只复制一个 ID 文本。当前尚未产生真实 v2.1.3 数据时不需要迁库。

## 3. 先验证当前代码

下面是当前分支已验证通过的 provider-free 快速检查：

```sh
uv run pytest tests/store/test_runtime_core_migration.py \
  tests/pipeline/test_pipeline_runtime_composition.py \
  tests/pipeline/test_funasr_timed_speech.py -q
```

测试应全部通过，数量随开发增加；不要把旧的固定用例数量当成验收条件。
这证明基础契约和组合代码可运行，不代表已经完成真实整集剪辑。

## 4. 启动 FunASR

FunASR 容器在同一进程内加载：

- SenseVoiceSmall：提供词级时间边界；
- FSMN-VAD：提供语音活动区间；
- 不使用 Whisper，也不需要单独启动 VAD 服务。

先准备两个完整模型目录：

```text
iic--SenseVoiceSmall/snapshots/master
iic--speech_fsmn_vad_zh-cn-16k-common-pytorch/snapshots/v2.0.4
```

配置并启动：

```sh
install -d -m 700 /absolute/private/autocut-config
install -m 600 deploy/funasr/.env.example \
  /absolute/private/autocut-config/funasr.env
# 编辑 funasr.env：填写绝对模型路径、本地随机 token 和有效 Profile。

cd deploy/funasr
rm -f .env
ln -s /absolute/private/autocut-config/funasr.env .env
podman compose --env-file .env -f compose.yml up --build --detach
curl --fail http://127.0.0.1:18765/health/live
curl --fail http://127.0.0.1:18765/health/ready
cd ../..
```

空或非法 Profile 会导致服务拒绝启动，这是当前 fail-closed 设计。Profile 的生成与
校准说明见 [FunASR 部署说明](../deploy/funasr/README.md)。

## 5. 启动本地 PostgreSQL

新版使用独立数据库 `autocut`，默认只绑定台式机回环地址 `127.0.0.1:5433`。不要复用
legacy 数据库，也不要删除已有 volume。

首次创建一个全新实例：

```sh
set -eu
export AUTOCUT_DB_PASSWORD='替换为本机随机强密码'
podman volume create autocut-postgres-data
podman run --detach --name autocut-postgres \
  --publish 127.0.0.1:5433:5432 \
  --env POSTGRES_DB=autocut \
  --env POSTGRES_USER=autocut_app \
  --env POSTGRES_PASSWORD="$AUTOCUT_DB_PASSWORD" \
  --volume autocut-postgres-data:/var/lib/postgresql/data:Z \
  docker.io/library/postgres:16
unset AUTOCUT_DB_PASSWORD
podman exec autocut-postgres pg_isready -U autocut_app -d autocut
```

应用迁移：

```sh
for migration in packages/autocut-kernel/migrations/*.sql; do
  echo "applying $migration"
  podman exec -i autocut-postgres \
    psql -v ON_ERROR_STOP=1 -U autocut_app -d autocut < "$migration"
done
```

这段迁移命令只用于全新空库。已有数据库在正式 migration runner 落地前不要重复执行。
`0020` 要求 pre-v7 的 accepted/running 任务先被明确处理；不会自动修改旧任务的策略、
补阶段或把它们判成功。`AUTOCUT_TEST_POSTGRES_DSN` 仅用于可丢弃的验收库：对应 pytest
会重建 schema，**绝不能指向这里保存真实运行数据的 `autocut` 库**。

## 6. 启动当前 HTTP 服务

普通 nanobot UI/API 可以先启动：

```sh
uv run auto_cut_bot webui \
  --config /absolute/private/autocut-config/config.json
```

或：

```sh
uv run auto_cut_bot serve \
  --config /absolute/private/autocut-config/config.json \
  --host 127.0.0.1 --port 8765
```

当前 `/v1/pipeline/run` 路由及真实阶段注册已经接线。仅启动 UI/API、不配置 Pipeline
运行环境时，才会返回 `503 Pipeline run service is not configured`。它不是固定占位路由。

启用 Pipeline 还需要完整的 `AUTO_CUT_BOT_PIPELINE_*` 数据库、SourceCatalog、Ark 配置，
`AUTO_CUT_BOT_MEDIA_PREFLIGHT_POLICY_JSON`、staging 目录、materialization limits，以及
与实际校准一致的 installed local-run resource。参数检查入口见
[`composition.py`](../auto_cut_bot/pipeline/runtime/composition.py)。缺项、策略不匹配或
未安装有效资源必须先解决，不能通过复制测试 fixture 激活。

本次 Stage 2 接线使用 execution profile v7，冻结完整 Stage 1/2 模型、提示词、预算和
故事选择策略。narrative/shadow source 保持 v2；local-run source 升为 v3，新增必填的
`stage2_command_policy` 与其 hash。旧 source 不自动补字段，不能用测试 fixture 激活。

已有有效校准不必重跑：保留原 narrative/shadow 文件和 accepted CalibrationRecord。
如果台式机已 bootstrap 旧 local-run 版本，新配置必须使用新的 `profile_version`，并
同步其中 timed-speech entry 的版本，再 bootstrap 新 key；不能覆盖旧版本的 anchor。
这只是新增运行策略的版本，不能修改测量结果或伪造校准。
详细边界见 [Stage 2 运行接线](./v213-task-plan/08-21-07-stage1-3-semantic-chain/stage2-runtime-wave.md)。

## 7. 当前真实进度

已经具备：

- PostgreSQL Artifact/Command/Receipt 基础；
- Doubao Ark streaming VLM 接口；
- SenseVoiceSmall + FSMN-VAD 的 timed evidence 接口；
- Stage 4 整数 tick、A/V 边界与 ExactSpan 基础；
- Pipeline HTTP 到共享 Kernel 的边界；
- Stage 1 真实生成 Command、八成员原子提交、独立校验与重放；
- Stage 2 真实生成 Command、五成员原子提交、19 项独立检查与重放；
- HTTP 新任务的 `source_prep → vlm → stage1_narrative → stage2_portfolio → media_preflight` 调度；
- 本地测试和 Podman 部署文件。

尚未闭合：

1. Authority bootstrap 与真实运行 Profile；
2. Stage 1/2 的真实模型/数据库验收，以及 Stage 3 Blueprint 脚本链；
3. Stage 4 到 Recipe 的真实整集组合；
4. FFmpeg Render 与本地 Publication QC；
5. 一个真实剧集的 HTTP 端到端运行；
6. Agent Runtime 的共享 Kernel conformance。

所以现在可以启动基础设施、配置好的服务和测试，但还不能用一条 HTTP 请求跑完整部剧。
即使当前五个阶段全部成功，整个 run 也不会冒充完整成片成功。下一项代码工作是 Stage
3；台式机并行准备真实校准/Profile 和单集验真。外部发布保持关闭。

## 8. 常用停止与更新命令

```sh
podman stop autocut-postgres
cd deploy/funasr
podman compose --env-file .env -f compose.yml down
cd ../..
```

更新代码前先确认工作区干净：

```sh
set -eu
test -z "$(git status --porcelain=v1)"
git fetch origin --prune
git switch feat/v213-contract-codegen
git merge --ff-only origin/feat/v213-contract-codegen
uv sync --extra dev
```

遇到问题时，先保存以下输出再定位：

```sh
git status --short --branch
git log -1 --oneline
podman ps --all
podman logs --tail 200 autocut-postgres
cd deploy/funasr && podman compose --env-file .env -f compose.yml logs --tail 200
```
