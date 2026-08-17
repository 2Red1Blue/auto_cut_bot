# VLM-first architecture: conditional legacy fallback.
# asr_transcript 仅在 confidence_check 判定 VLM 无硬字幕 / 低置信度时触发。
# 正常情况下被 orchestrator._should_skip_asr() 跳过。
"""AsrTranscriptStage — FunASR Paraformer 转录 + 说话人分离 (条件触发)。

Stage 3 in the pipeline (ASR path).
职责:
  - 读取 source_manifest 获取视频文件路径
  - 根据 asr_mode 决定完整 ASR / 抽样验证 / 跳过
  - 调用 FunASR 端点逐文件转录 (含说话人识别)
  - 落库 subtitles、speaker_mappings、boundaries
  - 与 API 字幕交叉验证 (LCS 比率), 分歧时标记 quality_issue
  - 发布 asr_transcript 产物到 ArtifactBus

依赖: FunASR 自部署端点 (config.asr_endpoint), 不可用时降级为 unavailable 状态。
"""

from __future__ import annotations

import json
import math
import random
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    load_json,
    sha256_file,
    update_project_stage,
)
from autocut_core.db.client import StageDBClient
from autocut_core.errors import ArtifactNotFoundError
from autocut_core.logging import fields, get_logger
from autocut_core.version import STAGE_VERSIONS

logger = get_logger(__name__)

# ── 常量 ──────────────────────────────────────────────────────────────────────

_ASR_SOURCE = "asr"
_API_SOURCE = "api"
_DEFAULT_COVERAGE_THRESHOLD = 0.8
_VALIDATE_SAMPLE_RATE = 0.2
_FFMPEG_TIMEOUT = 300  # seconds — ffmpeg 提取音频超时 (通常 <30s, 大文件也给够)
_HTTP_TIMEOUT = 600  # seconds — 单次 HTTP 请求超时 (nginx 代理超时通常 60-120s, 超过也没用)
_MAX_RETRIES = 5  # 总重试次数 (含 502 专用重试)
_RETRY_BACKOFF_BASE = 10.0  # seconds — 指数退避基数
_RETRY_BACKOFF_MAX = 120.0  # seconds — 退避上限
_ASR_WORKERS_DEFAULT = 3  # 默认并发数


