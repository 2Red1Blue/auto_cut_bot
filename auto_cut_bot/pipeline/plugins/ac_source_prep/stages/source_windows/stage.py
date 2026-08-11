"""SourceWindowsStage — 扫描视频源并生成滑动窗口清单 (流水线第一站)。

职责: 发现本地/远程视频源 → ffprobe 探测 → 按配置的窗口时长/
重叠时长切分滑动窗口 → 产出三个落盘清单:
  - source_manifest.json: 视频源清单 (时长/流信息/SHA);
  - window_manifest.json: 窗口清单 (起止时间 + 前后窗口链);
  - window-analysis-batch.json: 下游 window_analysis 的语义批处理任务单。

在流水线中的位置: _PIPELINE_ORDER 第一个 Stage, 无上游依赖;
下游 window_analysis 消费 window_batch 产物。
"""

from __future__ import annotations

import json
import math
import re
import subprocess
import urllib.parse
from pathlib import Path
from typing import Any

from autocut_core import (
    ArtifactBus,
    Artifact,
    PipelineConfig,
    Stage,
    StageContract,
    Task,
    atomic_write_json,
)
from autocut_core.errors import ConfigError
from autocut_core.io import update_project_stage
from autocut_core.version import STAGE_VERSIONS

from .contracts import (
    VIDEO_SUFFIXES,
    sliding_windows,
)

# 版本集中在 autocut_core.version 管理
WINDOW_ANALYSIS_STAGE_VERSION = STAGE_VERSIONS["source_windows"]


class SourceWindowsStage(Stage):
    """扫描本地或远程视频源 → 窗口清单 + 语义批处理任务单。"""

    @property
    def contract(self) -> StageContract:
        return StageContract(
            stage_name="source_windows",
            output_artifacts=["source_manifest", "window_manifest", "window_batch", "scene_boundaries"],
            description="Scan video sources, detect scene cuts, and generate sliding-window manifest",
            db_reads=[],
            db_writes=["boundaries"],
        )

    def prepare(self, bus: ArtifactBus) -> list[Task]:
        """本 Stage 无上游依赖 — 直接摄入外部视频源。"""
        return [Task(type="source_scan", payload={})]

    def execute(self, bus: ArtifactBus, tasks: list[Task]) -> list[Artifact]:
        """执行流程: 扫描源 → 参数校验 → 构建清单/切窗 → 发布产物 → 更新 project.json。"""
        cfg = self.config

        sources = _scan_sources(cfg)
        _validate(sources, cfg)

        job_root = cfg.job_root
        if job_root is None:
            raise ConfigError("job_root 未设置")
        refs = _build_manifests(
            sources,
            job_root=job_root,
            window_seconds=cfg.window_seconds,
            overlap_seconds=cfg.overlap_seconds,
            backend=cfg.backend,
            extract_local=cfg.extra.get("extract_local", True),
            ffmpeg=cfg.extra.get("ffmpeg", "ffmpeg"),
            overwrite=cfg.extra.get("overwrite", False),
        )

        # ── 场景检测: PySceneDetect 逐帧检测镜头切换点 ──
        # 复用已有场景边界 — 场景检测是确定性算法，结果不会变
        existing_scene_path = job_root / "scene_boundaries.json"
        if existing_scene_path.is_file():
            import json as _json
            scene_boundaries = _json.loads(existing_scene_path.read_text())
        else:
            scene_boundaries = _detect_all_scenes(
                sources,
                job_root=job_root,
                threshold=cfg.extra.get("scene_threshold", 27.0),
                min_scene_len=cfg.extra.get("scene_min_length", 1.0),
            )

        # 先记录产物 SHA — project.json 输出语义统一为 {名称: sha256}
        outputs = {ref.name: ref.sha256 for ref in refs}

        # 注册产物到 ArtifactBus — 下游 window_analysis.prepare() 通过
        # bus.latest("source_windows") 消费; 与其他 Stage 的 put 签名一致
        published: list[Artifact] = []
        for ref in refs:
            data: dict[str, Any] = {"path": str(ref.path)}
            if ref.name == "window_batch":
                # 嵌套键兼容下游 prepare() 按 artifacts["window_batch"]["path"] 解析
                data["window_batch"] = {"path": str(ref.path)}
            published.append(bus.put(ref.name, data, stage="source_windows"))

        # 发布场景边界产物
        scene_ref = bus.put("scene_boundaries", scene_boundaries, stage="source_windows")
        published.append(scene_ref)
        outputs["scene_boundaries"] = scene_ref.sha256

        # 状态写入枚举合法值 "completed" — 断点续传时
        # _build_checkpoint 需要能用 StageStatus 解析该值
        update_project_stage(
            job_root / "project.json",
            "source_windows",
            "completed",
            outputs=outputs,
        )

        return published


