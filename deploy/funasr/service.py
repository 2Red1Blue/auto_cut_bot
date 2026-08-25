"""Standalone SenseVoiceSmall + FSMN-VAD timed evidence service."""

from __future__ import annotations

import asyncio
import base64
import copy
import errno
import fcntl
import hashlib
import hmac
import importlib.metadata
import json
import os
import platform
import re
import stat
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, cast

import torch
from aiohttp import web
from funasr import AutoModel

PROVIDER_ID = "funasr-http-v1"
PROVIDER_VERSION = "1.0.0"
ASR_MODEL_ID = "SenseVoiceSmall"
VAD_MODEL_ID = "fsmn-vad"
ASR_INFERENCE_KIND = "sensevoice-word-timestamp"
VAD_INFERENCE_KIND = "fsmn-vad-direct"
SENSEVOICE_WORD_GUARD_PROFILE = "sensevoice_word_guard_v1"
RESOURCE_PRESSURE_TEXT = "resource-pressure"
CANONICAL_SINGLETON_LOCK_PATH = Path("/tmp").resolve(strict=True) / "autocut-funasr-service.lock"


@dataclass(frozen=True)
class ResourceSnapshot:
    available_bytes: int
    swap_total_bytes: int
    swap_used_bytes: int

    def __post_init__(self) -> None:
        if (
            self.available_bytes < 0
            or self.swap_total_bytes < 0
            or self.swap_used_bytes < 0
            or self.swap_used_bytes > self.swap_total_bytes
        ):
            raise RuntimeError("resource snapshot contains invalid byte counts")


ResourceReader = Callable[[], ResourceSnapshot]


def linux_resource_snapshot(meminfo: str) -> ResourceSnapshot:
    values: dict[str, int] = {}
    for line in meminfo.splitlines():
        match = re.fullmatch(r"(MemAvailable|SwapTotal|SwapFree):\s+([0-9]+)\s+kB", line)
        if match is not None:
            values[match.group(1)] = int(match.group(2)) * 1024
    if set(values) != {"MemAvailable", "SwapTotal", "SwapFree"}:
        raise RuntimeError("Linux resource snapshot is incomplete")
    if values["SwapFree"] > values["SwapTotal"]:
        raise RuntimeError("Linux swap counters are invalid")
    return ResourceSnapshot(
        values["MemAvailable"],
        values["SwapTotal"],
        values["SwapTotal"] - values["SwapFree"],
    )


def _scaled_bytes(value: str, unit: str) -> int:
    scales = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
    return int(Decimal(value) * scales[unit.upper()])


def macos_resource_snapshot(vm_stat: str, swapusage: str) -> ResourceSnapshot:
    page_match = re.search(r"page size of ([0-9]+) bytes", vm_stat)
    if page_match is None:
        raise RuntimeError("macOS page size is unavailable")
    pages: dict[str, int] = {}
    for line in vm_stat.splitlines():
        match = re.fullmatch(r"Pages (free|inactive|speculative):\s+([0-9]+)\.", line)
        if match is not None:
            pages[match.group(1)] = int(match.group(2))
    if set(pages) != {"free", "inactive", "speculative"}:
        raise RuntimeError("macOS available-page snapshot is incomplete")
    swap: dict[str, int] = {}
    for match in re.finditer(r"\b(total|used)\s*=\s*([0-9]+(?:\.[0-9]+)?)([KMGT])\b", swapusage):
        swap[match.group(1)] = _scaled_bytes(match.group(2), match.group(3))
    if set(swap) != {"total", "used"}:
        raise RuntimeError("macOS swap snapshot is incomplete")
    return ResourceSnapshot(
        sum(pages.values()) * int(page_match.group(1)),
        swap["total"],
        swap["used"],
    )


