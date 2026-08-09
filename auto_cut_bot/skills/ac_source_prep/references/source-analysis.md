# 素材与连续视频窗

## 输入

支持：

- 递归本地视频目录。
- 文本 URL 清单，每行一个 HTTP(S) URL。
- JSON 数组；成员可为 URL 字符串或 `{id, episode, url, duration_seconds}`。

本地文件支持 `.mp4`、`.mov`、`.mkv`、`.m4v`、`.webm`、`.avi`、`.ts`。

## 集号

本地模式从文件名最后一个正整数推断集号；无数字时使用排序位置。集号冲突时停止，不自行重排。远程 JSON 优先使用显式 `episode`。

## 窗口

- 默认 240 秒；显式 `--window-seconds 180` 仍受支持。
- 合法范围 150–360 秒。
- 相邻重叠至少 8 秒，默认 12 秒。
- 尾窗完整保留。
- 本地模式将窗口转码为 720p 上限、H.264/AAC 连续 MP4，供多模态请求使用。
- 窗口分析返回原视频绝对时间码。
- Highlight 按全剧、跨剧集一致的绝对尺度判断，不把当前窗口或本集最强片段自动
  当作高光；普通内容无需为了凑数生成 Candidate。
- Highlight 的自然完整表达边界优先于 Teaser 时长。自然边界不超过 15 秒时可进入
  直接 Teaser 资格；超过 15 秒时保留为剧情/正文证据，由本地合同排除直接 Teaser
  资格，不得为了时长截断核心表达或可见兑现。
- `strength` 只输出 1–10 的绝对评分，内部按剧情重要性、情绪/视觉冲击、独立传播
  能力和稀缺性四维校准；不得把"本集最强"自动记为 10。

## 隐私

- API Key 只从 `QWEN_AI_API_KEY` 或 `ARK_API_KEY` 读取。
- Source Manifest 中的 URL 删除 query 和 fragment。
- 含签名 URL 的窗口 batch 和 `remote-download-manifest.json` 使用 `0600`。
- 不把 Authorization、Data URL 或完整签名 URL写入摘要、诊断或交付物。

## 远程 URL 的并行本地化

远程准备阶段必须同时生成：

- `remote-download-manifest.json`：私密精确 URL，仅供下载进程读取。
- `local-source-manifest.json`：公开本地源状态，初始为 `pending`。
- `remote-download-report.json`：逐 Source 下载、探测和哈希结果。
- `remote-download-launch.json`：独立进程身份和启动状态。

首批 Window Analysis VLM 任务开始前，由 `run_semantic_batch.py` 使用独立进程并行
下载全部 Source。VLM 继续直接引用远程 URL，不等待下载；下载支持 `.part` 续传、
完成后的原子改名、FFprobe 验证和 SHA-256。进入本地 VAD、QC Proxy 或正式渲染前，
相关 Source 必须全部下载成功；失败时按 Source ID 报错，不回显签名 URL。

若完整远程源的 Window 响应只因核心 Event 越出声明范围而失败，失败子集重跑会在
对应下载源已经完成并通过路径、集号、SHA-256、视频流和时长校验后，转码真实物理
Window 并改用本地内联媒体。恢复请求使用独立的 Source/Clip SHA、范围和 policy
签名；原 Batch Job 保持不变。物理媒体仍不能通过原 Window Schema、身份和时间码
合同时硬停止，不删除核心 Event，也不无限重试。

## 覆盖

在进入 Episode Digest 前必须满足：

1. 每个 Source 有正时长。
2. 每个 Source 从 0 到源结尾被连续窗口覆盖。
3. 相邻窗满足声明的 overlap。
4. `window_manifest.windows[].id` 与 `window-summaries[].window_id` 集合完全一致。

## Junction 音频证据复用

Source Analysis 的多模态窗口不生成 L-cut 时间码。后续若操作员提交
`right_av_overlap` Junction 约束，编译器只复用 Story QC 已生成并以源文件 SHA-256
寻址的 Demucs + 双路 Silero VAD `speech_intervals`，计算左右尾音在同一输出时间轴上
的同时对白长度；VAD 报告哈希进入 Junction Plan 指纹。缺少任一 Source 的当前 VAD
证据时直接拒绝，不调用模型补猜。

## VFR (Variable Frame Rate) 处理

**问题**：手机拍摄的短剧素材经常是 VFR（可变帧率）。当前 `-ss` before `-i`（fast seek）在 VFR 源上不可靠——关键帧索引可能与可变帧时序不对齐，导致窗口切片起始时间偏移可达 GOP 大小（1-10 秒）。

**检测**：ffprobe 后比较 `r_frame_rate` 与实际帧数/时长的比值。若偏差超过 1%，判定为 VFR。

**处理**：
- VFR 检测到 → 记录 warning，切换到 accurate seek：`-ss` after `-i` 或添加 `-fflags +genpts -avoid_negative_ts make_zero`
- 新增配置项 `force_accurate_seek: bool = False`，用户可手动强制精确 seek
- 常量帧率源保持 fast seek（性能优先）

## FFmpeg Seek 精度验证

**问题**：`_cut_window()` 使用 `-ss` before `-i`（fast seek），实际切出的 clip 起始时间可能偏离目标。当前无验证。

**修复**：切窗后对输出 clip 执行 ffprobe 验证实际起始时间，与目标起始时间比较。偏差超过 1s 时记录 warning。对 VFR 源自动回退到 accurate seek。

## 窗口重叠策略

**当前默认**：window_seconds=240, overlap_seconds=12（5%）。处于标准区间 5-10% 的下沿。

**改进**：
- 默认 overlap 建议提高到 18-24s（7.5-10%），适应短剧快节奏对话
- 新增配置验证：overlap 至少 5% 窗口大小，低于 7.5% 时 warning
- `overlap_seconds` 改为项目级可配置参数，不硬编码

## 重叠区域事件归属

**问题**：两个连续窗口的重叠区域 [next_start, current_end] 被两个窗口分别分析，可能产生重复事件检测。

**归属约定**：重叠区域中，**较早的窗口（index 较小）为权威**。窗口 context 新增 `owned_range` 和 `overlap_range` 字段：
```python
context["ownership"] = {
    "owned_range": [start, next_start],      # 本窗口独占
    "overlap_range": [next_start, end],       # 共享区域，本窗口权威
}
```
下游 event_cards 阶段按此约定去重。

## 压缩质量权衡

**当前**：720p + crf 28 + veryfast preset。对人脸识别和文字识别（手机屏幕、路牌）处于临界状态。

**改进**：
- 默认 CRF 从 28 改为 26（文件体积增约 20%，文字/人脸细节显著改善）
- CRF 改为可配置参数 `window_crf: int = 26`
- 窗口 clip 仅用于 VLM 分析，渲染阶段**必须**使用原始视频——窗口 clip 在 VLM 分析完成后可选择性删除以节省磁盘