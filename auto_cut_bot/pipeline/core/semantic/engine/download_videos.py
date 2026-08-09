#!/usr/bin/env python3
"""Download prepared video URLs concurrently for local QC, VAD, and rendering.

Migrated from _legacy_v4/scripts/download_video_urls.py.
Runnable as a standalone subprocess (independent-process-v1) or importable as a library.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}-", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            import json as _json
            _json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def load_json(path: Path) -> dict[str, Any]:
    import json as _json
    with path.expanduser().resolve().open("r", encoding="utf-8") as handle:
        value = _json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def full_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def sanitize_error(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        parsed = urllib.parse.urlsplit(match.group(0).rstrip(".,);]"))
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return "<url-redacted>"
        return urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path or "/", "", "")
        )

    return re.sub(r"https?://[^\s\"']+", replace, value)


def probe_local(path: Path, ffprobe: str, timeout: float) -> dict[str, Any]:
    command = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration,format_name,size:stream=index,codec_type,codec_name,width,height,r_frame_rate",
        "-of",
        "json",
        str(path),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("local ffprobe timed out") from exc
    if completed.returncode != 0:
        raise RuntimeError("downloaded media failed ffprobe")
    import json as _json
    try:
        payload = _json.loads(completed.stdout)
        duration = float(payload.get("format", {}).get("duration"))
    except (TypeError, ValueError, _json.JSONDecodeError) as exc:
        raise RuntimeError("downloaded media has no valid duration") from exc
    if duration <= 0:
        raise RuntimeError("downloaded media duration must be positive")
    return {
        "duration_seconds": duration,
        "format_name": payload.get("format", {}).get("format_name"),
        "streams": payload.get("streams", []),
    }


def _download_once(
    source: dict[str, Any],
    *,
    timeout: float,
    overwrite: bool,
    ffprobe: str,
    probe_timeout: float,
    skip_media_verify: bool,
) -> dict[str, Any]:
    destination = Path(source["local_path"]).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not overwrite:
        technical = (
            {}
            if skip_media_verify
            else probe_local(destination, ffprobe, probe_timeout)
        )
        return {
            "id": source["id"],
            "status": "reused",
            "path": str(destination),
            "size_bytes": destination.stat().st_size,
            "sha256": full_sha256(destination),
            "technical": technical,
            "completed_at": now_iso(),
        }

    partial = destination.with_name(f".{destination.name}.part")
    existing = partial.stat().st_size if partial.exists() and not overwrite else 0
    if overwrite and partial.exists():
        partial.unlink()
    headers = {
        "User-Agent": "Codex-short-drama-story-first/4.2.0",
        "Accept-Encoding": "identity",
    }
    if existing > 0:
        headers["Range"] = f"bytes={existing}-"
    request = urllib.request.Request(source["url"], headers=headers, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        status = getattr(response, "status", response.getcode())
        append = existing > 0 and status == 206
        mode = "ab" if append else "wb"
        base_size = existing if append else 0
        expected_tail = response.headers.get("Content-Length")
        with partial.open(mode) as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        if expected_tail is not None:
            expected_total = base_size + int(expected_tail)
            if partial.stat().st_size != expected_total:
                raise RuntimeError(
                    f"incomplete download: {partial.stat().st_size} != {expected_total}"
                )
    technical = (
        {}
        if skip_media_verify
        else probe_local(partial, ffprobe, probe_timeout)
    )
    os.replace(partial, destination)
    return {
        "id": source["id"],
        "status": "downloaded",
        "path": str(destination),
        "size_bytes": destination.stat().st_size,
        "sha256": full_sha256(destination),
        "technical": technical,
        "completed_at": now_iso(),
    }


def download_with_retries(
    source: dict[str, Any],
    *,
    timeout: float,
    retries: int,
    overwrite: bool,
    ffprobe: str,
    probe_timeout: float,
    skip_media_verify: bool,
) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            result = _download_once(
                source,
                timeout=timeout,
                overwrite=overwrite,
                ffprobe=ffprobe,
                probe_timeout=probe_timeout,
                skip_media_verify=skip_media_verify,
            )
            result["attempts"] = attempt + 1
            return result
        except (
            OSError,
            RuntimeError,
            ValueError,
            urllib.error.URLError,
        ) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(min(8.0, 2.0**attempt))
    return {
        "id": source["id"],
        "status": "failed",
        "path": source["local_path"],
        "attempts": retries + 1,
        "error": sanitize_error(f"{type(last_error).__name__}: {last_error}"),
        "completed_at": now_iso(),
    }


def update_source_manifest(path: Path, results: dict[str, dict[str, Any]]) -> None:
    manifest = load_json(path)
    sources = manifest.get("sources")
    if not isinstance(sources, list):
        raise ValueError("source manifest requires sources array")
    for source in sources:
        if not isinstance(source, dict) or source.get("id") not in results:
            continue
        result = results[source["id"]]
        source["download"] = {
            key: result.get(key)
            for key in (
                "status",
                "path",
                "size_bytes",
                "sha256",
                "attempts",
                "completed_at",
                "error",
            )
            if result.get(key) is not None
        }
        if result.get("status") in {"downloaded", "reused"}:
            source["path"] = result["path"]
            source["sha256"] = result["sha256"]
            source["local_media"] = result.get("technical", {})
            local_duration = result.get("technical", {}).get("duration_seconds")
            if isinstance(local_duration, (int, float)) and local_duration > 0:
                source["duration_seconds"] = float(local_duration)
    manifest["downloads_ready"] = all(
        result.get("status") in {"downloaded", "reused"}
        for result in results.values()
    )
    atomic_write_json(path, manifest)


def apply_duration_gate(
    source_manifest_path: Path,
    results: dict[str, dict[str, Any]],
) -> None:
    manifest = load_json(source_manifest_path)
    sources = manifest.get("sources")
    if not isinstance(sources, list):
        raise ValueError("source manifest requires sources array")
    expected = {
        source.get("id"): source.get("duration_seconds")
        for source in sources
        if isinstance(source, dict)
    }
    for source_id, result in results.items():
        if result.get("status") not in {"downloaded", "reused"}:
            continue
        remote_duration = expected.get(source_id)
        local_duration = result.get("technical", {}).get("duration_seconds")
        if not isinstance(remote_duration, (int, float)) or not isinstance(
            local_duration, (int, float)
        ):
            continue
        delta = abs(float(remote_duration) - float(local_duration))
        if delta > 0.10:
            result["status"] = "failed"
            result["error"] = (
                "remote/local duration mismatch requires rebuilding windows: "
                f"{float(remote_duration):.3f}s != {float(local_duration):.3f}s"
            )
            result["duration_delta_seconds"] = round(delta, 6)


def run_downloads(
    manifest: dict[str, Any],
    *,
    workers: int = 4,
    timeout: float = 120.0,
    retries: int = 3,
    ffprobe: str = "ffprobe",
    probe_timeout: float = 60.0,
    overwrite: bool = False,
    skip_media_verify: bool = False,
    report_path: Path | None = None,
) -> dict[str, Any]:
    """Core download logic extracted from main() — callable without argparse.

    Returns the final report dict.
    """
    if manifest.get("version") != "1.0":
        raise ValueError("download manifest version must be '1.0'")
    sources = manifest.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("download manifest requires non-empty sources")
    download_root = Path(manifest["download_root"]).expanduser().resolve()
    for index, source in enumerate(sources):
        if not isinstance(source, dict):
            raise ValueError(f"sources[{index}] must be an object")
        for field in ("id", "url", "local_path"):
            if not isinstance(source.get(field), str) or not source[field].strip():
                raise ValueError(f"sources[{index}].{field} is required")
        local_path = Path(source["local_path"]).expanduser().resolve()
        try:
            local_path.relative_to(download_root)
        except ValueError as exc:
            raise ValueError(
                f"sources[{index}].local_path escapes download_root"
            ) from exc

    report_path_resolved = (
        report_path.expanduser().resolve()
        if report_path
        else Path(manifest.get("_manifest_path", ".")).with_name("remote-download-report.json")
    )

    started_at = now_iso()
    results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(
                download_with_retries,
                source,
                timeout=timeout,
                retries=retries,
                overwrite=overwrite,
                ffprobe=ffprobe,
                probe_timeout=probe_timeout,
                skip_media_verify=skip_media_verify,
            ): source
            for source in sources
        }
        for future in as_completed(future_map):
            source = future_map[future]
            result = future.result()
            results[source["id"]] = result
            state = {
                "version": "1.0",
                "status": "running",
                "started_at": started_at,
                "updated_at": now_iso(),
                "results": [
                    results[key] for key in sorted(results)
                ],
            }
            atomic_write_json(report_path_resolved, state)

    source_manifest_value = manifest.get("source_manifest_path")
    source_manifest_path = (
        Path(source_manifest_value).expanduser().resolve()
        if isinstance(source_manifest_value, str) and source_manifest_value
        else None
    )
    if source_manifest_path is not None and not skip_media_verify:
        apply_duration_gate(source_manifest_path, results)
    ordered_results = [results[source["id"]] for source in sources]
    failed = [item for item in ordered_results if item["status"] == "failed"]
    report = {
        "version": "1.0",
        "status": "failed" if failed else "ready",
        "started_at": started_at,
        "finished_at": now_iso(),
        "download_root": str(download_root),
        "counts": {
            "total": len(ordered_results),
            "downloaded": sum(item["status"] == "downloaded" for item in ordered_results),
            "reused": sum(item["status"] == "reused" for item in ordered_results),
            "failed": len(failed),
        },
        "results": ordered_results,
    }
    atomic_write_json(report_path_resolved, report)
    if source_manifest_path is not None:
        update_source_manifest(source_manifest_path, results)
    return report


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--probe-timeout", type=float, default=60.0)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--skip-media-verify", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.workers <= 16:
        parser.error("--workers must be in 1..16")
    if args.timeout <= 0 or args.probe_timeout <= 0:
        parser.error("timeouts must be positive")
    if not 0 <= args.retries <= 10:
        parser.error("--retries must be in 0..10")
    try:
        manifest_path = args.manifest.expanduser().resolve()
        manifest = load_json(manifest_path)
        manifest["_manifest_path"] = str(manifest_path)

        report_path = (
            args.report.expanduser().resolve()
            if args.report
            else manifest_path.with_name("remote-download-report.json")
        )
        if args.dry_run:
            download_root = Path(manifest["download_root"]).expanduser().resolve()
            dry_report = {
                "version": "1.0",
                "status": "planned",
                "download_root": str(download_root),
                "sources": [
                    {
                        "id": source["id"],
                        "url": source.get("url_redacted"),
                        "path": source["local_path"],
                    }
                    for source in manifest["sources"]
                ],
            }
            atomic_write_json(report_path, dry_report)
            print(f"PLANNED\t{report_path}")
            return 0

        run_downloads(
            manifest,
            workers=args.workers,
            timeout=args.timeout,
            retries=args.retries,
            ffprobe=args.ffprobe,
            probe_timeout=args.probe_timeout,
            overwrite=args.overwrite,
            skip_media_verify=args.skip_media_verify,
            report_path=report_path,
        )
        print(f"REPORT\t{report_path}")
        return 0
    except (OSError, ValueError) as exc:
        import json as _json
        try:
            _json.dumps(str(exc))
        except Exception:
            pass
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())