class AsrTranscriptStage(Stage):
    """FunASR Paraformer 转录 Stage — 逐视频文件调用 ASR 端点, 落库与交叉验证。"""

    @property
    def contract(self) -> StageContract:
        return StageContract(
            stage_name="asr_transcript",
            input_artifacts=["source_windows"],
            output_artifacts=["asr_transcript"],
            description="FunASR Paraformer transcription + speaker diarization + cross-validation",
            db_reads=["subtitles"],
            db_writes=["subtitles", "speaker_mappings", "boundaries"],
        )

    def prepare(self, bus: ArtifactBus) -> list[Task]:
        """从 ArtifactBus 读取 source_manifest, 构建每源的转录任务。"""
        # 精确取 source_manifest 产物, 不用 latest() (会拿到最后发布的 window_batch)
        ref = bus.resolve("source_windows", "source_manifest")
        if ref is None:
            raise ArtifactNotFoundError("上游 source_windows/source_manifest 产物未找到, 无法构建 ASR 任务")

        artifacts = bus.get(ref)
        source_manifest = _resolve_source_manifest(artifacts, bus, ref)
        if source_manifest is None:
            raise ArtifactNotFoundError("source_manifest 未在 source_windows 产物中找到")

        tasks: list[Task] = []
        for source in source_manifest.get("sources", []):
            if not isinstance(source, dict):
                continue
            source_path = source.get("path")
            if not source_path:
                continue
            tasks.append(
                Task(
                    type="asr_transcribe",
                    payload={
                        "source_id": source.get("id", ""),
                        "episode": source.get("episode"),
                        "path": source_path,
                        "duration_seconds": source.get("duration_seconds", 0),
                    },
                )
            )

        if not tasks:
            logger.warning("source_manifest 中无可转录的源文件, 跳过 ASR")
        return tasks

    def execute(self, bus: ArtifactBus, tasks: list[Task]) -> list[Artifact]:
        """执行 ASR 转录主流程: 模式判断 → 逐源转录 → 落库 → 交叉验证 → 发布产物。"""
        cfg = self.config
        if cfg.job_root is None:
            raise RuntimeError("job_root 未设置")

        job_root: Path = cfg.job_root
        asr_mode = cfg.asr_mode
        asr_endpoint = cfg.asr_endpoint
        asr_language = getattr(cfg, "asr_language", "") or ""
        coverage_threshold = getattr(cfg, "asr_api_coverage_threshold", _DEFAULT_COVERAGE_THRESHOLD)

        db = StageDBClient(db_url=cfg.db_url, schema=cfg.db_schema)
        book_id = cfg.extra.get("book_id", "")
        if not book_id:
            logger.warning("book_id 未配置, ASR 结果不会落库 — 仅产出转录产物")

        # ── 探测 ASR 端点可用性 ──────────────────────────────────────
        endpoint_available = _probe_endpoint(asr_endpoint)
        if not endpoint_available:
            logger.warning(
                "ASR 端点不可用: %s — 转录降级为 unavailable 状态", asr_endpoint,
                extra=fields(stage="asr_transcript"),
            )
            return _publish_unavailable(bus, job_root, book_id, asr_endpoint, tasks)

        # ── 并发转录 ─────────────────────────────────────────────────
        all_results: list[dict[str, Any]] = []
        summary_rows: list[dict[str, Any]] = []

        # 分离需要转录和跳过的任务
        transcribe_tasks: list[dict[str, Any]] = []
        for task in tasks:
            payload = task.payload
            source_id = payload.get("source_id", "")
            episode = payload.get("episode")
            source_path = payload["path"]

            should_transcribe = _decide_transcription(
                asr_mode=asr_mode,
                db=db,
                book_id=book_id,
                episode_id=episode,
                coverage_threshold=coverage_threshold,
            )

            if not should_transcribe:
                logger.info(
                    "跳过 ASR 转录: source=%s episode=%s mode=%s",
                    source_id, episode, asr_mode,
                    extra=fields(stage="asr_transcript"),
                )
                summary_rows.append({
                    "source_id": source_id,
                    "episode": episode,
                    "status": "skipped",
                    "mode": asr_mode,
                    "segments": 0,
                })
                continue
            transcribe_tasks.append({
                "source_id": source_id,
                "episode": episode,
                "source_path": source_path,
            })

        # 并发调用 FunASR — ASR 不依赖上下文, 天然可并行
        if transcribe_tasks:
            workers = min(len(transcribe_tasks), _ASR_WORKERS_DEFAULT)
            logger.info(
                "ASR 并发转录: %d 源, workers=%d",
                len(transcribe_tasks), workers,
                extra=fields(stage="asr_transcript"),
            )
            with ThreadPoolExecutor(max_workers=workers) as pool:
                future_map = {
                    pool.submit(
                        _call_funasr, asr_endpoint, t["source_path"], t["source_id"],
                        language=asr_language,
                    ): t
                    for t in transcribe_tasks
                }
                for future in as_completed(future_map):
                    t = future_map[future]
                    source_id = t["source_id"]
                    episode = t["episode"]
                    try:
                        asr_result = future.result()
                    except Exception as exc:
                        logger.error(
                            "ASR 并发任务异常: source=%s error=%s",
                            source_id, exc,
                            extra=fields(stage="asr_transcript"),
                        )
                        summary_rows.append({
                            "source_id": source_id,
                            "episode": episode,
                            "status": "error",
                            "mode": asr_mode,
                            "error": f"future_exception: {exc}",
                            "segments": 0,
                        })
                        continue

                    if asr_result is None:
                        logger.warning(
                            "ASR 调用失败: source=%s episode=%s", source_id, episode,
                            extra=fields(stage="asr_transcript"),
                        )
                        summary_rows.append({
                            "source_id": source_id,
                            "episode": episode,
                            "status": "error",
                            "mode": asr_mode,
                            "error": "endpoint_call_failed",
                            "segments": 0,
                        })
                        continue

                    segments = _parse_segments(asr_result)
                    text = _parse_full_text(asr_result)

                    all_results.append({
                        "source_id": source_id,
                        "episode": episode,
                        "text": text,
                        "segments": segments,
                        "raw": asr_result,
                    })

                    # 落库
                    if book_id and db.is_available:
                        _persist_to_db(
                            db=db,
                            book_id=book_id,
                            episode_id=episode,
                            source_id=source_id,
                            segments=segments,
                        )

                    summary_rows.append({
                        "source_id": source_id,
                        "episode": episode,
                        "status": "completed",
                        "mode": asr_mode,
                        "segments": len(segments),
                    })

        # ── 交叉验证 ─────────────────────────────────────────────────
        if book_id and db.is_available:
            for result in all_results:
                episode = result["episode"]
                asr_text = result["text"]
                _cross_validate(db, book_id, episode, asr_text, result["source_id"])

        # ── 发布产物 ─────────────────────────────────────────────────
        output_path = job_root / "asr-transcript.json"
        payload = {
            "schema_version": "1.0",
            "asr_endpoint": asr_endpoint,
            "asr_mode": asr_mode,
            "results": all_results,
            "summary": summary_rows,
        }
        atomic_write_json(output_path, payload)

        ref = bus.put(
            "asr_transcript",
            {"path": str(output_path)},
            stage="asr_transcript",
            inputs=[bus.latest("source_windows")] if bus.latest("source_windows") else [],
        )

        update_project_stage(
            job_root / "project.json",
            "asr_transcript",
            "completed",
            outputs={"asr_transcript": ref.sha256},
        )

        return [ref]


