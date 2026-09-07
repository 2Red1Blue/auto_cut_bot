"""Append-only attempt storage with atomic finalize and crash recovery.

Layout under the (private) artifacts root:

    <root>/<experiment_id>/spec.json
    <root>/<experiment_id>/recordings/<request_hash>.json
    <root>/<experiment_id>/attempt-NNNN/
        metadata.json          # ExperimentAttempt/v1
        calls/<call_id>/{request.json, raw_response, result.json}
        projection/projection.json
        metrics.json
        report.md
        .finalized.json        # success marker, written last

Every file is written tmp -> fsync -> rename. The success marker exists only
after raw, projection and metrics hashes are all in place; an attempt without
the marker is resumable. Attempts are append-only: existing evidence is never
overwritten.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path
from typing import Any

from tools.reuse.models import (
    TERMINAL_STATUSES,
    AttemptStatus,
    ErrorCode,
    ExperimentError,
    ProviderCallRequest,
    ProviderResult,
    json_sha256,
    load_json_strict,
)
from tools.reuse.models import (
    AttemptStatus as _AttemptStatus,
)

ATTEMPT_SCHEMA = "ExperimentAttempt/v1"
_ID_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def check_id(value: str, what: str) -> str:
    if not _ID_SAFE.match(value):
        raise ExperimentError(ErrorCode.PATH_ESCAPE, f"{what} is not a safe id: {value!r}")
    return value


def now_utc() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class ExperimentStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    # -- experiment level ---------------------------------------------------

    def experiment_dir(self, experiment_id: str) -> Path:
        check_id(experiment_id, "experiment_id")
        return self.root / experiment_id

    def recordings_dir(self, producer_id: str) -> Path:
        """Recordings are keyed by producer and exact request hash, shared across
        experiments: a live run records once, any later replay of the same
        request content hits it. They sit outside all attempt dirs."""
        return self.root / "recordings" / check_id(producer_id, "producer_id")

    def ensure_spec(self, experiment_dir: Path, spec_hash: str, spec_dict: dict[str, Any]) -> None:
        """First attempt writes spec.json; later attempts must hash-match it."""
        spec_path = experiment_dir / "spec.json"
        if spec_path.exists():
            existing = load_json_strict(spec_path.read_text(encoding="utf-8"), context="spec.json")
            if json_sha256(existing) != spec_hash:
                raise ExperimentError(
                    ErrorCode.SPEC_HASH_MISMATCH,
                    "resume/second attempt must reuse the identical frozen spec",
                )
        else:
            write_atomic(spec_path, json.dumps(spec_dict, sort_keys=True, indent=2, ensure_ascii=False))

    # -- attempts -----------------------------------------------------------

    def list_attempts(self, experiment_dir: Path) -> list[Path]:
        if not experiment_dir.is_dir():
            return []
        return sorted(p for p in experiment_dir.iterdir() if p.is_dir() and p.name.startswith("attempt-"))

    def load_metadata(self, attempt_dir: Path) -> dict[str, Any]:
        path = attempt_dir / "metadata.json"
        if not path.is_file():
            raise ExperimentError(ErrorCode.ATTEMPT_CONFLICT, f"missing metadata.json in {attempt_dir.name}")
        return load_json_strict(path.read_text(encoding="utf-8"), context="metadata.json")

    def reserve(
        self,
        experiment_dir: Path,
        spec_hash: str,
        base_metadata: dict[str, Any],
        *,
        resume: bool,
    ) -> tuple[Path, dict[str, Any]]:
        """Create the next attempt dir, or reuse the latest resumable one on --resume.

        Crash-safe ordering: metadata.json (status=reserved) is written before the
        calls/ dir, so an interrupted reserve leaves a readable resumable attempt
        rather than an unreadable half-directory. Orphan attempt dirs (metadata
        missing because of very old crashes) are skipped by scans, never fatal."""
        experiment_dir.mkdir(parents=True, exist_ok=True)
        if resume:
            for attempt_dir in reversed(self.list_attempts(experiment_dir)):
                if self.is_finalized(attempt_dir):
                    continue  # terminal evidence; never re-run it
                try:
                    meta = self.load_metadata(attempt_dir)
                except ExperimentError:
                    continue  # orphan dir without metadata; not resumable evidence
                if meta.get("spec_hash") != spec_hash:
                    raise ExperimentError(
                        ErrorCode.SPEC_HASH_MISMATCH,
                        f"attempt {attempt_dir.name} has a different spec hash",
                    )
                if AttemptStatus(meta.get("status")) not in TERMINAL_STATUSES:
                    return attempt_dir, meta
            raise ExperimentError(
                ErrorCode.ATTEMPT_CONFLICT,
                "--resume requested but no resumable (non-terminal) attempt found",
            )
        existing = self.list_attempts(experiment_dir)
        # A non-resumed run must not silently race another run of the same experiment.
        for attempt_dir in existing:
            try:
                meta = self.load_metadata(attempt_dir)
            except ExperimentError:
                continue  # orphan dir; tolerated, it just occupies its number
            if AttemptStatus(meta.get("status")) in (AttemptStatus.RESERVED, AttemptStatus.RUNNING):
                raise ExperimentError(
                    ErrorCode.ATTEMPT_CONFLICT,
                    f"attempt {attempt_dir.name} is still {meta.get('status')}; use --resume",
                )
        next_index = 1 + max(
            (-1, *(int(p.name.split("-")[1]) for p in existing if p.name.count("-") >= 1))
        )
        attempt_dir = experiment_dir / f"attempt-{next_index:04d}"
        if attempt_dir.exists():
            raise ExperimentError(ErrorCode.ATTEMPT_CONFLICT, f"attempt dir exists: {attempt_dir.name}")
        attempt_dir.mkdir(parents=True, exist_ok=False)
        metadata = {
            **base_metadata,
            "schema": ATTEMPT_SCHEMA,
            "attempt_id": attempt_dir.name,
            "spec_hash": spec_hash,
            "status": AttemptStatus.RESERVED.value,
            "error_code": None,
            "started_utc": now_utc(),
            "finished_utc": None,
        }
        write_atomic(attempt_dir / "metadata.json", json.dumps(metadata, sort_keys=True, indent=2, ensure_ascii=False))
        (attempt_dir / "calls").mkdir(exist_ok=False)
        return attempt_dir, metadata

    def mark(self, attempt_dir: Path, *, status: AttemptStatus, error_code: str | None = None,
             extra: dict[str, Any] | None = None) -> None:
        meta = self.load_metadata(attempt_dir)
        meta["status"] = status.value
        meta["error_code"] = error_code
        if status in TERMINAL_STATUSES or status == _AttemptStatus.RUNNING:
            pass
        if extra:
            meta.update(extra)
        if status in TERMINAL_STATUSES:
            meta["finished_utc"] = now_utc()
        write_atomic(attempt_dir / "metadata.json", json.dumps(meta, sort_keys=True, indent=2, ensure_ascii=False))

    # -- calls --------------------------------------------------------------

    def save_call_request(self, attempt_dir: Path, call: ProviderCallRequest) -> Path:
        """Idempotent: a crash between mkdir and write leaves a call dir without
        request.json; rewriting the deterministic request repairs it. A different
        request under the same call id is refused."""
        call_dir = attempt_dir / "calls" / check_id(call.call_id, "call_id")
        request_path = call_dir / "request.json"
        if request_path.exists():
            existing = ProviderCallRequest.from_dict(
                load_json_strict(request_path.read_text(encoding="utf-8"), context="request.json")
            )
            if existing.request_hash != call.request_hash:
                raise ExperimentError(
                    ErrorCode.ATTEMPT_CONFLICT,
                    f"call id {call.call_id} already bound to a different request",
                )
            return call_dir
        call_dir.mkdir(parents=True, exist_ok=True)
        write_atomic(request_path, json.dumps(call.to_dict(), sort_keys=True, indent=2, ensure_ascii=False))
        return call_dir

    def save_call_result(self, attempt_dir: Path, call: ProviderCallRequest, result: ProviderResult) -> None:
        call_dir = attempt_dir / "calls" / check_id(call.call_id, "call_id")
        raw_path = call_dir / "raw_response"
        if result.status == "done":
            write_atomic(raw_path, result.raw)
        write_atomic(
            call_dir / "result.json",
            json.dumps(
                {
                    "status": result.status,
                    "provider_response_id": result.provider_response_id,
                    "usage": {"input_tokens": result.usage.input_tokens, "output_tokens": result.usage.output_tokens},
                    "raw_sha256": (
                        __import__("hashlib").sha256(result.raw.encode("utf-8")).hexdigest()
                        if result.status == "done"
                        else None
                    ),
                },
                sort_keys=True, indent=2, ensure_ascii=False,
            ),
        )

    def load_completed_calls(self, attempt_dir: Path) -> dict[str, dict[str, Any]]:
        """Rebuild per-call state for adapter state reconstruction after a crash.

        Verifies each done call's recorded raw hash; tampered evidence is refused,
        never silently accepted into a rebuilt state."""
        import hashlib

        calls_dir = attempt_dir / "calls"
        if not calls_dir.is_dir():
            return {}
        state: dict[str, dict[str, Any]] = {}
        for call_dir in sorted(calls_dir.iterdir()):
            result_path = call_dir / "result.json"
            if not result_path.is_file():
                continue
            result = load_json_strict(result_path.read_text(encoding="utf-8"), context="result.json")
            request = load_json_strict(
                (call_dir / "request.json").read_text(encoding="utf-8"), context="request.json"
            )
            if result.get("status") == "done":
                raw = (call_dir / "raw_response").read_text(encoding="utf-8")
                raw_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
                if result.get("raw_sha256") != raw_hash:
                    raise ExperimentError(
                        ErrorCode.PROJECTION_REJECTED,
                        f"raw hash mismatch for call {call_dir.name}: evidence tampered or corrupt",
                    )
                state[call_dir.name] = {"request": request, "result": result, "raw": raw}
            else:
                state[call_dir.name] = {"request": request, "result": result}
        return state

    def load_requests(self, attempt_dir: Path) -> list[ProviderCallRequest]:
        calls_dir = attempt_dir / "calls"
        if not calls_dir.is_dir():
            return []
        out = []
        for call_dir in sorted(calls_dir.iterdir()):
            req_path = call_dir / "request.json"
            if req_path.is_file():
                out.append(
                    ProviderCallRequest.from_dict(
                        load_json_strict(req_path.read_text(encoding="utf-8"), context="request.json")
                    )
                )
        return out

    # -- artifacts & finalize ------------------------------------------------

    def write_projection(self, attempt_dir: Path, projection_dict: dict[str, Any]) -> str:
        text = json.dumps(projection_dict, sort_keys=True, indent=2, ensure_ascii=False)
        write_atomic(attempt_dir / "projection" / "projection.json", text)
        return json_sha256(projection_dict)

    def write_metrics(self, attempt_dir: Path, metrics_dict: dict[str, Any]) -> str:
        text = json.dumps(metrics_dict, sort_keys=True, indent=2, ensure_ascii=False)
        write_atomic(attempt_dir / "metrics.json", text)
        return json_sha256(metrics_dict)

    def write_report(self, attempt_dir: Path, report: str) -> str:
        write_atomic(attempt_dir / "report.md", report)
        import hashlib

        return hashlib.sha256(report.encode("utf-8")).hexdigest()

    def finalize(self, attempt_dir: Path, hashes: dict[str, str]) -> None:
        """Write the success marker only after every required artifact hash exists."""
        projection = attempt_dir / "projection" / "projection.json"
        metrics = attempt_dir / "metrics.json"
        report = attempt_dir / "report.md"
        for required in (projection, metrics, report):
            if not required.is_file():
                raise ExperimentError(
                    ErrorCode.PROJECTION_REJECTED, f"finalize refused: missing {required.name}"
                )
        calls = self.load_completed_calls(attempt_dir)
        if not calls:
            raise ExperimentError(
                ErrorCode.PROJECTION_REJECTED, "finalize refused: no completed provider calls"
            )
        for call_id, entry in calls.items():
            if entry["result"].get("status") != "done":
                raise ExperimentError(
                    ErrorCode.PROVIDER_RESULT_UNKNOWN,
                    f"finalize refused: call {call_id} has no terminal result",
                )
        raw_hashes = {
            call_id: entry["result"].get("raw_sha256") for call_id, entry in sorted(calls.items())
        }
        if any(v is None for v in raw_hashes.values()):
            raise ExperimentError(ErrorCode.PROJECTION_REJECTED, "finalize refused: raw hash missing")
        marker = {
            "finalized": True,
            "raw_hashes": raw_hashes,
            "projection_sha256": hashes.get("projection"),
            "metrics_sha256": hashes.get("metrics"),
            "report_sha256": hashes.get("report"),
            "finalized_utc": now_utc(),
        }
        write_atomic(attempt_dir / ".finalized.json", json.dumps(marker, sort_keys=True, indent=2, ensure_ascii=False))
        self.mark(attempt_dir, status=AttemptStatus.SUCCEEDED)

    def is_finalized(self, attempt_dir: Path) -> bool:
        return (attempt_dir / ".finalized.json").is_file()


class RunLock:
    """Cross-process advisory lock over one experiment directory.

    Guarantees that spec.json freeze checks, attempt numbering and recordings
    are mutated by at most one runner process at a time. No-op on platforms
    without fcntl (the supported runtime is Linux/WSL)."""

    def __init__(self, experiment_dir: Path) -> None:
        self._path = experiment_dir / ".lock"
        self._fd: int | None = None

    def __enter__(self) -> "RunLock":
        try:
            import fcntl
        except ImportError:  # pragma: no cover - non-POSIX
            return self
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(self._path, os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(self._fd)
            self._fd = None
            raise ExperimentError(
                ErrorCode.ATTEMPT_CONFLICT,
                f"another runner process holds the lock for this experiment: {exc}",
            ) from exc
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._fd is not None:
            try:
                import fcntl

                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None
