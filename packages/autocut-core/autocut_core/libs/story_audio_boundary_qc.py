#!/usr/bin/env python3
"""Run the installed dual-track local audio boundary guard for Story Plans."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autocut_core.io import atomic_write_json, load_json, sha256_file


AUDIO_REPORT_VERSION = "1.1"
SAFE_AUDIO_STATUSES = frozenset(
    {"safe", "safe_source_edge", "not_applicable_no_audio"}
)
KNOWN_AUDIO_STATUSES = SAFE_AUDIO_STATUSES | frozenset(
    {"adjustment_required", "blocked_replan", "analysis_error"}
)
PINNED_AUDIO_ENGINES = {
    "demucs": "4.1.0",
    "silero-vad": "6.2.1",
    "onnxruntime": "1.24.3",
}


def canonical_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, "", "")
    )


def audio_guard_default() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "autocut"
        / "scripts"
        / "audio_boundary_guard.py"
    )


def audio_policy(audio_guard_script: Path) -> dict[str, Any]:
    rules_path = (
        audio_guard_script.expanduser().resolve().parent.parent
        / "references"
        / "qc-rules.json"
    )
    if rules_path.is_file():
        value = load_json(rules_path)
        policy = value.get("audio_boundary") if isinstance(value, dict) else None
    else:
        fallback_path = (
            Path(__file__).resolve().parent.parent
            / "references"
            / "story-audio-boundary-policy.json"
        )
        policy = load_json(fallback_path)
    if not isinstance(policy, dict):
        raise ValueError(f"audio boundary policy is unavailable: {rules_path}")
    return policy


def ordered_plan_clips(plan: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for block in sorted(plan["blocks"], key=lambda item: item["play_order"]):
        for clip in block["clips"]:
            records.append(
                {
                    **clip,
                    "_block_id": block["id"],
                    "_block_role": block["role"],
                }
            )
    return records


def local_source_map(
    source_manifest: dict[str, Any],
    local_source_manifest: dict[str, Any] | None,
    required_source_ids: set[str],
) -> list[dict[str, Any]]:
    primary = {
        item["id"]: item
        for item in source_manifest.get("sources", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    local = {
        item["id"]: item
        for item in (
            local_source_manifest.get("sources", [])
            if isinstance(local_source_manifest, dict)
            else []
        )
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    records: list[dict[str, Any]] = []
    for source_id in sorted(required_source_ids):
        source = primary.get(source_id)
        if source is None:
            raise ValueError(f"Story Plans reference unknown Source {source_id}")
        candidate = source
        path_value = source.get("path")
        if not isinstance(path_value, str) or not Path(
            path_value
        ).expanduser().resolve().is_file():
            candidate = local.get(source_id, {})
            path_value = candidate.get("path")
        if not isinstance(path_value, str):
            raise ValueError(
                f"{source_id}: local audio QC requires a downloaded local source path"
            )
        path = Path(path_value).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"{source_id}: local source is missing: {path}")
        primary_duration = source.get("duration_seconds")
        local_duration = candidate.get("duration_seconds")
        if not isinstance(primary_duration, (int, float)):
            raise ValueError(f"{source_id}: primary duration is unavailable")
        if isinstance(local_duration, (int, float)) and abs(
            float(local_duration) - float(primary_duration)
        ) > 0.1:
            raise ValueError(f"{source_id}: local source duration does not match")
        primary_url = source.get("url")
        local_url = (
            candidate.get("remote_url")
            or candidate.get("url_redacted")
            or candidate.get("url")
        )
        if isinstance(primary_url, str):
            if not isinstance(local_url, str) or canonical_url(
                primary_url
            ) != canonical_url(local_url):
                raise ValueError(
                    f"{source_id}: local source URL identity does not match"
                )
        records.append(
            {
                "id": source_id,
                "path": str(path),
                "duration_seconds": float(primary_duration),
            }
        )
    return records


def build_audio_plan(
    job_root: Path,
    *,
    plan_index_path: Path | None,
    local_source_manifest_path: Path | None,
    include_blocked: bool,
    output_path: Path,
) -> dict[str, Any]:
    plan_index_path = (
        plan_index_path.expanduser().resolve()
        if plan_index_path is not None
        else job_root / "story-plans" / "index.json"
    )
    source_manifest_path = job_root / "source_manifest.json"
    plan_index = load_json(plan_index_path)
    source_manifest = load_json(source_manifest_path)
    local_manifest = (
        load_json(local_source_manifest_path)
        if local_source_manifest_path is not None
        else None
    )
    outputs: list[dict[str, Any]] = []
    required_source_ids: set[str] = set()
    plan_fingerprints: list[dict[str, str]] = []
    for entry in sorted(
        plan_index.get("plans", []),
        key=lambda item: (item.get("production_slot", 0), item.get("story_id", "")),
    ):
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            continue
        if (
            entry.get("status") != "ready_for_video_qc"
            and not include_blocked
        ):
            continue
        plan_path = Path(entry["path"]).expanduser().resolve()
        plan = load_json(plan_path)
        clips = ordered_plan_clips(plan)
        segments: list[dict[str, Any]] = []
        for clip in clips:
            required_source_ids.add(clip["source_id"])
            segments.append(
                {
                    "id": clip["id"],
                    "source_id": clip["source_id"],
                    "source_start": clip["source_start"],
                    "source_end": clip["source_end"],
                    "role": clip["_block_role"],
                    "boundary_reason": "; ".join(clip.get("material_risks", [])),
                }
            )
        if segments:
            outputs.append(
                {
                    "id": plan["story_id"],
                    "repair_round": int(entry.get("repair_round", 0)),
                    "segments": segments,
                }
            )
            plan_fingerprints.append(
                {
                    "story_id": plan["story_id"],
                    "path": str(plan_path),
                    "sha256": sha256_file(plan_path),
                }
            )
    if not outputs:
        raise ValueError("no Story Plan clips are eligible for local audio QC")
    sources = local_source_map(
        source_manifest,
        local_manifest,
        required_source_ids,
    )
    value = {
        "version": "story-qc-audio-plan-1.0",
        "story_plan_index_path": str(plan_index_path),
        "story_plan_index_sha256": sha256_file(plan_index_path),
        "source_manifest_path": str(source_manifest_path),
        "source_manifest_sha256": sha256_file(source_manifest_path),
        "local_source_manifest_path": (
            str(local_source_manifest_path)
            if local_source_manifest_path is not None
            else None
        ),
        "local_source_manifest_sha256": (
            sha256_file(local_source_manifest_path)
            if local_source_manifest_path is not None
            else None
        ),
        "story_plans": plan_fingerprints,
        "sources": sources,
        "outputs": outputs,
    }
    atomic_write_json(output_path, value, private=True)
    return value


def source_identities(plan: dict[str, Any]) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for source in plan.get("sources", []):
        path = Path(source["path"]).expanduser().resolve()
        records.append(
            {
                "source_id": source["id"],
                "path": str(path),
                "sha256": sha256_file(path),
            }
        )
    return records


def plan_fingerprint(
    plan: dict[str, Any], identities: list[dict[str, str]]
) -> str:
    payload = {
        "version": plan.get("version"),
        "sources": [
            {
                "id": source.get("id"),
                "path": source.get("path"),
                "duration_seconds": source.get("duration_seconds"),
            }
            for source in plan.get("sources", [])
            if isinstance(source, dict)
        ],
        "outputs": [
            {
                "id": output.get("id"),
                "segments": [
                    {
                        "id": segment.get("id"),
                        "source_id": segment.get("source_id"),
                        "source_start": segment.get("source_start"),
                        "source_end": segment.get("source_end"),
                        "role": segment.get("role"),
                        "boundary_reason": segment.get("boundary_reason"),
                    }
                    for segment in output.get("segments", [])
                    if isinstance(segment, dict)
                ],
            }
            for output in plan.get("outputs", [])
            if isinstance(output, dict)
        ],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def expected_boundary_keys(
    plan: dict[str, Any],
) -> dict[tuple[str, str, str], float]:
    records: dict[tuple[str, str, str], float] = {}
    for output in plan.get("outputs", []):
        for segment in output.get("segments", []):
            for boundary in ("source_start", "source_end"):
                value = segment.get(boundary)
                if (
                    not isinstance(value, (int, float))
                    or isinstance(value, bool)
                    or not math.isfinite(value)
                ):
                    raise ValueError(
                        f"{output.get('id')}/{segment.get('id')}: invalid {boundary}"
                    )
                records[
                    (str(output.get("id")), str(segment.get("id")), boundary)
                ] = float(value)
    return records


def validate_audio_report(
    audio_plan_path: Path,
    report_path: Path,
    *,
    expected_policy: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    audio_plan = load_json(audio_plan_path)
    report = load_json(report_path)
    if report.get("version") != AUDIO_REPORT_VERSION:
        errors.append(
            f"audio boundary report version must be {AUDIO_REPORT_VERSION}"
        )
        return errors
    identities = source_identities(audio_plan)
    if (
        report.get("source_identities") is not None
        and report.get("source_identities") != identities
    ):
        errors.append("audio boundary source identities are stale")
    if report.get("plan_fingerprint") != plan_fingerprint(
        audio_plan, identities
    ):
        errors.append("audio boundary plan fingerprint is stale")
    if Path(str(report.get("plan_path", ""))).expanduser().resolve() != (
        audio_plan_path.expanduser().resolve()
    ):
        errors.append("audio boundary plan path does not match")
    if report.get("policy") != expected_policy:
        errors.append("audio boundary policy is stale")
    engines = report.get("engines")
    if not isinstance(engines, dict) or any(
        engines.get(name) != version
        for name, version in PINNED_AUDIO_ENGINES.items()
    ):
        errors.append("audio boundary engine versions are invalid")
    expected = expected_boundary_keys(audio_plan)
    actual: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in report.get("boundaries", []):
        if not isinstance(item, dict):
            errors.append("audio boundary report contains a non-object record")
            continue
        key = (
            str(item.get("output_id")),
            str(item.get("segment_id")),
            str(item.get("boundary")),
        )
        if key in actual:
            errors.append(f"duplicate audio boundary: {'/'.join(key)}")
        actual[key] = item
    if set(actual) != set(expected):
        errors.append(
            "audio boundary records do not cover Story clips exactly: "
            f"missing={sorted(set(expected) - set(actual))}, "
            f"extra={sorted(set(actual) - set(expected))}"
        )
    for key, cut in expected.items():
        item = actual.get(key)
        if item is None:
            continue
        planned = item.get("planned_source_seconds")
        if not isinstance(planned, (int, float)) or abs(
            float(planned) - cut
        ) > 0.001:
            errors.append(f"audio boundary timestamp mismatch: {'/'.join(key)}")
        if item.get("status") not in KNOWN_AUDIO_STATUSES:
            errors.append(f"unknown audio boundary status: {'/'.join(key)}")
    if report.get("source_errors"):
        errors.append("audio boundary report contains source analysis errors")
    return errors


def run_audio_guard(
    *,
    audio_python: Path,
    audio_guard_script: Path,
    audio_plan_path: Path,
    report_path: Path,
    cache_dir: Path,
    device: str,
    force: bool,
) -> int:
    if not audio_python.expanduser().resolve().is_file():
        raise FileNotFoundError(
            f"local audio Python is missing: {audio_python}"
        )
    if not audio_guard_script.expanduser().resolve().is_file():
        raise FileNotFoundError(
            f"audio boundary guard is missing: {audio_guard_script}"
        )
    command = [
        str(audio_python.expanduser().resolve()),
        str(audio_guard_script.expanduser().resolve()),
        "analyze",
        str(audio_plan_path),
        "--cache-dir",
        str(cache_dir),
        "--report",
        str(report_path),
        "--device",
        device,
    ]
    if force:
        command.append("--force")
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.stdout:
        print(completed.stdout.rstrip())
    if completed.returncode not in {0, 1}:
        detail = (completed.stderr or completed.stdout or "").strip()[-2000:]
        raise RuntimeError(
            "local audio boundary analysis failed"
            + (f": {detail}" if detail else "")
        )
    if not report_path.is_file():
        raise FileNotFoundError("local audio boundary report was not generated")
    return completed.returncode


def run_audio_guard_sharded(
    *,
    audio_python: Path,
    audio_guard_script: Path,
    audio_plan_path: Path,
    report_path: Path,
    cache_dir: Path,
    device: str,
    force: bool,
    workers: int,
) -> None:
    if workers <= 1:
        run_audio_guard(
            audio_python=audio_python,
            audio_guard_script=audio_guard_script,
            audio_plan_path=audio_plan_path,
            report_path=report_path,
            cache_dir=cache_dir,
            device=device,
            force=force,
        )
        return
    plan = load_json(audio_plan_path)
    sources = {
        item["id"]: item for item in plan.get("sources", [])
    }
    buckets: list[list[str]] = [[] for _ in range(workers)]
    loads = [0.0 for _ in range(workers)]
    for source in sorted(
        sources.values(),
        key=lambda item: float(item["duration_seconds"]),
        reverse=True,
    ):
        bucket_index = min(range(workers), key=lambda index: loads[index])
        buckets[bucket_index].append(source["id"])
        loads[bucket_index] += float(source["duration_seconds"])
    shard_dir = audio_plan_path.parent / "story-audio-shards"
    shard_jobs: list[tuple[Path, Path]] = []
    for index, source_ids in enumerate(buckets, start=1):
        if not source_ids:
            continue
        selected = set(source_ids)
        outputs = []
        for output in plan["outputs"]:
            segments = [
                item
                for item in output["segments"]
                if item["source_id"] in selected
            ]
            if segments:
                outputs.append(
                    {
                        "id": output["id"],
                        "repair_round": output.get("repair_round", 0),
                        "segments": segments,
                    }
                )
        shard_plan = {
            "version": plan["version"],
            "sources": [sources[source_id] for source_id in source_ids],
            "outputs": outputs,
        }
        shard_plan_path = shard_dir / f"shard-{index:02d}.plan.json"
        shard_report_path = shard_dir / f"shard-{index:02d}.report.json"
        atomic_write_json(shard_plan_path, shard_plan, private=True)
        shard_jobs.append((shard_plan_path, shard_report_path))
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                run_audio_guard,
                audio_python=audio_python,
                audio_guard_script=audio_guard_script,
                audio_plan_path=shard_plan_path,
                report_path=shard_report_path,
                cache_dir=cache_dir,
                device=device,
                force=force,
            ): (shard_plan_path, shard_report_path)
            for shard_plan_path, shard_report_path in shard_jobs
        }
        for future in as_completed(futures):
            shard_plan_path, _ = futures[future]
            try:
                future.result()
            except Exception as exc:  # noqa: BLE001 - subprocess boundary
                failures.append(f"{shard_plan_path.name}: {exc}")
    if failures:
        raise RuntimeError(
            "local audio boundary shards failed: " + "; ".join(failures)
        )
    policy = audio_policy(audio_guard_script)
    reports: list[dict[str, Any]] = []
    for shard_plan_path, shard_report_path in shard_jobs:
        errors = validate_audio_report(
            shard_plan_path,
            shard_report_path,
            expected_policy=policy,
        )
        if errors:
            raise ValueError(
                f"invalid audio shard {shard_report_path.name}: "
                + "; ".join(errors)
            )
        reports.append(load_json(shard_report_path))
    identities = source_identities(plan)
    boundary_by_key = {
        (
            item["output_id"],
            item["segment_id"],
            item["boundary"],
        ): item
        for report in reports
        for item in report["boundaries"]
    }
    expected = expected_boundary_keys(plan)
    boundaries = [boundary_by_key[key] for key in expected]
    source_errors = [
        item for report in reports for item in report.get("source_errors", [])
    ]
    blocking = [
        item for item in boundaries if item["status"] not in SAFE_AUDIO_STATUSES
    ]
    engines = reports[0]["engines"]
    merged = {
        "version": AUDIO_REPORT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "blocked" if blocking or source_errors else "approved",
        "plan_path": str(audio_plan_path),
        "plan_fingerprint": plan_fingerprint(plan, identities),
        "source_identities": identities,
        "policy": policy,
        "engines": engines,
        "source_analyses": [
            item
            for report in reports
            for item in report.get("source_analyses", [])
        ],
        "source_errors": source_errors,
        "boundaries": boundaries,
        "summary": {
            "expected_boundary_count": len(expected),
            "analyzed_boundary_count": len(boundaries),
            "safe_boundary_count": len(boundaries) - len(blocking),
            "blocking_boundary_count": len(blocking),
        },
        "final_media_uses_original_audio": True,
        "remote_audio_upload": False,
    }
    atomic_write_json(report_path, merged)


def prepare_and_run(
    job_root: Path,
    *,
    plan_index_path: Path | None,
    local_source_manifest_path: Path | None,
    audio_python: Path,
    audio_guard_script: Path,
    cache_dir: Path,
    report_path: Path,
    audio_plan_path: Path,
    device: str,
    force: bool,
    include_blocked: bool,
    workers: int,
) -> dict[str, Any]:
    build_audio_plan(
        job_root,
        plan_index_path=plan_index_path,
        local_source_manifest_path=local_source_manifest_path,
        include_blocked=include_blocked,
        output_path=audio_plan_path,
    )
    run_audio_guard_sharded(
        audio_python=audio_python,
        audio_guard_script=audio_guard_script,
        audio_plan_path=audio_plan_path,
        report_path=report_path,
        cache_dir=cache_dir,
        device=device,
        force=force,
        workers=workers,
    )
    policy = audio_policy(audio_guard_script)
    errors = validate_audio_report(
        audio_plan_path,
        report_path,
        expected_policy=policy,
    )
    if errors:
        raise ValueError("invalid local audio boundary report: " + "; ".join(errors))
    report = load_json(report_path)
    return {
        "method": "demucs-silero-dual-vad-v1.1",
        "status": report["status"],
        "story_plan_index_path": str(
            (
                plan_index_path
                if plan_index_path is not None
                else job_root / "story-plans" / "index.json"
            )
            .expanduser()
            .resolve()
        ),
        "audio_plan_path": str(audio_plan_path),
        "audio_plan_sha256": sha256_file(audio_plan_path),
        "report_path": str(report_path),
        "report_sha256": sha256_file(report_path),
        "audio_guard_script": str(audio_guard_script.expanduser().resolve()),
        "audio_guard_script_sha256": sha256_file(
            audio_guard_script.expanduser().resolve()
        ),
        "audio_python": str(audio_python.expanduser().resolve()),
        "cache_dir": str(cache_dir),
        "device": device,
        "workers": workers,
        "policy": policy,
        "engines": {
            key: report["engines"][key] for key in PINNED_AUDIO_ENGINES
        },
        "summary": report["summary"],
        "remote_audio_upload": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_root", type=Path)
    parser.add_argument("--story-plan-index", type=Path)
    parser.add_argument("--local-source-manifest", type=Path)
    parser.add_argument("--audio-python", type=Path)
    parser.add_argument("--audio-boundary-script", type=Path)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--audio-plan", type=Path)
    parser.add_argument("--device", choices=("cpu", "cuda", "mps"), default="cpu")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--include-blocked", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.workers <= 4:
        parser.error("--workers must be in 1..4")
    job_root = args.job_root.expanduser().resolve()
    audio_python = (
        args.audio_python.expanduser().resolve()
        if args.audio_python
        else job_root / ".venv-audio-boundary" / "bin" / "python"
    )
    audio_guard_script = (
        args.audio_boundary_script.expanduser().resolve()
        if args.audio_boundary_script
        else audio_guard_default()
    )
    metadata = prepare_and_run(
        job_root,
        plan_index_path=(
            args.story_plan_index.expanduser().resolve()
            if args.story_plan_index
            else None
        ),
        local_source_manifest_path=(
            args.local_source_manifest.expanduser().resolve()
            if args.local_source_manifest
            else None
        ),
        audio_python=audio_python,
        audio_guard_script=audio_guard_script,
        cache_dir=(
            args.cache_dir.expanduser().resolve()
            if args.cache_dir
            else job_root / ".audio-boundary-cache"
        ),
        report_path=(
            args.report.expanduser().resolve()
            if args.report
            else job_root / "story-qc-audio-boundary.json"
        ),
        audio_plan_path=(
            args.audio_plan.expanduser().resolve()
            if args.audio_plan
            else job_root / ".qc-cache" / "story-qc-audio-plan.json"
        ),
        device=args.device,
        force=args.force,
        include_blocked=args.include_blocked,
        workers=args.workers,
    )
    metadata_path = job_root / "story-qc-audio-boundary.metadata.json"
    atomic_write_json(metadata_path, metadata)
    print(f"STORY_AUDIO_QC\t{metadata['status']}\t{metadata['report_path']}")
    print(f"STORY_AUDIO_QC_METADATA\t{metadata_path}")
    return 0 if metadata["status"] == "approved" else 1


if __name__ == "__main__":
    raise SystemExit(main())