# ═══════════════════════════════════════════════════════════════════════════════
# 模式判定
# ═══════════════════════════════════════════════════════════════════════════════


def _decide_transcription(
    *,
    asr_mode: str,
    db: StageDBClient,
    book_id: str,
    episode_id: int | None,
    coverage_threshold: float,
) -> bool:
    """根据 asr_mode 决定是否需要对当前源执行完整 ASR 转录。

    always               → 始终转录
    skip_if_api_complete  → API 字幕覆盖率 > 阈值时跳过
    validate_only         → 仅抽样, 不转录 (此阶段不需完整转录)
    """
    if asr_mode == "always":
        return True

    if episode_id is None:
        return asr_mode != "skip_if_api_complete"

    api_subtitles = db.query_subtitles(book_id, episode_id, source=_API_SOURCE)
    if not api_subtitles:
        # API 无字幕 → 需要 ASR 补充
        return True

    if asr_mode == "skip_if_api_complete":
        coverage = _estimate_coverage(api_subtitles)
        if coverage >= coverage_threshold:
            logger.info(
                "API 字幕覆盖率 %.1f%% >= %.0f%%, 跳过 ASR (book=%s ep=%s)",
                coverage * 100, coverage_threshold * 100, book_id, episode_id,
                extra=fields(stage="asr_transcript"),
            )
            return False
        return True

    if asr_mode == "validate_only":
        # validate_only 也从端点拉结果, 但只抽样部分窗口
        return True

    return True


def _estimate_coverage(subtitles: list[dict[str, Any]]) -> float:
    """估算 API 字幕的时间覆盖率。

    基于字幕的总时间跨度覆盖区间占第一个到最后一个字幕的区间比例。
    返回 [0.0, 1.0] 的比率。
    """
    if not subtitles:
        return 0.0
    if len(subtitles) == 1:
        return 1.0

    starts = [s["start_time"] for s in subtitles]
    ends = [s["end_time"] for s in subtitles]
    total_start = min(starts)
    total_end = max(ends)
    total_span = total_end - total_start
    if total_span <= 0:
        return 1.0

    # 合并重叠区间, 计算覆盖总时长
    intervals = sorted(zip(starts, ends), key=lambda x: x[0])
    merged: list[tuple[float, float]] = []
    for start, end in intervals:
        if merged and start <= merged[-1][1] + 0.01:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    covered = sum(end - start for start, end in merged)
    return min(covered / total_span, 1.0)


# ═══════════════════════════════════════════════════════════════════════════════
# FunASR 端点调用
# ═══════════════════════════════════════════════════════════════════════════════


