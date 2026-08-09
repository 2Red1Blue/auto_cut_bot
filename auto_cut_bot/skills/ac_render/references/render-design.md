# Story QC 后本地正式渲染

## 目录

- [目的与边界](#目的与边界)
- [准入条件](#准入条件)
- [正式命令](#正式命令)
- [正式产物](#正式产物)
- [Render Recipe](#render-recipe)
- [高光与正文分隔](#高光与正文分隔)
- [本地素材与输出规格](#本地素材与输出规格)
- [渲染验证](#渲染验证)
- [部分完成与失败恢复](#部分完成与失败恢复)

## 目的与边界

把 QC 后 Winner Publisher 已发布的唯一有效 Story Plan 编译为不可变 Render Recipe，并使用本地
FFmpeg 从原始本地素材生成正式 MP4。正式渲染不读取 QC Proxy，不重新选择
Story、Beat、Span、Block 或时间码。

Story Plan 继续保存故事和原片播放决策；Story QC 默认使用硬切代理暴露真实问题，
存在已编译 Junction Edit 时使用效果态代理；Render Recipe 单独保存包装、编码和
受控 Junction Edit 决策。不得把这些决策回写到基础或派生 Story Plan。

## 准入条件

逐 Story 检查：

1. `story-qc/<story-id>.json.status` 必须为 `approved`；或为 `review` 且人工
   显式打开 `--include-review`；auto 模式只允许使用
   `--include-auto-safe-review`，并要求完整类型化 finding 集合全部命中正式
   `fade_fallback` 白名单。
2. `story-qc-validation.json` 必须能由当前输入重新验证通过。
3. Candidate Arena 必须存在可重算的 `story-plan-winner-selection.json`，正式
   `story-plans/index.json` / `story-qc/index.json` 必须与 Winner 精确一致；旧单 Plan
   模式继续使用 `story-qc-batch.json.story_plan_index_path` 指向的有效 Plan Index；
   发生 Boundary Repair 时必须使用最终 `round-<NN>.plan.json`。
4. QC 报告的 `story_plan_sha256` 必须等于有效 Plan 文件 SHA-256。
5. 原始 `source_manifest.json`、本地 Source Manifest 和本地源文件必须有效。
6. 本地 Source 的 ID、Episode、时长和文件 SHA-256 必须与 QC 输入一致。
7. 若 QC Batch 绑定 Junction Edit Index，逐 Story Edit Plan、约束、有效 Plan 与
   两份 Source Manifest 的 SHA-256 必须保持一致，QC 报告必须已复核同一效果态代理。

`blocked` Story 不得通过参数绕过门禁。人工 `--include-review` 是有审计的宽覆盖；
auto 的 `--include-auto-safe-review` 是窄白名单，当前只接受
`local-audio-fade-fallback-source_start/end`，不得接受对白/因果连续性、视觉/
环境、source-edge human review、无类型 review 或 block finding。QC 报告 status
不改，Recipe Index 分别记录 `include_review` 与 `include_auto_safe_review`。
批次允许只处理 individually 入选的 Story；其余 Story 在 Recipe Index 中保存
跳过原因。

## 正式命令

本地任务的 `source_manifest.json` 已含 `path` 时：

```bash
python3 /absolute/skill/scripts/build_story_render_recipes.py \
  /absolute/job
```

原始输入为远程 URL 时，必须显式使用 Story QC 已验证过的本地下载清单：

```bash
python3 /absolute/skill/scripts/build_story_render_recipes.py \
  /absolute/job \
  --local-source-manifest /absolute/local-download-job/source_manifest.json
```

人工确实决定覆盖任意 `review` 时显式追加 `--include-review`。Auto orchestrator
只追加 `--include-auto-safe-review`，不得同时打开宽覆盖参数。

验证 Recipe：

```bash
python3 /absolute/skill/scripts/validate_story_render_recipes.py \
  /absolute/job
```

本地并发渲染：

```bash
python3 /absolute/skill/scripts/render_story_videos.py \
  /absolute/job --jobs 2
```

验证最终 MP4：

```bash
python3 /absolute/skill/scripts/validate_story_renders.py \
  /absolute/job
```

正式验证不得使用 `--skip-decode-check`。已有输出只有在明确允许替换时才使用
`--overwrite`。

## 正式产物

```text
story-render-recipes/
  index.json
  <story-id>.json
story-render-recipe-validation.json
story-render-review.md

story-renders/
  index.json
  <production-slot>-<story-id>.mp4
story-render-validation.json
```

可重建片段缓存位于：

```text
.render-cache/story-render/<story-id>/<recipe-sha256>/
```

缓存不是业务交付物。正式交付以 `story-renders/index.json`、
`story-render-validation.json` 和对应 MP4 为准。

## Render Recipe

没有 Junction Edit 的 Recipe 保持兼容格式：

```text
schema_version = 1.1
method = approved-qc-local-render-recipe-v1
status = ready_for_render
```

存在新编译的 pair-level Junction Edit 时使用：

```text
schema_version = 2.1
method = approved-qc-local-render-recipe-v3-pair-timeline
status = ready_for_render
```

旧 Recipe 2.0 与旧 Junction Plan v1 仍可验证和读取；重新编译后写 Recipe 2.1，
不会原地改写旧产物。

Recipe 必须绑定：

- Story QC Index 和单 Story QC 报告 SHA-256。
- 原始 Source Manifest 与本地 Source Manifest SHA-256。
- 有效 Story Plan Index 和单 Story Plan SHA-256。
- Story Boundary Repair 元数据 SHA-256。
- 本地源路径、源文件 SHA-256 和源时长。
- 展平后的 Clip、唯一转场和完整播放 Timeline。
- 适用时的 Junction Edit Plan SHA-256、闭合策略、禁画区间、左右音画入口保留
  断言；`reviewed_bridge` 另存静音桥接源区间和逐帧对齐参数，
  `right_av_overlap` 另存 overlap、双路 VAD 同时对白、左右淡化与零音画偏移证明。
- 输出规格、预计时长和输出文件名。

Timeline 使用连续、从 1 开始的 `order`，并保存每项的
`start_seconds`、`end_seconds` 和 `duration_seconds`。正式渲染必须逐字执行
Timeline，不得根据文件名、Episode 或模型摘要重新排序。

`right_av_overlap` 仍使用连续的逐 Clip Timeline：左 Clip 的有效时长截止于
`left_video_end_seconds`，右 Clip 保持原视频时长并带
`incoming_junction_edit_id`。渲染右 Clip 时，右侧原画面和原声都从
`source_start` 同步开始，左侧从安全画面点到 Plan 音频终点的尾音混入右侧头部。
因此 Recipe 的 `source_duration_seconds` 与 `expected_duration_seconds` 都比原 Plan
串行总时长减少 overlap；QC Proxy 与正式渲染调用同一 pair media primitive。

## 高光与正文分隔

Render Recipe 必须按 Story Plan 的 Teaser 模式生成转场：

- `single_highlight`：首个 Story Block 必须是 `teaser`，后面至少有一个正文
  Block；在 Teaser 最后一个 Clip 与正文第一个 Clip 之间插入且只插入一次下述
  `black_separator`。
- `mode=none`：所有 Block 均为正文，不得出现 Teaser Block；`transitions=[]`、
  `transition_count=0`、`transition_duration_seconds=0`，不得插入黑场或对应 fade。

`single_highlight` 的唯一转场为：

```json
{
  "type": "black_separator",
  "duration_seconds": 0.35,
  "audio_policy": "silence",
  "fade_out_seconds": 0.18,
  "fade_in_seconds": 0.18,
  "fade_curve": "tri"
}
```

- `fade_out_seconds`：Teaser 末段 Clip 尾部 0.18 秒的视频与音频线性淡出（fade
  → 全黑 + `afade curve=tri` → 静音），在其自己的时长内完成，不改变 Clip 边界或
  播放位置。
- `fade_in_seconds`：正文首段 Clip 头部 0.18 秒的视频与音频线性淡入，从全黑/
  静音进入原始亮度和音量，同样在其自己的时长内完成。
- fade 包络由 `render_story_videos.py` 通过 FFmpeg `fade` / `afade` 滤镜直接
  应用到相邻 Clip 的编码媒体上，作为 Clip 内容的一部分与其时长绑定，因此不占
  用额外时长、不改变 Story Plan 的时间码、也不改变 Recipe 的 `expected_duration_seconds`。
- 最终 stream-copy concat 必须以 Recipe 的 `expected_duration_seconds` 作为
  输出时长上限，避免逐 Clip AAC encoder priming 在容器尾部累计；这只是裁掉
  编码填充，不改变任何 Clip、黑场或 Story Plan 时间码。最终 MP4 仍须通过原有
  实测时长与完整解码验证，不得通过放宽容差掩盖漂移。

Teaser Block 内部和其余 Junction 默认保留 `hard_cut`；`mode=none` 的全部普通
Junction 同样默认硬切。Recipe 2.0 继续兼容旧 `audio_tail_over_bridge`；新编译的
Recipe 2.1 使用闭合的 `reviewed_bridge` / `right_av_overlap`。前者把左侧对白
尾音铺在静音安全桥接画面上；后者在左画面安全点后立即播放右侧原音画，并把左侧
尾音混入右侧头部。两种策略都必须完整保留左侧尾音、保持右侧音画
`source_start` 不变，并证明实际保留/替代画面不与
`forbidden_visual_ranges` 相交。不得：

- 在 Teaser 的每个 Clip 后重复插入黑场。
- 把黑场误插到正文内部。
- 用交叉叠化、跨 Clip xfade、未经编译的正片内 J-cut/L-cut 或与语义无关的静音
  掩盖未通过 QC 的边界。
- 改变本条 fade 的长度、曲线或方向（例如让 Teaser 淡入或让正文淡出）；本地
  fade 只作用于紧邻黑场的两条 Clip 边缘，不覆写 Story Plan 的原片时间码。
- 把默认黑场当成带语义的时间/地点标题卡。

预计成片时长为：

```text
全部 Clip 播放时长之和 + 适用转场时长
```

`single_highlight` 的适用转场时长为 0.35 秒，`mode=none` 为 0 秒。fade 包络在
Clip 自身时长内完成，不额外累计。

## Filler Tail 兜底（v4.13+，v4.14+ 跨集）

Plan 阶段撤除时长下限后，`build_render_recipe` 在生成 Recipe 时检查
`Clip 播放时长之和 + 适用转场时长` 是否达到 **300 秒**。不足时执行两步：

1. **拼到集尾**：把 Plan 最后一个 Clip 沿 `source_end` 延伸至该集物理
   尾部；即使这一步就已经超过 300 秒，也不半途裁剪，让最后一段停在
   自然的集尾。
2. **跨集追加整集**：若上一步之后仍不足 300 秒，按 Source Manifest
   集号递增顺序追加后续集的完整整集 Clip（`source_start=0` → 该集
   物理尾部），直到累计成片时长**首次达到或超过 300 秒**。

规则：

- 只把追加集挂到最后一个正文 Block，不新增 Block；`single_highlight` 不改变
  Teaser 结构，`mode=none` 继续保持无 Teaser。
- 追加集之间使用**硬切**，不新增黑场、fade 或其他转场；仅
  `single_highlight` 保留唯一的 Teaser→正文 0.35 秒黑场，`mode=none` 保持
  `transition_count=0`。
- 每个 filler Clip 的 `filler_tail_seconds` 等于该 Clip 全部作为
  filler 的时长（延伸的末尾 Clip 记录延伸秒数；追加的整集 Clip 记录
  该集全长）。Recipe 顶层 `filler_tail_seconds` 为末尾一段连续 filler
  Clip 的 `filler_tail_seconds` **总和**，`filler_tail_target_seconds=300`。
- 集号方向上的候选集耗尽仍不足 300 秒**不阻断**，产出多少算多少。
- Filler tail 段不做剧情连贯性检查、不进入 Boundary Repair，也不做本地音频
  VAD——集自然结束属于合法退出点，Story QC
  与 Boundary Repair 对整段兜底无感知。

Recipe Validator 允许 `filler_tail_seconds > 0` 出现在 Clip 列表的
**任意末尾连续后缀**上；不允许非末尾 Clip 携带 filler，也不允许 Recipe
顶层字段与所有 Clip 之和不一致。

## 本地素材与输出规格

正式渲染只允许本地文件路径。原始输入为 URL 时，使用与 Source ID、Episode、
去签名 URL 和时长一致且已完成本地音频 QC 的下载清单。不得在正式渲染阶段重新
下载远程素材，也不得把签名 URL 写入 Recipe 或日志。

默认 `delivery` 规格：

```text
1080 × 1920
25 fps
contain + black pad
H.264 libx264 / CRF 18 / medium
yuv420p
AAC / 48 kHz / stereo / 192 kbps
faststart
```

每个 Clip 先归一分辨率、帧率、像素格式、音频采样率和声道。源缺少音轨时补等长
静音，但不得修改有声源的对白内容。最终 MP4 使用临时路径生成，全部检查通过后
原子替换目标文件。

## 渲染验证

正式验证必须：

- 重新验证 Render Recipe 及全部输入哈希。
- 验证输出文件和 Recipe SHA-256。
- 使用 FFprobe 检查 H.264/AAC、1080×1920、25 fps、48 kHz 双声道。
- 校验实测时长与 Recipe 预计时长。
- `single_highlight` 在 Recipe 声明的位置检测唯一黑场和静音区间；`mode=none`
  验证不存在该转场。
- 确认输出覆盖全部 Render Recipe，且没有额外 Story。
- 对完整视频和音频流执行 FFmpeg 解码检查。
- 保存最终 MP4 SHA-256、大小、流信息和逐转场检测结果。

只有 `story-render-validation.json.ok=true` 的 MP4 才是正式结果。

## 部分完成与失败恢复

Recipe Index 状态：

- `complete`：请求数量中的全部入选 Story 均已生成 Recipe。
- `partial`：至少一条入选 Story 已生成 Recipe，其他为 `review`（未打开
  `--include-review`）或 `blocked`。
- `blocked`：没有入选 Story 可渲染。

Render Index 状态：

- `complete`：完整 Recipe 批次全部渲染。
- `partial`：所有入选 Recipe 已渲染，但 QC 批次仍有跳过项；或部分渲染失败。
- `failed`：没有生成任何有效 MP4。

单条渲染失败不得删除其他成功 MP4。修复源文件、Recipe 或有效 Story Plan 后，
相关哈希变化，必须重建受影响 Recipe 并重新渲染。内容寻址缓存只在 Recipe 哈希
不变且缓存媒体通过基础流与时长检查时复用。

当前不支持通用自由 J-cut/L-cut、动态转场、文字标题卡、字幕、水印、BGM、响度
美化、调色、补帧、远程渲染或自动上传。唯一受控音视频分离例外是已编译并经过
效果态 Story QC 的 `audio_tail_visual_repair`，其策略闭合为
`reviewed_bridge` / `right_av_overlap`；旧 `audio_tail_over_bridge` 继续兼容读取。

## 部分重渲染 (Partial Re-render)

### 问题

当前缓存键是 recipe_sha256（整个渲染配方的哈希）。如果 300s 视频有 15 个 clip，仅 1 个 clip 的时间戳变更，其余 14 个 clip 也全部重新渲染。浪费 CPU 时间。

### 方案：内容寻址 Clip 缓存

```python
def clip_cache_key(clip):
    """每个 clip 独立的内容寻址缓存键"""
    return sha256(
        clip["source_sha256"][:16] + "|" +
        str(clip["source_start"]) + "|" +
        str(clip["source_end"]) + "|" +
        clip.get("profile", "default") + "|" +
        str(clip.get("fade_in", 0)) + "|" +
        str(clip.get("fade_out", 0))
    )[:16]
```

### 缓存目录结构

```
旧: cache_root/{recipe_sha256}/clips/{clip_id}.mp4
     ↑ 任何 clip 变更 → 全部重渲染

新: cache_root/clips/{clip_cache_key}.mp4
     ↑ 每个 clip 独立缓存，跨 recipe/story 复用
```

### render_clip() 逻辑

```python
def render_clip(clip, cache_root):
    cache_key = clip_cache_key(clip)
    cached_path = cache_root / "clips" / f"{cache_key}.mp4"

    if cached_path.exists():
        return cached_path  # 缓存命中

    output = render_with_ffmpeg(clip)
    shutil.copy(output, cached_path)  # 写入缓存
    return output
```

### 收益

- 单 clip 修改 → 仅重渲染 1 个 clip（而非 15 个）
- 跨 story 复用：相同源视频+相同时间范围+相同参数 → 缓存命中
- 开发期间迭代渲染配方时显著节省 CPU 时间