# ── 视频源扫描 ──────────────────────────────────────────────────────


def _scan_sources(cfg: PipelineConfig) -> list[dict[str, Any]]:
    """发现并探测视频源 — local 模式扫目录, remote 模式读 URL 清单。"""
    job_root = cfg.job_root

    if cfg.source_kind == "remote":
        url_path = cfg.extra.get("url_list")
        if not url_path:
            raise ValueError("remote mode requires --url-list")
        if job_root is None:
            raise ConfigError("job_root 未设置")
        return _scan_remote(Path(url_path), job_root)
    else:
        input_root = cfg.extra.get("input_root")
        if not input_root:
            raise ValueError("local mode requires --input-root")
        return _scan_local(Path(input_root))


def _scan_local(input_root: Path) -> list[dict[str, Any]]:
    """递归扫描本地目录下的视频文件, 逐个 ffprobe 探测并计算 SHA。

    排序保证多次运行源 ID (source-001...) 稳定 — 幂等重跑时
    同一文件获得同一 ID, 下游引用不断裂。
    """
    paths = sorted(
        p
        for p in Path(input_root).expanduser().resolve().rglob("*")
        if p.is_file() and p.suffix.lower() in VIDEO_SUFFIXES
    )
    if not paths:
        raise FileNotFoundError("no supported video files found")

    sources: list[dict[str, Any]] = []
    for index, path in enumerate(paths, start=1):
        episode = _infer_episode(path, index)
        info = _probe(str(path))
        sha = _sha256(path)
        sources.append(
            {
                "id": f"source-{index:03d}",
                "episode": episode,
                "path": str(path.resolve()),
                "duration_seconds": info["duration_seconds"],
                "streams": info["streams"],
                "sha256": sha,
            }
        )
    return sources


def _scan_remote(url_path: Path, job_root: Path) -> list[dict[str, Any]]:
    """解析远程 URL 清单文件 — 兼容 JSON 数组/对象与逐行纯文本格式。

    安全处理: 清单中同时保留 exact_url (执行用) 与 redacted_url
    (去 query/fragment, 落盘公开清单用) — 避免带签名参数的 URL 泄露。
    """
    text = url_path.expanduser().resolve().read_text(encoding="utf-8-sig")
    if text.lstrip().startswith(("[", "{")):
        value = json.loads(text)
        if isinstance(value, dict):
            for key in ("sources", "urls", "video_urls"):
                if key in value:
                    value = value[key]
                    break
        records = value if isinstance(value, list) else []
    else:
        records = [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]

    sources: list[dict[str, Any]] = []
    for index, item in enumerate(records, start=1):
        raw = {"url": item} if isinstance(item, str) else item
        url = raw.get("url", "")
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"item {index}: invalid URL")
        redacted = urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, "", "")
        )
        episode = raw.get("episode", index)
        duration = raw.get("duration_seconds", 0.0)
        try:
            duration_value = float(duration)
        except (TypeError, ValueError):
            duration_value = 0.0
        sources.append(
            {
                "id": f"source-{index:03d}",
                "episode": episode,
                "exact_url": url,
                "redacted_url": redacted,
                "duration_seconds": duration_value if duration_value > 0 else None,
            }
        )
    return sources


# ── 探测/切窗辅助 ───────────────────────────────────────────────────────────────