def _probe_endpoint(endpoint: str) -> bool:
    """探测 ASR 端点是否可达 — 优先 socket 端口探测, 回退 HTTP GET。

    FunASR /recognition 端点只支持 POST, HEAD 会返回 405 导致误判不可用。
    用 socket connect 探测端口可达性更可靠; 若端口可达但 HTTP GET 失败,
    仍认为端点在跑 (FastAPI 会返回 404/405, 但说明服务在线)。
    """
    import socket
    from urllib.parse import urlparse

    parsed = urlparse(endpoint)
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    # 1. socket 端口探测 — 最可靠
    try:
        with socket.create_connection((host, port), timeout=5):
            return True
    except OSError:
        return False

    # 2. (历史回退) HTTP GET 探测 — 不会走到这里
    try:
        req = urllib.request.Request(endpoint, method="GET")
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception:
        return False


def _call_funasr(
    endpoint: str,
    audio_path: str,
    source_id: str,
    *,
    language: str = "",
) -> dict[str, Any] | None:
    """调用 FunASR /recognition 端点, 上传音频文件获取转录结果。

    对于视频文件 (.mp4/.mkv/.mov), 先用 ffmpeg 提取音频再上传。
    language 参数为空时不传 language 字段 (使用服务端默认值)。
    重试策略: 最多 _MAX_RETRIES 次, 指数退避。
    502/503/504 是 nginx 代理超时, FunASR 可能仍在推理 — 增加退避间隔。
    返回 None 表示全部重试失败。
    """
    audio_file = Path(audio_path)
    if not audio_file.is_file():
        logger.error("音频文件不存在: %s", audio_path)
        return None

    # ── 视频文件: 提取音频 (ffmpeg -vn -acodec mp3) ──
    upload_path, cleanup_temp = _extract_audio_if_video(audio_file, source_id)

    # ── 构建 multipart 请求体 ──
    boundary = "----FunASRFormBoundary"
    file_bytes = upload_path.read_bytes()

    body_parts: list[bytes] = []
    # audio field
    body_parts.append(f"--{boundary}\r\n".encode("utf-8"))
    body_parts.append(
        f'Content-Disposition: form-data; name="audio"; filename="{upload_path.name}"\r\n'.encode(
            "utf-8"
        )
    )
    body_parts.append(b"Content-Type: audio/mpeg\r\n\r\n")
    body_parts.append(file_bytes)
    body_parts.append(b"\r\n")
    # 传递 language 参数 — FunASR 支持 "zh"/"en"/"ja"/"ko"/"auto"
    if language:
        body_parts.append(f"--{boundary}\r\n".encode("utf-8"))
        body_parts.append(b'Content-Disposition: form-data; name="language"\r\n\r\n')
        body_parts.append(language.encode("utf-8"))
        body_parts.append(b"\r\n")
    body_parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(body_parts)

    last_error: str | None = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(
                endpoint,
                data=body,
                headers={
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "Content-Length": str(len(body)),
                },
                method="POST",
            )
            # 禁用代理 — FunASR 是本地服务，走代理会导致 502
            no_proxy_handler = urllib.request.ProxyHandler({})
            opener = urllib.request.build_opener(no_proxy_handler)
            with opener.open(req, timeout=_HTTP_TIMEOUT) as resp:
                raw = resp.read().decode("utf-8")
                result = json.loads(raw)
                return result
        except urllib.error.HTTPError as exc:
            last_error = str(exc)
            status_code = exc.code
            # 502/503/504: nginx 代理超时, FunASR 推理可能仍在进行
            # 给更长的退避时间, 等 FunASR 完成推理
            is_gateway = status_code in (502, 503, 504)
            backoff = _RETRY_BACKOFF_BASE * (2 ** attempt) if is_gateway else _RETRY_BACKOFF_BASE * (1.5 ** attempt)
            backoff = min(backoff, _RETRY_BACKOFF_MAX)
            if attempt < _MAX_RETRIES:
                logger.warning(
                    "ASR 端点调用失败 (HTTP %d, attempt %d/%d): %s — %.1f 秒后重试",
                    status_code, attempt + 1, _MAX_RETRIES + 1, source_id, backoff,
                    extra=fields(stage="asr_transcript", source_id=source_id),
                )
                time.sleep(backoff)
            else:
                logger.error(
                    "ASR 端点调用全部重试失败: source=%s HTTP=%d error=%s",
                    source_id, status_code, last_error,
                    extra=fields(stage="asr_transcript"),
                )
        except Exception as exc:
            last_error = str(exc)
            backoff = min(_RETRY_BACKOFF_BASE * (1.5 ** attempt), _RETRY_BACKOFF_MAX)
            if attempt < _MAX_RETRIES:
                logger.warning(
                    "ASR 端点调用失败 (attempt %d/%d): %s — %.1f 秒后重试",
                    attempt + 1, _MAX_RETRIES + 1, last_error, backoff,
                    extra=fields(stage="asr_transcript", source_id=source_id),
                )
                time.sleep(backoff)
            else:
                logger.error(
                    "ASR 端点调用全部重试失败: source=%s error=%s",
                    source_id, last_error,
                    extra=fields(stage="asr_transcript"),
                )
        finally:
            if cleanup_temp and attempt >= _MAX_RETRIES:
                _cleanup_temp(upload_path.parent)

    return None


