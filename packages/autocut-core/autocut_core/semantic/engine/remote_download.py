"""并行远程下载协调 — 从 semantic_engine.py 提取。

包含:
  - launch_parallel_remote_download: 启动远程视频下载子进程
  - refresh_parallel_download: 刷新下载状态
  - process_is_running: 检查进程存活
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from autocut_core.io import atomic_write_json, load_json, utc_now


def process_is_running(pid: int) -> bool:
    """检查指定 pid 的进程是否存活 (signal 0 探测)。"""
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


def launch_parallel_remote_download(
    manifest: dict[str, Any],
    *,
    manifest_path: Path,
    dry_run: bool,
) -> tuple[dict[str, Any] | None, subprocess.Popen[Any] | None]:
    """启动 (或复用) 并行远程视频下载进程。

    仅当 manifest 配置了 parallel_remote_download 且存在 vlm_analysis
    任务时生效; 已完成或进程存活时直接复用。返回 (启动信息, 子进程句柄),
    未启用时返回 (None, None)。
    """
    config = manifest.get("parallel_remote_download")
    has_window_jobs = any(
        isinstance(item, dict) and item.get("task") in ("vlm_analysis", "window_analysis")
        for item in manifest.get("jobs", [])
    )
    if not isinstance(config, dict) or not has_window_jobs:
        return None, None
    required = ("manifest_path", "report_path", "launch_path", "local_source_manifest_path")
    if any(not isinstance(config.get(key), str) for key in required):
        raise ValueError(
            "parallel_remote_download requires manifest/report/launch/local-source paths"
        )
    download_manifest_path = Path(config["manifest_path"]).expanduser().resolve()
    report_path = Path(config["report_path"]).expanduser().resolve()
    launch_path = Path(config["launch_path"]).expanduser().resolve()
    local_manifest_path = Path(config["local_source_manifest_path"]).expanduser().resolve()
    if not download_manifest_path.is_file():
        raise FileNotFoundError(f"remote download manifest is missing: {download_manifest_path}")
    if report_path.is_file() and local_manifest_path.is_file():
        report = load_json(report_path)
        local_manifest = load_json(local_manifest_path)
        if report.get("status") == "ready" and local_manifest.get("downloads_ready") is True:
            return (
                {"status": "ready", "mode": "reused_completed_download",
                 "report_path": str(report_path), "local_source_manifest_path": str(local_manifest_path)},
                None,
            )
    if launch_path.is_file():
        previous = load_json(launch_path)
        previous_pid = previous.get("pid")
        if isinstance(previous_pid, int) and previous_pid > 0 and process_is_running(previous_pid):
            return (
                {"status": "running", "mode": "reused_live_process", "pid": previous_pid,
                 "launch_path": str(launch_path), "report_path": str(report_path),
                 "local_source_manifest_path": str(local_manifest_path)},
                None,
            )
    workers = int(config.get("workers", 4))
    timeout = float(config.get("timeout_seconds", 120.0))
    retries = int(config.get("retries", 3))
    if not 1 <= workers <= 16 or timeout <= 0 or not 0 <= retries <= 10:
        raise ValueError("parallel_remote_download settings are invalid")
    log_path = manifest_path.parent / "logs" / "remote-video-download.log"
    launch = {
        "schema_version": "1.0", "method": "independent-process-v1",
        "status": "planned" if dry_run else "launching", "started_at": utc_now(),
        "parent_manifest_path": str(manifest_path),
        "download_manifest_path": str(download_manifest_path),
        "report_path": str(report_path),
        "local_source_manifest_path": str(local_manifest_path),
        "log_path": str(log_path), "workers": workers,
        "timeout_seconds": timeout, "retries": retries, "pid": None,
    }
    if dry_run:
        atomic_write_json(launch_path, launch)
        return {**launch, "launch_path": str(launch_path)}, None
    script_path = Path(__file__).resolve().parent / "download_videos.py"
    if not script_path.is_file():
        raise FileNotFoundError(f"remote download worker is missing: {script_path}")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable, str(script_path), str(download_manifest_path),
        "--workers", str(workers), "--timeout", str(timeout),
        "--retries", str(retries), "--report", str(report_path),
    ]
    with log_path.open("a", encoding="utf-8") as log_handle:
        process = subprocess.Popen(
            command, cwd=str(manifest_path.parent),
            stdout=log_handle, stderr=subprocess.STDOUT,
            text=True, start_new_session=True,
        )
    launch["status"] = "running"
    launch["pid"] = process.pid
    atomic_write_json(launch_path, launch)
    return {**launch, "launch_path": str(launch_path)}, process


def refresh_parallel_download(
    launch: dict[str, Any] | None,
    process: subprocess.Popen[Any] | None,
) -> dict[str, Any] | None:
    """刷新并行下载状态: 子进程仍在跑则 running, 结束后按退出码与报告定 ready/failed。"""
    if launch is None:
        return None
    if process is None:
        return launch
    returncode = process.poll()
    if returncode is None:
        return {**launch, "status": "running"}
    report_path = Path(launch["report_path"]).expanduser().resolve()
    report_status = load_json(report_path).get("status") if report_path.is_file() else "missing"
    return {
        **launch,
        "status": "ready" if returncode == 0 and report_status == "ready" else "failed",
        "returncode": returncode, "report_status": report_status,
    }