def system_resource_snapshot() -> ResourceSnapshot:
    sys_platform = platform.system()
    if sys_platform == "Linux":
        return linux_resource_snapshot(Path("/proc/meminfo").read_text(encoding="ascii"))
    if sys_platform == "Darwin":
        vm_stat = subprocess.run(
            ["/usr/bin/vm_stat"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
        swapusage = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "vm.swapusage"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
        return macos_resource_snapshot(vm_stat, swapusage)
    raise RuntimeError("resource snapshots support only macOS and Linux")


_PROCESS_LOCK_PATHS: set[Path] = set()
_PROCESS_LOCK_GUARD = threading.Lock()


def canonical_singleton_lock_path(raw_path: str) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        raise RuntimeError("FUNASR_SINGLETON_LOCK_PATH must be absolute")
    resolved = path.parent.resolve(strict=True) / path.name
    if resolved != CANONICAL_SINGLETON_LOCK_PATH:
        raise RuntimeError(
            "FUNASR_SINGLETON_LOCK_PATH must resolve to the canonical singleton lock path"
        )
    return CANONICAL_SINGLETON_LOCK_PATH


class HostSingletonLock:
    def __init__(self, raw_path: str | Path) -> None:
        path = Path(raw_path)
        if not path.is_absolute():
            raise RuntimeError("FUNASR_SINGLETON_LOCK_PATH must be absolute")
        self.path = path.parent.resolve(strict=True) / path.name
        self.fd: int | None = None

    def acquire(self) -> None:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        with _PROCESS_LOCK_GUARD:
            if self.path in _PROCESS_LOCK_PATHS:
                raise RuntimeError("FunASR singleton lock is already held")
            if self.path.is_symlink():
                raise RuntimeError("FunASR singleton lock must not be a symbolic link")
            try:
                fd = os.open(self.path, flags, 0o600)
            except OSError as error:
                if error.errno == errno.ELOOP:
                    raise RuntimeError(
                        "FunASR singleton lock must not be a symbolic link"
                    ) from error
                raise
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise RuntimeError("FunASR singleton lock must be a regular file")
                os.fchmod(fd, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                os.ftruncate(fd, 0)
                os.write(fd, f"{os.getpid()}\n".encode("ascii"))
            except BlockingIOError as error:
                os.close(fd)
                raise RuntimeError("FunASR singleton lock is already held") from error
            except Exception:
                os.close(fd)
                raise
            _PROCESS_LOCK_PATHS.add(self.path)
            self.fd = fd

    def release(self) -> None:
        with _PROCESS_LOCK_GUARD:
            if self.fd is None:
                return
            fd = self.fd
            self.fd = None
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
                _PROCESS_LOCK_PATHS.discard(self.path)


def canon(v: object) -> bytes:
    return json.dumps(v, sort_keys=True, separators=(",", ":")).encode()


def sha(v: bytes) -> str:
    return "sha256:" + hashlib.sha256(v).hexdigest()


def tick(ms: int, tb: dict[str, int], end: bool) -> int:
    v = Fraction(ms, 1000) * tb["denominator"] / tb["numerator"]
    return -((-v.numerator) // v.denominator) if end else v.numerator // v.denominator


def tree_hash(path: Path) -> str:
    records = []
    for p in sorted(path.rglob("*")):
        if p.is_symlink():
            raise RuntimeError("model symlink forbidden")
        if p.is_file():
            digest = hashlib.sha256()
            size = 0
            with p.open("rb") as source:
                while block := source.read(1024 * 1024):
                    digest.update(block)
                    size += len(block)
            records.append(
                {
                    "path": p.relative_to(path).as_posix(),
                    "size": size,
                    "sha256": "sha256:" + digest.hexdigest(),
                }
            )
    return sha(canon(records))


def positive_environment(name: str) -> int:
    raw = os.environ[name]
    if not raw.isascii() or not raw.isdecimal() or int(raw) <= 0:
        raise RuntimeError(f"{name} must be a positive decimal integer")
    return int(raw)


def nonnegative_environment(name: str) -> int:
    raw = os.environ[name]
    if not raw.isascii() or not raw.isdecimal():
        raise RuntimeError(f"{name} must be a non-negative decimal integer")
    return int(raw)


def service_hash() -> str:
    with Path(__file__).open("rb") as source:
        digest = hashlib.sha256()
        while block := source.read(1024 * 1024):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def module_device(module: object) -> str:
    devices: set[str] = set()
    for method_name in ("parameters", "buffers"):
        method = getattr(module, method_name, None)
        if callable(method):
            for value in method():
                device = getattr(value, "device", None)
                if device is not None:
                    devices.add(str(getattr(device, "type", device)))
    if not devices:
        device = getattr(module, "device", None)
        if device is not None:
            devices.add(str(getattr(device, "type", device)))
    if len(devices) != 1:
        raise RuntimeError("model parameters do not have one measurable device")
    return devices.pop()


def detector_hash(identity: dict[str, object], profile: dict[str, object]) -> str:
    return sha(
        canon(
            {
                "device": profile["device"],
                "funasr_version": profile["funasr_version"],
                "model_id": identity["model_id"],
                "model_revision": identity["model_revision"],
                "model_sha256": identity["model_sha256"],
                "producer_kind": identity["producer_kind"],
                "provider_id": profile["provider_id"],
                "provider_version": profile["provider_version"],
                "service_sha256": profile["service_sha256"],
                "timed_speech_policy_sha256": profile["timed_speech_policy_sha256"],
                "torch_version": profile["torch_version"],
                "word_timing_capability": profile["word_timing_capability"],
            }
        )
    )


def coverage(m: dict[str, object], outcome: str) -> dict[str, object]:
    s = cast(dict[str, object], m["source"])
    c = cast(dict[str, object], m["audio_clock"])
    r = cast(dict[str, object], m["requested_range"])
    return {
        "source_id": s["source_id"],
        "source_sha256": s["source_sha256"],
        "clock_id": c["clock_id"],
        "time_base": c["time_base"],
        "in_tick": r["in_tick"],
        "out_tick": r["out_tick"],
        "outcome": outcome,
    }


def empty_transcript(required: bool, indeterminate: bool = False) -> dict[str, object]:
    return {
        "coverage_outcome": "partial" if indeterminate else "complete",
        "lexical_outcome": "indeterminate" if indeterminate else "no_lexical_content",
        "completeness": {
            "segment": "partial" if indeterminate else "complete",
            "word": "partial"
            if indeterminate and required
            else "complete"
            if required
            else "not_applicable",
            "sentence": "partial" if indeterminate else "not_applicable",
        },
        "segments": [],
        "words": [],
        "sentences": [],
        "boundary_touch": {"left": False, "right": False},
        "truncated": False,
    }


def transcript(
    raw: object, tb: dict[str, int], rr: dict[str, int], required: bool, gap: int
) -> dict[str, object]:
    bad = empty_transcript(required, True)
    if type(raw) is not list or len(raw) != 1 or type(raw[0]) is not dict:
        return bad
    x = raw[0]
    text = x.get("text")
    ts = x.get("timestamp")
    words = x.get("words")
    if required:
        if (
            type(text) is str
            and not text.strip()
            and type(ts) is list
            and not ts
            and (words is None or words == [])
        ):
            return empty_transcript(True)
        if (
            type(text) is not str
            or type(words) is not list
            or type(ts) is not list
            or not words
            or len(words) != len(ts)
        ):
            return bad
        out = []
        ms = []
        try:
            for n, (w, pair) in enumerate(zip(words, ts, strict=True)):
                a, b = pair
                if (
                    type(w) is not str
                    or not w.strip()
                    or type(a) is not int
                    or type(b) is not int
                    or a < 0
                    or a >= b
                    or (ms and ms[-1][1] > a)
                ):
                    raise ValueError
                i = rr["in_tick"] + tick(a, tb, False)
                o = rr["in_tick"] + tick(b, tb, True)
                if i < rr["in_tick"] or o > rr["out_tick"]:
                    raise ValueError
                out.append(
                    {"word_id": f"word-{n:08d}", "in_tick": i, "out_tick": o, "text": w.strip()}
                )
                ms.append((a, b))
        except Exception:
            return bad
        ranges = []
        start = 0
        for n in range(1, len(out)):
            if ms[n][0] - ms[n - 1][1] > gap:
                ranges.append((start, n))
                start = n
        ranges.append((start, len(out)))
        seg = []
        for n, (a, b) in enumerate(ranges):
            selected = out[a:b]
            txt = "".join(z["text"] for z in selected)
            seg.append(
                {
                    "segment_id": f"segment-{n:08d}",
                    "in_tick": selected[0]["in_tick"],
                    "out_tick": selected[-1]["out_tick"],
                    "sentence_ids": [],
                    "text": txt,
                }
            )
        return {
            "coverage_outcome": "complete",
            "lexical_outcome": "transcript_available",
            "completeness": {
                "segment": "complete",
                "word": "complete",
                "sentence": "not_applicable",
            },
            "segments": seg,
            "words": out,
            "sentences": [],
            "boundary_touch": {
                "left": out[0]["in_tick"] <= rr["in_tick"],
                "right": out[-1]["out_tick"] >= rr["out_tick"],
            },
            "truncated": False,
        }
    return bad


def vad(
    raw: object, tb: dict[str, int], rr: dict[str, int], gap: int
) -> tuple[str, list[dict[str, object]]]:
    if (
        type(raw) is not list
        or len(raw) != 1
        or type(raw[0]) is not dict
        or type(raw[0].get("value")) is not list
    ):
        return "indeterminate", []
    values = raw[0]["value"]
    if not values:
        return "no_speech", []
    merged = []
    try:
        for a, b in values:
            if (
                type(a) is not int
                or type(b) is not int
                or a < 0
                or a >= b
                or (merged and merged[-1][0] > a)
            ):
                raise ValueError
            if merged and a - merged[-1][1] <= gap:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        result = []
        for n, (a, b) in enumerate(merged):
            i = rr["in_tick"] + tick(a, tb, False)
            o = rr["in_tick"] + tick(b, tb, True)
            if i < rr["in_tick"] or o > rr["out_tick"]:
                raise ValueError
            result.append(
                {
                    "speech_segment_id": f"speech-{n:08d}",
                    "in_tick": i,
                    "out_tick": o,
                    "confidence_ppm": None,
                }
            )
        return "speech", result
    except Exception:
        return "indeterminate", []


class Service:
    def __init__(self, resource_reader: ResourceReader = system_resource_snapshot) -> None:
        profile = json.loads(os.environ["FUNASR_PROFILE_JSON"])
        if type(profile) is not dict:
            raise RuntimeError("FUNASR_PROFILE_JSON must be an object")
        self.profile = cast(dict[str, object], profile)
        self.lock = asyncio.Lock()
        self.admission_lock = asyncio.Lock()
        self.admitted = 0
        self.ready = False
        self.max_request = positive_environment("FUNASR_MAX_REQUEST_BYTES")
        self.max_response = positive_environment("FUNASR_MAX_RESPONSE_BYTES")
        self.timeout = positive_environment("FUNASR_INFERENCE_TIMEOUT_SECONDS")
        self.queue_capacity = positive_environment("FUNASR_QUEUE_CAPACITY")
        if self.queue_capacity != 3:
            raise RuntimeError("FUNASR_QUEUE_CAPACITY must be 3")
        required_python = os.environ["FUNASR_REQUIRED_PYTHON_VERSION"]
        if required_python != platform.python_version():
            raise RuntimeError("FUNASR_REQUIRED_PYTHON_VERSION does not match the runtime")
        self.startup_min_available = positive_environment("FUNASR_STARTUP_MIN_AVAILABLE_BYTES")
        self.inference_min_available = positive_environment("FUNASR_INFERENCE_MIN_AVAILABLE_BYTES")
        self.max_swap_used = nonnegative_environment("FUNASR_MAX_SWAP_USED_BYTES")
        self.resource_reader = resource_reader
        lock_path = canonical_singleton_lock_path(os.environ["FUNASR_SINGLETON_LOCK_PATH"])
        self.singleton = HostSingletonLock(lock_path)
        token = os.environ["FUNASR_SHARED_TOKEN"]
        if not token:
            raise RuntimeError("FUNASR_SHARED_TOKEN must be non-empty")
        self.shared_token = token
        self.model: Any = None
        self.measured: str | None = None
        self.measured_profile: dict[str, object] | None = None
        self.identities: list[dict[str, object]] = []
        self._fatal_exit = os._exit

    async def require_resources(self, minimum_available: int) -> ResourceSnapshot:
        try:
            snapshot = await asyncio.to_thread(self.resource_reader)
        except Exception as error:
            raise RuntimeError(f"{RESOURCE_PRESSURE_TEXT}: snapshot unavailable") from error
        if (
            snapshot.available_bytes < minimum_available
            or snapshot.swap_used_bytes > self.max_swap_used
        ):
            raise RuntimeError(
                f"{RESOURCE_PRESSURE_TEXT}: available={snapshot.available_bytes} "
                f"required={minimum_available} swap_used={snapshot.swap_used_bytes} "
                f"max_swap_used={self.max_swap_used}"
            )
        return snapshot

    async def load(self) -> None:
        a = Path(os.environ["FUNASR_ASR_MODEL_PATH"]).resolve(strict=True)
        v = Path(os.environ["FUNASR_VAD_MODEL_PATH"]).resolve(strict=True)
        if not a.is_dir() or not v.is_dir():
            raise RuntimeError("model paths must be directories")
        expected_fields = {
            "schema_version",
            "provider_id",
            "provider_version",
            "service_sha256",
            "funasr_version",
            "torch_version",
            "device",
            "word_timing_capability",
            "max_request_bytes",
            "profile_calibration_sha256",
            "timed_speech_policy_sha256",
            "utterance_gap_milliseconds",
            "vad_merge_gap_milliseconds",
            "producers",
        }
        if set(self.profile) != expected_fields:
            raise RuntimeError("profile schema is not closed")
        if self.profile["max_request_bytes"] != self.max_request:
            raise RuntimeError("FUNASR_MAX_REQUEST_BYTES does not match the measured profile")
        if self.profile["word_timing_capability"] != "required":
            raise RuntimeError("sensevoice_word_guard_v1 requires real word timestamps")
        measured = {
            **{k: self.profile[k] for k in expected_fields - {"producers"}},
            "provider_id": PROVIDER_ID,
            "provider_version": PROVIDER_VERSION,
            "service_sha256": service_hash(),
            "funasr_version": importlib.metadata.version("funasr"),
            "torch_version": torch.__version__,
        }
        producers = self.profile["producers"]
        if type(producers) is not list or len(producers) != 2:
            raise RuntimeError("profile must contain ASR and VAD producers")
        declared = cast(list[dict[str, object]], producers)
        measured_models = (
            (
                "asr",
                ASR_MODEL_ID,
                a.name,
                await asyncio.to_thread(tree_hash, a),
                ASR_INFERENCE_KIND,
            ),
            (
                "vad",
                VAD_MODEL_ID,
                v.name,
                await asyncio.to_thread(tree_hash, v),
                VAD_INFERENCE_KIND,
            ),
        )
        identity_fields = {
            "producer_kind",
            "producer_id",
            "producer_version",
            "generation_policy_sha256",
            "detector_sha256",
            "calibration_policy_sha256",
            "calibration_record_sha256",
            "timing_error_bound_tick",
            "model_id",
            "model_revision",
            "model_sha256",
            "service_sha256",
            "inference_kind",
        }
        identities = []
        for raw, (kind, model_id, revision, model_sha256, inference_kind) in zip(
            declared, measured_models, strict=True
        ):
            if type(raw) is not dict or set(raw) != identity_fields:
                raise RuntimeError("producer profile schema is not closed")
            actual = {
                **raw,
                "producer_kind": kind,
                "model_id": model_id,
                "model_revision": revision,
                "model_sha256": model_sha256,
                "service_sha256": measured["service_sha256"],
                "inference_kind": inference_kind,
            }
            actual["detector_sha256"] = detector_hash(actual, measured)
            if actual != raw:
                raise RuntimeError(f"measured {kind} identity mismatch")
            identities.append(actual)
        for key, actual in measured.items():
            if key != "producers" and actual != self.profile[key]:
                raise RuntimeError(f"measured profile identity mismatch {key}")
        await self.require_resources(self.startup_min_available)
        self.singleton.acquire()
        model_task = asyncio.create_task(
            asyncio.to_thread(
                AutoModel,
                model=str(a),
                vad_model=str(v),
                device=self.profile["device"],
                disable_update=True,
                disable_pbar=True,
            )
        )
        try:
            try:
                self.model = await asyncio.shield(model_task)
            except asyncio.CancelledError:
                await asyncio.shield(model_task)
                raise
            devices = (module_device(self.model.model), module_device(self.model.vad_model))
            if devices != (self.profile["device"], self.profile["device"]):
                raise RuntimeError("model parameter device mismatch")
        except BaseException:
            self.model = None
            self.singleton.release()
            raise
        measured["producers"] = identities
        self.measured_profile = measured
        self.identities = identities
        self.measured = sha(canon(measured))
        self.ready = True

    def infer(self, p: Path) -> tuple[object, object]:
        return self.model.generate(input=str(p), output_timestamp=True), self.model.inference(
            str(p), model=self.model.vad_model, kwargs=copy.deepcopy(self.model.vad_kwargs)
        )

    async def admit(self) -> None:
        async with self.admission_lock:
            try:
                await self.require_resources(self.inference_min_available)
            except RuntimeError as error:
                raise web.HTTPServiceUnavailable(
                    text=RESOURCE_PRESSURE_TEXT, headers={"Retry-After": "1"}
                ) from error
            if self.admitted >= self.queue_capacity:
                raise web.HTTPServiceUnavailable(text="inference queue full")
            self.admitted += 1

    async def release(self) -> None:
        async with self.admission_lock:
            self.admitted -= 1

    async def close(self) -> None:
        self.ready = False
        async with self.lock:
            self.model = None
            self.singleton.release()

    def fatal(self, code: int) -> None:
        self.ready = False
        self._fatal_exit(code)
        raise RuntimeError("fatal exit returned")

    async def run_inference(self, path: Path) -> tuple[object, object]:
        async with self.lock:
            task = asyncio.create_task(asyncio.to_thread(self.infer, path))
            try:
                return await asyncio.wait_for(asyncio.shield(task), self.timeout)
            except asyncio.CancelledError:
                try:
                    await asyncio.shield(task)
                except Exception:
                    self.fatal(71)
                raise
            except TimeoutError:
                self.fatal(70)
            except Exception:
                self.fatal(71)

    def validate_manifest_identity(self, manifest: dict[str, object]) -> None:
        if self.measured_profile is None:
            raise web.HTTPServiceUnavailable()
        profile = cast(dict[str, object], manifest.get("profile"))
        expected_profile = {
            key: self.measured_profile[key]
            for key in (
                "provider_id",
                "provider_version",
                "funasr_version",
                "torch_version",
                "device",
                "word_timing_capability",
                "max_request_bytes",
                "profile_calibration_sha256",
            )
        }
        if profile != expected_profile:
            raise web.HTTPConflict(text="measured profile identity drift")
        limits = cast(dict[str, int], manifest["source_byte_limits"])
        if (
            limits["service_max_request_bytes"] != self.measured_profile["max_request_bytes"]
            or limits["effective_max_source_bytes"]
            != min(limits["kernel_max_source_bytes"], limits["service_max_request_bytes"])
        ):
            raise web.HTTPConflict(text="measured source-byte policy drift")
        if manifest.get("timed_speech_policy_sha256") != self.measured_profile.get(
            "timed_speech_policy_sha256"
        ) or manifest.get("timing_policy") != {
            "utterance_gap_milliseconds": self.measured_profile.get("utterance_gap_milliseconds"),
            "vad_merge_gap_milliseconds": self.measured_profile.get("vad_merge_gap_milliseconds"),
        }:
            raise web.HTTPConflict(text="measured timing policy drift")
        if self.measured_profile["word_timing_capability"] != "required":
            raise web.HTTPConflict(text="sensevoice word timing capability drift")
        if manifest.get("transcript_capability") != {
            "profile": SENSEVOICE_WORD_GUARD_PROFILE,
            "segment": "complete",
            "segment_semantics": "utterance_gap_protected_range",
            "sentence": "not_applicable",
            "word": "complete",
            "word_timing": "required",
        }:
            raise web.HTTPConflict(text="measured transcript capability drift")
        expected = manifest.get("expected_producers")
        if expected != [
            {**identity, "timing_error_bound_tick": identity["timing_error_bound_tick"]}
            for identity in self.identities
        ]:
            raise web.HTTPConflict(text="measured producer identity drift")

    @staticmethod
    def validate_manifest_schema(manifest: object) -> dict[str, object]:
        fields = {
            "schema_version",
            "source",
            "source_byte_limits",
            "container",
            "audio_clock",
            "requested_range",
            "profile",
            "expected_producers",
            "timed_speech_policy_sha256",
            "response_limits",
            "timing_policy",
            "transcript_capability",
        }
        if type(manifest) is not dict or set(manifest) != fields:
            raise web.HTTPBadRequest(text="manifest schema is not closed")
        value = cast(dict[str, object], manifest)
        source = value["source"]
        source_byte_limits = value["source_byte_limits"]
        container = value["container"]
        clock = value["audio_clock"]
        requested = value["requested_range"]
        response_limits = value["response_limits"]
        if (
            type(source) is not dict
            or set(source) != {"source_id", "source_sha256"}
            or type(source_byte_limits) is not dict
            or set(source_byte_limits)
            != {
                "kernel_max_source_bytes",
                "service_max_request_bytes",
                "effective_max_source_bytes",
            }
            or type(container) is not dict
            or container != {"media_type": "video/mp4", "safe_suffix": ".mp4"}
            or type(clock) is not dict
            or set(clock) != {"clock_id", "time_base", "origin_tick", "duration_tick"}
            or type(requested) is not dict
            or set(requested) != {"in_tick", "out_tick"}
            or type(response_limits) is not dict
            or set(response_limits) != {"max_response_bytes"}
        ):
            raise web.HTTPBadRequest(text="manifest member schema is not closed")
        time_base = clock["time_base"]
        integer_values = (
            clock["origin_tick"],
            clock["duration_tick"],
            requested["in_tick"],
            requested["out_tick"],
            response_limits["max_response_bytes"],
            *cast(dict[str, object], source_byte_limits).values(),
        )
        if (
            type(time_base) is not dict
            or set(time_base) != {"numerator", "denominator"}
            or any(type(item) is not int for item in (*integer_values, *time_base.values()))
            or clock["duration_tick"] <= 0
            or response_limits["max_response_bytes"] <= 0
            or any(item <= 0 for item in cast(dict[str, int], source_byte_limits).values())
        ):
            raise web.HTTPBadRequest(text="manifest clock/bounds are invalid")
        return value

    async def evidence(self, req: web.Request) -> web.Response:
        if not self.ready:
            raise web.HTTPServiceUnavailable()
        supplied = req.headers.get("Authorization", "")
        if not hmac.compare_digest(supplied, f"Bearer {self.shared_token}"):
            raise web.HTTPUnauthorized(text="unauthorized")
        try:
            decoded = json.loads(
                base64.b64decode(req.headers["X-Timed-Speech-Manifest"], validate=True)
            )
            m = self.validate_manifest_schema(decoded)
        except Exception as e:
            if isinstance(e, web.HTTPException):
                raise
            raise web.HTTPBadRequest(text="bad manifest") from e
        if sha(canon(m)) != req.headers.get("X-Timed-Speech-Request-SHA256"):
            raise web.HTTPBadRequest(text="identity")
        self.validate_manifest_identity(m)
        rr = m["requested_range"]
        c = m["audio_clock"]
        if rr != {"in_tick": c["origin_tick"], "out_tick": c["origin_tick"] + c["duration_tick"]}:
            raise web.HTTPBadRequest(text="full source only")
        await self.admit()
        try:
            with tempfile.TemporaryDirectory(prefix="funasr-request-") as d:
                p = Path(d) / "source.mp4"
                h = hashlib.sha256()
                size = 0
                with p.open("xb") as f:
                    async for chunk in req.content.iter_chunked(1 << 20):
                        size += len(chunk)
                        if size > m["source_byte_limits"]["effective_max_source_bytes"]:
                            raise web.HTTPRequestEntityTooLarge(
                                max_size=m["source_byte_limits"]["effective_max_source_bytes"],
                                actual_size=size,
                            )
                        h.update(chunk)
                        f.write(chunk)
                if "sha256:" + h.hexdigest() != m["source"]["source_sha256"]:
                    raise web.HTTPBadRequest(text="source hash")
                a, v = await self.run_inference(p)
            state, vsegs = vad(
                v, c["time_base"], rr, m["timing_policy"]["vad_merge_gap_milliseconds"]
            )
            t = transcript(
                a,
                c["time_base"],
                rr,
                m["profile"]["word_timing_capability"] == "required",
                m["timing_policy"]["utterance_gap_milliseconds"],
            )
            t["coverage"] = coverage(m, t.pop("coverage_outcome"))
            speech = {
                "coverage": coverage(m, "partial" if state == "indeterminate" else "complete"),
                "speech_outcome": {
                    "speech": "speech_detected",
                    "no_speech": "none_detected",
                    "indeterminate": "indeterminate",
                }[state],
                "segments": vsegs,
            }
            identities = [
                {
                    **{k: value for k, value in identity.items() if k != "timing_error_bound_tick"},
                    **{
                        k: self.measured_profile[k]
                        for k in (
                            "provider_id",
                            "provider_version",
                            "funasr_version",
                            "torch_version",
                            "device",
                        )
                    },
                }
                for identity in self.identities
            ]
            bounds = {
                identity["producer_kind"]: {
                    "early_tick": identity["timing_error_bound_tick"],
                    "late_tick": identity["timing_error_bound_tick"],
                    "time_base": c["time_base"],
                }
                for identity in self.identities
            }
            response = {
                "schema_version": "timed-speech-evidence-response-v1",
                "request_identity_sha256": sha(canon(m)),
                "source": m["source"],
                "source_byte_limits": m["source_byte_limits"],
                "container": m["container"],
                "audio_clock": c,
                "requested_range": rr,
                "timed_speech_policy_sha256": m["timed_speech_policy_sha256"],
                "transcript_capability": m["transcript_capability"],
                "producer_identities": identities,
                "timing_error_bounds": bounds,
                "transcript": t,
                "speech_activity": speech,
            }
            raw = canon(response)
            if len(raw) > min(self.max_response, m["response_limits"]["max_response_bytes"]):
                raise web.HTTPInternalServerError(text="response bound")
            return web.Response(body=raw, content_type="application/json")
        finally:
            await self.release()


SERVICE_KEY = web.AppKey("service", Service)


async def startup(app: web.Application) -> None:
    await app[SERVICE_KEY].load()


async def cleanup(app: web.Application) -> None:
    await app[SERVICE_KEY].close()


async def live(_: web.Request) -> web.Response:
    return web.json_response({"status": "live"})


async def ready(request: web.Request) -> web.Response:
    service = request.app[SERVICE_KEY]
    return web.json_response(
        {
            "status": "ready" if service.ready else "loading",
            "profile_identity_sha256": service.measured,
        },
        status=200 if service.ready else 503,
    )


def create_app(service: Service | None = None) -> web.Application:
    s = service or Service()
    app = web.Application(client_max_size=s.max_request)
    app[SERVICE_KEY] = s
    app.add_routes(
        [
            web.get("/health/live", live),
            web.get("/health/ready", ready),
            web.post("/v1/timed-speech-evidence", s.evidence),
        ]
    )
    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    return app


def main() -> None:
    s = Service()
    app = create_app(s)
    web.run_app(app, host="127.0.0.1", port=int(os.environ.get("FUNASR_PORT", "8765")))


if __name__ == "__main__":
    main()