def _extract_audio_if_video(
    audio_file: Path, source_id: str
) -> tuple[Path, bool]:
    """视频文件提取音频为 mp3; 非视频文件直接返回原路径。

    Returns (upload_path, needs_cleanup).
    """
    if audio_file.suffix.lower() not in ('.mp4', '.mkv', '.mov'):
        return audio_file, False

    import subprocess
    import tempfile

    temp_dir = Path(tempfile.mkdtemp(prefix="asr_audio_"))
    temp_audio = temp_dir / f"{audio_file.stem}.mp3"
    try:
        cmd = [
            "ffmpeg", "-y", "-vn",
            "-i", str(audio_file),
            "-acodec", "libmp3lame",
            "-q:a", "4",
            str(temp_audio),
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_FFMPEG_TIMEOUT,
        )
        if result.returncode != 0:
            logger.warning(
                "ffmpeg 音频提取失败 (rc=%d): %s — 回退到原始文件上传",
                result.returncode,
                result.stderr[:200],
                extra=fields(stage="asr_transcript", source_id=source_id),
            )
            _cleanup_temp(temp_dir)
            return audio_file, False
        if temp_audio.is_file():
            logger.info(
                "音频提取: %s → %s (%d bytes)",
                audio_file.name,
                temp_audio.name,
                temp_audio.stat().st_size,
                extra=fields(stage="asr_transcript", source_id=source_id),
            )
            return temp_audio, True
        _cleanup_temp(temp_dir)
        return audio_file, False
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.warning(
            "ffmpeg 不可用或超时: %s — 回退到原始文件上传",
            exc,
            extra=fields(stage="asr_transcript", source_id=source_id),
        )
        _cleanup_temp(temp_dir)
        return audio_file, False


def _cleanup_temp(temp_dir: Path) -> None:
    """清理临时目录。"""
    import shutil

    try:
        shutil.rmtree(temp_dir, ignore_errors=True)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# 结果解析
# ═══════════════════════════════════════════════════════════════════════════════


def _parse_segments(asr_result: dict[str, Any]) -> list[dict[str, Any]]:
    """从 FunASR 返回结果中提取逐句段时间戳和文本。

    FunASR /recognition 返回格式:
      {"text": "...", "sentences": [{"text": "...", "start": 0.0, "end": 1.0}, ...], "code": 0}
    """
    segments: list[dict[str, Any]] = []
    sentences = asr_result.get("sentences", [])
    for sentence in sentences:
        if not isinstance(sentence, dict):
            continue
        seg_text = sentence.get("text", "").strip()
        if not seg_text:
            continue
        segments.append(
            {
                "start_time": sentence.get("start", 0.0),
                "end_time": sentence.get("end", 0.0),
                "text": seg_text,
            }
        )
    return segments


def _parse_full_text(asr_result: dict[str, Any]) -> str:
    """从 FunASR 返回结果中提取完整文本。"""
    return asr_result.get("text", "").strip()


# ═══════════════════════════════════════════════════════════════════════════════
# 数据库落库
# ═══════════════════════════════════════════════════════════════════════════════