def _probe(source: str) -> dict[str, Any]:
    """ffprobe 探测视频: 时长/格式/大小/流信息; 时长非法时报错。"""
    completed = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration,format_name,size:"
            "stream=index,codec_type,codec_name,width,height,r_frame_rate",
            "-of", "json", source,
        ],
        capture_output=True, text=True, timeout=120, check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {source}")
    payload = json.loads(completed.stdout)
    duration = float(payload["format"]["duration"])
    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"invalid duration: {source}")
    return {
        "duration_seconds": duration,
        "format_name": payload.get("format", {}).get("format_name"),
        "size_bytes": int(payload.get("format", {}).get("size", 0) or 0),
        "streams": payload.get("streams", []),
    }


def _sha256(path: Path) -> str:
    """计算源文件 SHA-256 — 内容指纹, 源文件变化时下游缓存失效。"""
    from autocut_core.io import sha256_file
    return sha256_file(path)


def _infer_episode(path: Path, fallback: int) -> int:
    """从文件名推断集数: 取文件名中最后一个正整数 (如"第3集"→3);
    无数字时回退为扫描序号。"""
    matches = re.findall(r"\d+", path.stem)
    if not matches:
        return fallback
    values = [int(m) for m in matches if int(m) > 0]
    return values[-1] if values else fallback


def _cut_window(
    source: Path,
    destination: Path,
    start: float,
    end: float,
    *,
    ffmpeg: str,
    overwrite: bool,
) -> None:
    """用 ffmpeg 从源视频切出窗口片段: 限宽 720 + h264 压缩,
    控制单窗口体积以适配 VLM 上传限制; 目标已存在且未指定
    overwrite 时直接跳过 (幂等)。"""
    if destination.is_file() and not overwrite:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg,
        "-hide_banner", "-loglevel", "error",
        "-y" if overwrite else "-n",
        "-ss", f"{start:.6f}",
        "-t", f"{end - start:.6f}",
        "-i", str(source),
        "-map", "0:v:0", "-map", "0:a:0?",
        "-vf", "scale='min(720,iw)':-2,format=yuv420p",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "32",
        "-c:a", "aac", "-b:a", "96k",
        "-movflags", "+faststart",
        str(destination),
    ]
    try:
        completed = subprocess.run(command, check=False)
    except FileNotFoundError as exc:
        # ffmpeg 可执行文件不存在 — 抛配置类异常并给出安装指引
        raise ConfigError(
            f"ffmpeg 不可用 (命令: {ffmpeg}) — 窗口切片依赖 ffmpeg, 请先安装 "
            "(macOS: brew install ffmpeg; Debian/Ubuntu: apt-get install ffmpeg)"
        ) from exc
    if completed.returncode != 0 or not destination.is_file():
        raise RuntimeError(f"ffmpeg window extraction failed: {destination}")


def _validate(sources: list[dict[str, Any]], cfg: PipelineConfig) -> None:
    """窗口参数合法性校验 — 窗口时长在允许区间内, 重叠时长
    至少 8 秒且小于窗口时长 (重叠不足会导致跨窗事件丢失)。"""
    if not (cfg.window_min_seconds <= cfg.window_seconds <= cfg.window_max_seconds):
        raise ValueError(
            f"window_seconds must be {cfg.window_min_seconds}..{cfg.window_max_seconds}"
        )
    if cfg.overlap_seconds < 8 or cfg.overlap_seconds >= cfg.window_seconds:
        raise ValueError("overlap must be ≥8s and < window_seconds")