def _persist_to_db(
    *,
    db: StageDBClient,
    book_id: str,
    episode_id: int | None,
    source_id: str,
    segments: list[dict[str, Any]],
) -> None:
    """将 ASR 转录结果写入 subtitles、speaker_mappings、boundaries 三表。

    说话人分离: 当前阶段 speaker=null, 后续 speaker_resolution 阶段填充。
    speaker_mappings: 按 speaker_label 插入 (当前为 null, 保留占位)。
    boundaries: 为每个句子插入 dialogue 类型边界事件。
    """
    if episode_id is None:
        logger.warning("episode_id 为空, 跳过 ASR 结果落库 (source=%s)", source_id)
        return

    # ── 1. subtitles ──────────────────────────────────────────────────
    subtitle_segments: list[dict[str, Any]] = []
    for seg in segments:
        subtitle_segments.append(
            {
                "start_time": seg["start_time"],
                "end_time": seg["end_time"],
                "text": seg["text"],
                "speaker": None,  # 说话人分离由后续阶段处理
                "source": _ASR_SOURCE,
                "confidence": _estimate_confidence(seg),
                "cer_estimate": None,  # CER 由交叉验证阶段计算
            }
        )

    inserted = db.insert_subtitles(
        book_id, episode_id, subtitle_segments, source=_ASR_SOURCE,
    )
    logger.info(
        "ASR subtitles 落库: book=%s ep=%s count=%d/%d",
        book_id, episode_id, inserted, len(subtitle_segments),
        extra=fields(stage="asr_transcript"),
    )

    # ── 2. speaker_mappings ───────────────────────────────────────────
    # 当前阶段不进行说话人识别, 插入占位映射
    speaker_labels: set[str] = set()
    for seg in segments:
        spk = seg.get("speaker")
        if spk:
            speaker_labels.add(spk)

    speaker_mappings: list[dict[str, Any]] = []
    for label in sorted(speaker_labels):
        speaker_mappings.append(
            {
                "speaker_label": label,
                "mapped_subject_id": None,
                "confidence": 0.0,
                "resolved_by": "asr",
            }
        )

    if speaker_mappings:
        db.upsert_speaker_mappings(book_id, episode_id, speaker_mappings)

    # ── 3. boundaries ─────────────────────────────────────────────────
    boundaries: list[dict[str, Any]] = []
    for idx, seg in enumerate(segments, start=1):
        boundaries.append(
            {
                "boundary_id": f"asr-{source_id}-{idx:04d}",
                "episode_id": episode_id,
                "event_type": "dialogue",
                "start_time": seg["start_time"],
                "end_time": seg["end_time"],
                "description": seg["text"][:200],
                "subjects": [],
                "source_table": _ASR_SOURCE,
                "source_id": f"asr-{source_id}-{idx:04d}",
                "confidence": "medium",
                "precision": 2.0,
            }
        )

    if boundaries:
        db.insert_boundaries(book_id, boundaries)


def _estimate_confidence(segment: dict[str, Any]) -> float | None:
    """估算 ASR 句段的置信度 — 基于文本长度和时间跨度。

    短文本 (< 2 字符) 或极短时间跨度 (< 0.1s) 视为低置信度。
    返回 None 表示无法估算。
    """
    text = segment.get("text", "")
    if not text:
        return None
    duration = segment.get("end_time", 0) - segment.get("start_time", 0)
    if len(text) < 2 or duration < 0.05:
        return 0.3
    if len(text) < 5 or duration < 0.2:
        return 0.6
    return 0.85


# ═══════════════════════════════════════════════════════════════════════════════
# 交叉验证 (LCS ratio)
# ═══════════════════════════════════════════════════════════════════════════════


def _cross_validate(
    db: StageDBClient,
    book_id: str,
    episode_id: int | None,
    asr_text: str,
    source_id: str,
) -> None:
    """与 API 字幕进行 LCS 比率交叉验证。

    比较 ASR 完整文本与 API 字幕拼接文本的 LCS 比率。
    分歧超过阈值时记录 quality_issue 日志 (后续可扩展为 quality_issues 表写入)。
    """
    if episode_id is None or not asr_text:
        return

    api_subtitles = db.query_subtitles(book_id, episode_id, source=_API_SOURCE)
    if not api_subtitles:
        logger.info("无 API 字幕可供交叉验证: book=%s ep=%s", book_id, episode_id)
        return

    api_text = "".join(s.get("text", "") for s in api_subtitles)
    if not api_text:
        return

    lcs_ratio = _compute_lcs_ratio(asr_text, api_text)

    logger.info(
        "ASR/API 交叉验证: book=%s ep=%s LCS=%.4f asr_len=%d api_len=%d",
        book_id, episode_id, lcs_ratio, len(asr_text), len(api_text),
        extra=fields(stage="asr_transcript"),
    )

    if lcs_ratio < 0.5:
        logger.warning(
            "ASR/API 字幕分歧显著: book=%s ep=%s LCS=%.4f — 标记 quality_issue",
            book_id, episode_id, lcs_ratio,
            extra=fields(
                stage="asr_transcript",
                quality_issue="asr_api_divergence",
                source_id=source_id,
            ),
        )