def _build_manifests(
    sources: list[dict[str, Any]],
    *,
    job_root: Path,
    window_seconds: float,
    overlap_seconds: float,
    backend: str,
    extract_local: bool = True,
    ffmpeg: str = "ffmpeg",
    overwrite: bool = False,
) -> list[Artifact]:
    """构建三个产物: source_manifest、window_manifest 和 window-analysis 批次。

    核心循环: 按集数排序逐源处理 → 滑动窗口切分 → 为每个窗口
    写上下文文件 (含前后窗口链, 支撑跨窗事件合并) 和批处理 job。
    集数重复直接报错 — 同一集多份源会导致下游归并歧义。
    """
    source_entries: list[dict[str, Any]] = []
    window_records: list[dict[str, Any]] = []
    jobs: list[dict[str, Any]] = []
    seen: set[int] = set()

    contexts_dir = job_root / "intermediate" / "window-contexts"
    outputs_dir = job_root / "window-results"
    assets_dir = job_root / "window-assets"

    for source in sorted(sources, key=lambda s: (s["episode"], s["id"])):
        ep = source["episode"]
        if ep in seen:
            raise ValueError(f"duplicate episode: {ep}")
        seen.add(ep)

        public: dict[str, Any] = {
            "id": source["id"],
            "episode": ep,
            "duration_seconds": source["duration_seconds"],
            "streams": source.get("streams", []),
        }
        if source.get("path"):
            public["path"] = source["path"]
            public["sha256"] = source["sha256"]
        else:
            public["url"] = source["redacted_url"]
        source_entries.append(public)

        ranges = sliding_windows(source["duration_seconds"], window_seconds, overlap_seconds)
        for idx, (start, end) in enumerate(ranges, start=1):
            wid = f"{source['id']}-w{idx:03d}"
            prev_id = f"{source['id']}-w{idx - 1:03d}" if idx > 1 else None
            next_id = f"{source['id']}-w{idx + 1:03d}" if idx < len(ranges) else None

            context = {
                "schema_version": "1.0",
                "source_id": source["id"],
                "episode": ep,
                "window_id": wid,
                "window": {"start": start, "end": end},
                "previous_window_id": prev_id,
                "next_window_id": next_id,
            }
            ctx_path = contexts_dir / f"{wid}.json"
            atomic_write_json(ctx_path, context)

            out_path = outputs_dir / f"{wid}.json"
            job: dict[str, Any] = {
                "id": wid,
                "task": "window_analysis",
                "stage_version": WINDOW_ANALYSIS_STAGE_VERSION,
                "source_id": source["id"],
                "episode": ep,
                "window_id": wid,
                "start": start,
                "end": end,
                "context_file": str(ctx_path.resolve()),
                "output": str(out_path.resolve()),
            }
            if source.get("path"):
                # 本地源: media_file 指向切出的窗口视频片段 (window-assets),
                # 而非整源视频 — 控制 VLM 单次输入体积。
                asset = assets_dir / source["id"] / f"{wid}.mp4"
                if extract_local:
                    _cut_window(
                        Path(source["path"]),
                        asset,
                        start,
                        end,
                        ffmpeg=ffmpeg,
                        overwrite=overwrite,
                    )
                job["media_file"] = str(asset.resolve())
            else:
                job["media_url"] = source.get("exact_url", source.get("redacted_url", ""))
                job["media_url_mode"] = "full_source"

            jobs.append(job)
            window_records.append(
                {
                    "id": wid,
                    "source_id": source["id"],
                    "episode": ep,
                    "start": start,
                    "end": end,
                    "previous_window_id": prev_id,
                    "next_window_id": next_id,
                }
            )

    # 落盘三个产物文件
    source_path = job_root / "source_manifest.json"
    window_path = job_root / "window_manifest.json"
    batch_path = job_root / "window-analysis-batch.json"

    atomic_write_json(source_path, {
        "schema_version": "1.0",
        "sources": source_entries,
    })
    atomic_write_json(window_path, {
        "schema_version": "1.0",
        "window_seconds": window_seconds,
        "overlap_seconds": overlap_seconds,
        "windows": window_records,
    })
    atomic_write_json(batch_path, {
        "schema_version": "1.0",
        "backend": backend,
        "jobs": jobs,
    })

    # 返回 Artifact 引用 — 注册到 ArtifactBus 由 execute() 统一完成
    from autocut_core.io import sha256_file as _sf
    return [
        Artifact(
            stage="source_windows", name="source_manifest",
            sha256=_sf(source_path), path=source_path,
        ),
        Artifact(
            stage="source_windows", name="window_manifest",
            sha256=_sf(window_path), path=window_path,
        ),
        Artifact(
            stage="source_windows", name="window_batch",
            sha256=_sf(batch_path), path=batch_path,
        ),
    ]


# ── 场景检测: PySceneDetect ──────────────────────────────────────────────────


def _detect_all_scenes(
    sources: list[dict[str, Any]],
    *,
    job_root: Path,
    threshold: float = 27.0,
    min_scene_len: float = 1.0,
) -> dict[str, Any]:
    """对每个视频源运行 PySceneDetect 检测镜头切换点。

    PySceneDetect 逐帧计算相邻帧的视觉差异（直方图/亮度/内容变化），
    当差异超过阈值时判定为镜头切换。确定性算法，不依赖 AI 模型，
    时间戳精度为帧级（~40ms）。
    """
    import scenedetect
    from autocut_core.io import atomic_write_json

    episodes: dict[str, list[float]] = {}
    total_cuts = 0

    for source in sources:
        video_path = source.get("path")
        if not video_path or not Path(video_path).is_file():
            continue

        episode = str(source.get("episode", source.get("id", "?")))
        cuts = _detect_scene_cuts(
            video_path,
            threshold=threshold,
            min_scene_len=min_scene_len,
        )
        episodes[episode] = cuts
        total_cuts += len(cuts)

    result = {
        "schema_version": "1.0",
        "detector": "PySceneDetect",
        "threshold": threshold,
        "min_scene_len": min_scene_len,
        "total_cuts": total_cuts,
        "episodes": episodes,
    }

    output_path = job_root / "scene_boundaries.json"
    atomic_write_json(output_path, result)

    return result


def _detect_scene_cuts(
    video_path: str,
    *,
    threshold: float = 27.0,
    min_scene_len: float = 1.0,
) -> list[float]:
    """对单个视频文件运行 PySceneDetect，返回镜头切换时间戳列表。

    使用 ContentDetector (基于 HSV 直方图差异) 检测画面内容突变。
    返回每个镜头切换点的时间戳 (秒)，按时间升序排列。
    第一个切点总是 0.0 (视频开始)。
    """
    from scenedetect import open_video, SceneManager, ContentDetector

    video = open_video(video_path)
    scene_manager = SceneManager()
    scene_manager.add_detector(
        ContentDetector(threshold=threshold, min_scene_len=min_scene_len)
    )
    scene_manager.detect_scenes(video)

    scene_list = scene_manager.get_scene_list()
    if not scene_list:
        return [0.0]

    cuts = [round(s[0].seconds, 3) for s in scene_list]
    # 后处理: 过滤闪白和快速运镜造成的误检
    cuts = _filter_false_cuts(cuts, video_path, min_scene_len=1.5)
    return cuts


# ── PySceneDetect 后处理: 过滤闪白/运镜误检 ──────────────────────────────────


def _filter_false_cuts(
    cuts: list[float],
    video_path: str,
    *,
    min_scene_len: float = 1.5,
    flash_window: float = 0.5,
) -> list[float]:
    """过滤 PySceneDetect 的误检切点。

    闪白特征: 切点前后 flash_window 秒内亮度异常飙升 → 跳过
    运镜特征: 相邻切点间隔 < min_scene_len 且画面相似 → 合并

    这是确定性后处理，不改 PySceneDetect 本身。
    """
    if len(cuts) <= 1:
        return cuts

    import cv2
    import numpy as np

    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS)
    if fps <= 0:
        fps = 30.0

    filtered: list[float] = [cuts[0]]  # 始终保留第一个切点

    for i in range(1, len(cuts)):
        current = cuts[i]
        prev = filtered[-1]
        gap = current - prev

        # 1. min_scene_len 过滤: 间隔太短 → 合并
        if gap < min_scene_len:
            continue

        # 2. 闪白检测: 切点帧亮度异常
        if _is_flash_frame(cap, current, fps, flash_window):
            continue

        filtered.append(current)

    cap.release()
    return filtered


def _is_flash_frame(
    cap: "cv2.VideoCapture",
    ts: float,
    fps: float,
    window: float,
) -> bool:
    """检测时间戳 ts 处的帧是否为闪白帧。

    判断: 当前帧亮度 > 相邻帧平均亮度 × 3.0
    """
    import cv2

    frame_idx = int(ts * fps)
    frames: list[float] = []

    for offset in (-int(window * fps), 0, int(window * fps)):
        idx = max(0, frame_idx + offset)
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ret, frame = cap.read()
        if ret and frame is not None:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            frames.append(float(gray.mean()))

    if len(frames) < 3:
        return False

    before, current, after = frames[0], frames[1], frames[2]
    neighbor_avg = (before + after) / 2.0

    # 当前帧亮度 > 相邻帧 3 倍 → 闪白
    return current > neighbor_avg * 3.0