def _compute_lcs_ratio(a: str, b: str) -> float:
    """计算两个字符串的 LCS (最长公共子序列) 比率。

    返回 [0.0, 1.0] 的值, 1.0 表示完全一致。
    基于字符级 LCS 长度除以较长字符串长度。
    """
    if not a or not b:
        return 0.0

    len_a, len_b = len(a), len(b)
    # 动态规划: 使用两行滚动数组降低内存
    prev = [0] * (len_b + 1)
    curr = [0] * (len_b + 1)

    for i in range(1, len_a + 1):
        for j in range(1, len_b + 1):
            if a[i - 1] == b[j - 1]:
                curr[j] = prev[j - 1] + 1
            else:
                curr[j] = max(prev[j], curr[j - 1])
        prev, curr = curr, prev

    lcs_len = prev[len_b]
    return lcs_len / max(len_a, len_b)


# ═══════════════════════════════════════════════════════════════════════════════
# 降级发布
# ═══════════════════════════════════════════════════════════════════════════════


def _publish_unavailable(
    bus: ArtifactBus,
    job_root: Path,
    book_id: str,
    asr_endpoint: str,
    tasks: list[Task],
) -> list[Artifact]:
    """ASR 端点不可用时发布降级产物 (status='unavailable')。"""
    output_path = job_root / "asr-transcript.json"
    payload = {
        "schema_version": "1.0",
        "asr_endpoint": asr_endpoint,
        "status": "unavailable",
        "error": f"ASR endpoint {asr_endpoint} is not reachable",
        "results": [],
        "summary": [
            {
                "source_id": t.payload.get("source_id", ""),
                "episode": t.payload.get("episode"),
                "status": "unavailable",
                "mode": "none",
                "segments": 0,
            }
            for t in tasks
        ],
    }
    atomic_write_json(output_path, payload)

    ref = bus.put(
        "asr_transcript",
        {"path": str(output_path), "status": "unavailable"},
        stage="asr_transcript",
        inputs=[bus.latest("source_windows")] if bus.latest("source_windows") else [],
    )

    update_project_stage(
        job_root / "project.json",
        "asr_transcript",
        "completed",
        outputs={"asr_transcript": ref.sha256},
    )

    return [ref]


# ═══════════════════════════════════════════════════════════════════════════════
# 产物解析辅助
# ═══════════════════════════════════════════════════════════════════════════════


def _resolve_source_manifest(
    artifacts: dict[str, Any],
    bus: ArtifactBus,
    ref: Artifact,
) -> dict[str, Any] | None:
    """从 source_windows 产物中解析 source_manifest 内容。

    支持多种产物结构: 直接嵌套的 source_manifest key, 或通过 project.json 回退。
    """
    if isinstance(artifacts, dict):
        manifest = artifacts.get("source_manifest")
        if isinstance(manifest, dict):
            return manifest
        # 顶层可能是路径引用
        for key in ("source_manifest", "manifest_path", "path"):
            val = artifacts.get(key)
            if isinstance(val, str) and Path(val).is_file():
                return load_json(Path(val))

    # project.json 回退
    project = bus.get(ref)
    if isinstance(project, dict):
        stages = project.get("stages", {})
        sw = stages.get("source_windows", {}) if isinstance(stages, dict) else {}
        outputs = sw.get("outputs", {}) if isinstance(sw, dict) else {}
        path = outputs.get("source_manifest")
        if isinstance(path, str) and Path(path).is_file():
            return load_json(Path(path))

    return None
