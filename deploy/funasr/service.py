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
import importlib.resources
import json
import os
import platform
import re
import stat
import struct
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any, BinaryIO, Callable, NoReturn, TypeVar, cast

import torch
from aiohttp import web
from autocut_kernel.media.local_audio_window import (
    DecodedAudioFrameClock,
    LocalAudioWindowError,
    LocalAudioWindowSpec,
    LocalAudioWindowTracker,
)
from autocut_kernel.media.local_speech_window import (
    DecodedLocalPcmReport,
    LocalSpeechWindowPolicy,
)
from autocut_kernel.media.local_speech_window_busy import LocalSpeechWindowBusyProof
from autocut_kernel.media.local_speech_window_codec import (
    decode_local_speech_window_request,
    decode_local_speech_window_response,
    encode_local_speech_window_response,
)
from autocut_kernel.media.local_speech_window_projection import project_local_speech_window
from autocut_kernel.media.types import TimeBase
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
NORMAL_PROFILE_SCHEMA = "funasr-measured-profile-v1"
SHADOW_CALIBRATION_PROFILE_SCHEMA = "funasr-shadow-calibration-profile-v1"
SHADOW_CALIBRATION_REQUEST_SCHEMA = "shadow-calibration-funasr-raw-request-v1"
SHADOW_CALIBRATION_RESPONSE_SCHEMA = "shadow-calibration-funasr-raw-response-v1"
_InferenceResult = TypeVar("_InferenceResult")


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


def is_sha256(value: object) -> bool:
    return (
        type(value) is str
        and re.fullmatch(r"sha256:[0-9a-f]{64}", value) is not None
        and value != "sha256:" + "0" * 64
    )


def strict_json_loads(value: str | bytes) -> object:
    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        decoded: dict[str, object] = {}
        for key, item in pairs:
            if key in decoded:
                raise ValueError("duplicate JSON object key")
            decoded[key] = item
        return decoded

    return json.loads(value, object_pairs_hook=reject_duplicate_keys)


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


def _pcm_dependencies() -> tuple[Any, Any, Any]:
    # The old full-source HTTP path does not load the new decoder implicitly.
    import av
    import numpy
    import soundfile

    return av, numpy, soundfile


def decoder_identity() -> dict[str, object]:
    """Measured implementation identity, not a calibration/acceptance claim."""
    av, numpy, soundfile = _pcm_dependencies()
    libraries: dict[str, list[int]] = {}
    for name, version in cast(dict[str, object], av.library_versions).items():
        if type(version) is not tuple:
            raise LocalAudioWindowError("decoder library identity is malformed")
        values = cast(tuple[object, ...], version)
        if (type(name) is not str or len(values) != 3
                or any(type(item) is not int or item < 0 for item in values)):
            raise LocalAudioWindowError("decoder library identity is malformed")
        libraries[name] = list(cast(tuple[int, int, int], values))
    versions = {
        "pyav": av.__version__, "numpy": numpy.__version__,
        "soundfile": soundfile.__version__, "libsndfile": soundfile.__libsndfile_version__,
    }
    if not libraries or any(type(value) is not str or not value for value in versions.values()):
        raise LocalAudioWindowError("decoder dependency identity is unavailable")
    sources: list[dict[str, str]] = []
    for name in (
        "local_audio_window.py", "local_speech_window.py", "local_speech_window_codec.py",
        "local_speech_window_projection.py", "types.py",
    ):
        resource = importlib.resources.files("autocut_kernel").joinpath("media", name)
        with resource.open("rb") as stream:
            raw = stream.read(4 * 1024 * 1024 + 1)
        if not raw or len(raw) > 4 * 1024 * 1024:
            raise LocalAudioWindowError("installed audio planner source is missing or oversized")
        sources.append({"path": f"media/{name}", "sha256": sha(raw)})
    return {
        "schema_version": "local-pcm-decoder-identity-v1",
        "service_sha256": service_hash(), "versions": versions,
        "libav_versions": libraries, "planner_sources": sources,
        "pcm_encoding": "interleaved-little-endian-float32",
        "wav_subtype": "FLOAT", "resampling": "none",
    }


def decoder_identity_sha256() -> str:
    return sha(canon(decoder_identity()))


def _bounded_file_hash(stream: BinaryIO, maximum: int) -> tuple[str, int]:
    stream.seek(0)
    digest = hashlib.sha256()
    count = 0
    while block := stream.read(min(1024 * 1024, maximum - count + 1)):
        count += len(block)
        if count > maximum:
            raise LocalAudioWindowError("file exceeds explicit byte limit")
        digest.update(block)
    return "sha256:" + digest.hexdigest(), count


def _frame_pcm(frame: Any, selected: Any, spec: LocalAudioWindowSpec, numpy: Any) -> Any:
    """Convert one checked frame slice; no resampling, clipping or channel mix."""
    formats = {
        "u8": ("u", 1, 128), "s16": ("i", 2, 32768),
        "s32": ("i", 4, 2147483648), "s64": ("i", 8, 9223372036854775808),
        "flt": ("f", 4, None), "dbl": ("f", 8, None),
    }
    name = frame.format.name
    planar = frame.format.is_planar
    if type(name) is not str or type(planar) is not bool:
        raise LocalAudioWindowError("decoded sample format is malformed")
    base_name = name[:-1] if planar and name.endswith("p") else name
    if base_name not in formats or name != base_name + ("p" if planar else ""):
        raise LocalAudioWindowError("decoded sample format is unsupported")
    kind, width, divisor = formats[base_name]
    plane_bytes = 0
    for plane in frame.planes:
        size = plane.buffer_size
        if type(size) is not int or size < 0:
            raise LocalAudioWindowError("decoded audio plane size is invalid")
        plane_bytes += size
    if plane_bytes > spec.max_frame_bytes:
        raise LocalAudioWindowError("decoded audio planes exceed frame byte limit")
    raw = frame.to_ndarray()
    expected_shape = (spec.channels, frame.samples) if planar else (1, spec.channels * frame.samples)
    if (not isinstance(raw, numpy.ndarray) or raw.shape != expected_shape
            or raw.dtype.kind != kind or raw.dtype.itemsize != width
            or raw.nbytes > spec.max_frame_bytes):
        raise LocalAudioWindowError("decoded ndarray does not match the declared frame")
    interleaved = raw.T if planar else raw.reshape(frame.samples, spec.channels)
    selected_data = interleaved[selected.start_sample:selected.end_sample]
    if not numpy.isfinite(selected_data).all():
        raise LocalAudioWindowError("decoded PCM contains non-finite samples")
    with numpy.errstate(over="ignore", invalid="ignore"):
        pcm = numpy.array(selected_data, dtype="<f4", order="C", copy=True)
        if base_name == "u8":
            pcm -= 128
        if divisor is not None:
            pcm /= divisor
    if not numpy.isfinite(pcm).all():
        raise LocalAudioWindowError("FLOAT PCM conversion is non-finite")
    return pcm


def _remove_created_output(path: Path, identity: tuple[int, int]) -> None:
    try:
        current = path.lstat()
        if (current.st_dev, current.st_ino) == identity:
            path.unlink()
    except FileNotFoundError:
        pass


def _canonical_float_wav(stream: BinaryIO, spec: LocalAudioWindowSpec) -> None:
    """Remove libsndfile's PEAK wall-clock timestamp, never alter PCM samples."""
    stream.seek(0, os.SEEK_END)
    length = stream.tell()
    stream.seek(0)
    header = stream.read(12)
    if (len(header) != 12 or header[:4] != b"RIFF" or header[8:] != b"WAVE"
            or struct.unpack("<I", header[4:8])[0] != length - 8):
        raise LocalAudioWindowError("local WAV container is not a bounded RIFF WAVE")
    seen: set[bytes] = set()
    while stream.tell() < min(length, 4096):
        chunk = stream.read(8)
        if len(chunk) != 8:
            break
        kind, size = chunk[:4], struct.unpack("<I", chunk[4:])[0]
        if kind in seen or kind not in {b"fmt ", b"fact", b"PEAK", b"data"}:
            break
        seen.add(kind)
        offset = stream.tell()
        if kind == b"data":
            if (not {b"fmt ", b"fact"} <= seen or size != spec.expected_samples * spec.channels * 4
                    or offset + size != length):
                break
            stream.flush()
            return
        if size > 4096 or offset + size > min(length, 4096):
            break
        payload = stream.read(size)
        if kind == b"fmt " and payload != struct.pack(
            "<HHIIHH", 3, spec.channels, spec.sample_rate,
            spec.sample_rate * spec.channels * 4, spec.channels * 4, 32,
        ):
            break
        if kind == b"fact" and payload != struct.pack("<I", spec.expected_samples):
            break
        if kind == b"PEAK":
            if size != 8 + 8 * spec.channels or payload[:4] != struct.pack("<I", 1):
                break
            stream.seek(offset + 4)
            stream.write(b"\0" * 4)
        stream.seek(offset + size + size % 2)
    raise LocalAudioWindowError("local FLOAT WAV header or sample length is invalid")


def _deny_secondary_audio_io(_url: str, _flags: int, _options: object) -> NoReturn:
    raise LocalAudioWindowError("local decoder forbids secondary container resources")


def decode_local_pcm(
    source_path: Path, spec: LocalAudioWindowSpec, output_path: Path,
) -> DecodedLocalPcmReport:
    """Decode a verified descriptor to an exclusively created local FLOAT WAV."""
    if type(spec) is not LocalAudioWindowSpec:
        raise LocalAudioWindowError("local decoder requires an exact extraction spec")
    if not source_path.is_absolute() or not output_path.is_absolute():
        raise LocalAudioWindowError("local decoder requires private absolute paths")
    if spec.expected_samples * spec.channels * 4 > 2**32 - 4096:
        raise LocalAudioWindowError("requested PCM exceeds the RIFF WAV format bound")
    identity = decoder_identity_sha256()
    if identity != spec.decoder_identity_sha256:
        raise LocalAudioWindowError("local decoder identity differs from extraction spec")
    av, numpy, soundfile = _pcm_dependencies()
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    created: tuple[int, int] | None = None
    try:
        with os.fdopen(os.open(source_path, flags), "rb") as source:
            initial = os.fstat(source.fileno())
            if not stat.S_ISREG(initial.st_mode) or not 0 < initial.st_size <= spec.max_source_bytes:
                raise LocalAudioWindowError("source must be a bounded nonempty regular file")
            source_hash, source_size = _bounded_file_hash(source, spec.max_source_bytes)
            if source_hash != spec.source_sha256 or source_size != initial.st_size:
                raise LocalAudioWindowError("original source hash or size differs")
            source.seek(0)
            tracker = LocalAudioWindowTracker(spec)
            pcm_digest = hashlib.sha256()
            pcm_bytes = 0
            with av.open(source, mode="r", io_open=_deny_secondary_audio_io) as container:
                streams = [item for item in container.streams
                           if item.index == spec.audio_stream_index and item.type == "audio"]
                if len(streams) != 1:
                    raise LocalAudioWindowError("requested audio stream does not exist")
                output_fd = os.open(output_path, os.O_RDWR | os.O_CREAT | os.O_EXCL
                                    | getattr(os, "O_NOFOLLOW", 0), 0o600)
                with os.fdopen(output_fd, "w+b") as output:
                    output_stat = os.fstat(output.fileno())
                    created = (output_stat.st_dev, output_stat.st_ino)
                    with soundfile.SoundFile(output, mode="w", samplerate=spec.sample_rate,
                                             channels=spec.channels, format="WAV", subtype="FLOAT",
                                             endian="LITTLE", closefd=False) as wav:
                        for frame in container.decode(streams[0]):
                            base = frame.time_base
                            if base is None:
                                raise LocalAudioWindowError("decoded audio frame has no time base")
                            clock = DecodedAudioFrameClock(
                                frame.pts, TimeBase(base.numerator, base.denominator),
                                frame.sample_rate, len(frame.layout.channels), frame.samples,
                            )
                            selected = tracker.take(clock)
                            if selected is not None:
                                pcm = _frame_pcm(frame, selected, spec, numpy)
                                block = pcm.tobytes(order="C")
                                pcm_bytes += len(block)
                                if pcm_bytes > spec.max_pcm_bytes:
                                    raise LocalAudioWindowError("local PCM exceeds byte budget")
                                wav.write(pcm)
                                pcm_digest.update(block)
                                del pcm, block
                            del frame
                            if tracker.complete:
                                break
                        samples = tracker.finish()
                    if pcm_bytes != samples * spec.channels * 4:
                        raise LocalAudioWindowError("written PCM sample count differs from coverage")
                    output.flush()
                    _canonical_float_wav(output, spec)
                    # FLOAT WAV has a small format header, separate from the PCM budget.
                    wav_hash, wav_size = _bounded_file_hash(output, spec.max_pcm_bytes + 4096)
            final_hash, final_size = _bounded_file_hash(source, spec.max_source_bytes)
            final = os.fstat(source.fileno())
            if ((final_hash, final_size) != (source_hash, source_size)
                    or (initial.st_mtime_ns, initial.st_ctime_ns) != (final.st_mtime_ns, final.st_ctime_ns)):
                raise LocalAudioWindowError("original source changed during extraction")
        actual_output = output_path.lstat()
        if (actual_output.st_dev, actual_output.st_ino) != created:
            raise LocalAudioWindowError("local PCM output path changed during extraction")
        return DecodedLocalPcmReport(
            source_hash, spec.canonical_hash, identity,
            "sha256:" + pcm_digest.hexdigest(), wav_hash, wav_size,
            spec.sample_rate, spec.channels, samples, tracker.decoded_frames,
        )
    except BaseException:
        if created is not None:
            _remove_created_output(output_path, created)
        raise


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
        try:
            profile = strict_json_loads(os.environ["FUNASR_PROFILE_JSON"])
        except ValueError as error:
            raise RuntimeError("FUNASR_PROFILE_JSON contains duplicate JSON object keys") from error
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
        normal_profile_fields = {
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
        shadow_profile_fields = {
            "schema_version",
            "provider_id",
            "provider_version",
            "service_sha256",
            "funasr_version",
            "torch_version",
            "device",
            "word_timing_capability",
            "max_request_bytes",
            "native_port_identity_sha256",
            "timed_speech_policy_sha256",
            "word_gap_policy_sha256",
            "vad_merge_policy_sha256",
            "utterance_gap_milliseconds",
            "vad_merge_gap_milliseconds",
            "producers",
        }
        profile_schema = self.profile.get("schema_version")
        if profile_schema == NORMAL_PROFILE_SCHEMA:
            expected_fields = normal_profile_fields
            calibration_profile = False
        elif profile_schema == SHADOW_CALIBRATION_PROFILE_SCHEMA:
            expected_fields = shadow_profile_fields
            calibration_profile = True
        else:
            raise RuntimeError("profile schema is not supported")
        if set(self.profile) != expected_fields:
            raise RuntimeError("profile schema is not closed")
        if not calibration_profile and not is_sha256(self.profile["profile_calibration_sha256"]):
            raise RuntimeError("normal profile calibration identity is invalid")
        if calibration_profile and any(
            not is_sha256(self.profile[key])
            for key in (
                "service_sha256",
                "native_port_identity_sha256",
                "timed_speech_policy_sha256",
                "word_gap_policy_sha256",
                "vad_merge_policy_sha256",
            )
        ):
            raise RuntimeError("shadow profile identity hash is invalid")
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
        normal_identity_fields = {
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
        shadow_identity_fields = normal_identity_fields - {
            "calibration_record_sha256",
            "timing_error_bound_tick",
        }
        identity_fields = shadow_identity_fields if calibration_profile else normal_identity_fields
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
            if calibration_profile and (
                type(actual["producer_id"]) is not str
                or not actual["producer_id"]
                or any(
                    not is_sha256(actual[key])
                    for key in (
                        "generation_policy_sha256",
                        "detector_sha256",
                        "calibration_policy_sha256",
                        "model_sha256",
                        "service_sha256",
                    )
                )
            ):
                raise RuntimeError(f"measured {kind} shadow identity is invalid")
            if not calibration_profile and (
                not is_sha256(actual["calibration_record_sha256"])
                or type(actual["timing_error_bound_tick"]) is not int
                or actual["timing_error_bound_tick"] <= 0
            ):
                raise RuntimeError(f"measured {kind} calibration identity is invalid")
            identities.append(actual)
        if calibration_profile and identities[0]["producer_id"] == identities[1]["producer_id"]:
            raise RuntimeError("shadow producer IDs must be distinct")
        for key, actual in measured.items():
            if key != "producers" and actual != self.profile[key]:
                raise RuntimeError(f"measured profile identity mismatch {key}")
        if calibration_profile:
            measured_native_identity = sha(
                canon(
                    {
                        **{
                            key: value
                            for key, value in measured.items()
                            if key != "native_port_identity_sha256"
                        },
                        "producers": identities,
                    }
                )
            )
            if (
                not is_sha256(self.profile["native_port_identity_sha256"])
                or measured_native_identity != self.profile["native_port_identity_sha256"]
            ):
                raise RuntimeError("measured shadow native identity mismatch")
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

    def infer_window(
        self, source_path: Path, spec: LocalAudioWindowSpec,
    ) -> tuple[DecodedLocalPcmReport, object, object]:
        """Internal native seam only; no old-profile acceptance or HTTP opt-in."""
        with tempfile.TemporaryDirectory(prefix="funasr-local-pcm-") as directory:
            wav_path = Path(directory) / "window.wav"
            report = decode_local_pcm(source_path, spec, wav_path)
            asr, vad_output = self.infer(wav_path)
            return report, asr, vad_output

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

    def fatal(self, code: int) -> NoReturn:
        self.ready = False
        self._fatal_exit(code)
        raise RuntimeError("fatal exit returned")

    async def _run_serial_inference(
        self, operation: Callable[[], _InferenceResult], *, allow_window_error: bool,
    ) -> _InferenceResult:
        """Cancellation cannot release native ownership or extend its deadline."""
        async with self.lock:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.timeout
            task = asyncio.create_task(asyncio.to_thread(operation))
            cancelled = False
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    self.fatal(70)
                try:
                    result = await asyncio.wait_for(asyncio.shield(task), remaining)
                except asyncio.CancelledError:
                    # Repeated cancellation still drains the same worker under
                    # the same lock and original absolute timeout.
                    cancelled = True
                    continue
                except TimeoutError:
                    self.fatal(70)
                except LocalAudioWindowError:
                    if allow_window_error:
                        if cancelled:
                            raise asyncio.CancelledError from None
                        raise
                    self.fatal(71)
                except Exception:
                    self.fatal(71)
                if cancelled:
                    raise asyncio.CancelledError
                return result

    async def run_inference(self, path: Path) -> tuple[object, object]:
        return await self._run_serial_inference(
            lambda: self.infer(path), allow_window_error=False,
        )

    async def run_window_inference(
        self, source_path: Path, spec: LocalAudioWindowSpec,
    ) -> tuple[DecodedLocalPcmReport, object, object]:
        """Keep extraction and both native calls within the existing model lock."""
        return await self._run_serial_inference(
            lambda: self.infer_window(source_path, spec), allow_window_error=True,
        )

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

    def validate_shadow_calibration_manifest_identity(self, manifest: dict[str, object]) -> None:
        if self.measured_profile is None:
            raise web.HTTPServiceUnavailable()
        if self.measured_profile["schema_version"] != SHADOW_CALIBRATION_PROFILE_SCHEMA:
            raise web.HTTPConflict(text="shadow calibration profile is unavailable")
        limits = cast(dict[str, int], manifest["source_byte_limits"])
        if (
            limits["service_max_request_bytes"] != self.max_request
            or limits["effective_max_source_bytes"]
            != min(limits["kernel_max_source_bytes"], limits["service_max_request_bytes"])
        ):
            raise web.HTTPConflict(text="measured source-byte policy drift")
        if manifest["expected_producers"] != self.identities:
            raise web.HTTPConflict(text="measured producer identity drift")
        expected_policies = {
            "timed_speech_policy_sha256": self.measured_profile["timed_speech_policy_sha256"],
            "word_gap_policy_sha256": self.measured_profile["word_gap_policy_sha256"],
            "vad_merge_policy_sha256": self.measured_profile["vad_merge_policy_sha256"],
            "native_profile_identity_sha256": self.measured_profile[
                "native_port_identity_sha256"
            ],
        }
        if any(manifest[key] != value for key, value in expected_policies.items()):
            raise web.HTTPConflict(text="measured shadow calibration identity drift")
        if manifest["timing_policy"] != {
            "utterance_gap_milliseconds": self.measured_profile["utterance_gap_milliseconds"],
            "vad_merge_gap_milliseconds": self.measured_profile["vad_merge_gap_milliseconds"],
        } or manifest["transcript_capability"] != {
            "profile": SENSEVOICE_WORD_GUARD_PROFILE,
            "segment": "complete",
            "segment_semantics": "utterance_gap_protected_range",
            "sentence": "not_applicable",
            "word": "complete",
            "word_timing": "required",
        }:
            raise web.HTTPConflict(text="measured shadow calibration policy drift")

    @staticmethod
    def validate_shadow_calibration_manifest_schema(manifest: object) -> dict[str, object]:
        fields = {
            "schema_version",
            "source",
            "source_byte_limits",
            "container",
            "audio_clock",
            "requested_range",
            "expected_producers",
            "timed_speech_policy_sha256",
            "word_gap_policy_sha256",
            "vad_merge_policy_sha256",
            "native_profile_identity_sha256",
            "response_limits",
            "timing_policy",
            "transcript_capability",
        }
        if type(manifest) is not dict or set(manifest) != fields:
            raise web.HTTPBadRequest(text="shadow calibration manifest schema is not closed")
        value = cast(dict[str, object], manifest)
        if value["schema_version"] != SHADOW_CALIBRATION_REQUEST_SCHEMA:
            raise web.HTTPBadRequest(text="shadow calibration manifest schema is invalid")
        source = value["source"]
        source_byte_limits = value["source_byte_limits"]
        container = value["container"]
        clock = value["audio_clock"]
        requested = value["requested_range"]
        response_limits = value["response_limits"]
        timing_policy = value["timing_policy"]
        transcript_capability = value["transcript_capability"]
        if (
            type(source) is not dict
            or set(source) != {"source_id", "source_sha256"}
            or type(source["source_id"]) is not str
            or not source["source_id"]
            or not is_sha256(source["source_sha256"])
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
            or type(clock["clock_id"]) is not str
            or not clock["clock_id"]
            or type(requested) is not dict
            or set(requested) != {"in_tick", "out_tick"}
            or type(response_limits) is not dict
            or set(response_limits) != {"max_response_bytes"}
            or type(timing_policy) is not dict
            or set(timing_policy)
            != {"utterance_gap_milliseconds", "vad_merge_gap_milliseconds"}
            or type(transcript_capability) is not dict
            or set(transcript_capability)
            != {
                "profile",
                "segment",
                "segment_semantics",
                "sentence",
                "word",
                "word_timing",
            }
            or type(value["expected_producers"]) is not list
            or len(value["expected_producers"]) != 2
            or any(
                not is_sha256(value[key])
                for key in (
                    "timed_speech_policy_sha256",
                    "word_gap_policy_sha256",
                    "vad_merge_policy_sha256",
                    "native_profile_identity_sha256",
                )
            )
        ):
            raise web.HTTPBadRequest(text="shadow calibration manifest member schema is not closed")
        time_base = clock["time_base"]
        integer_values = (
            clock["origin_tick"],
            clock["duration_tick"],
            requested["in_tick"],
            requested["out_tick"],
            response_limits["max_response_bytes"],
            *cast(dict[str, object], source_byte_limits).values(),
            *cast(dict[str, object], timing_policy).values(),
        )
        if (
            type(time_base) is not dict
            or set(time_base) != {"numerator", "denominator"}
            or any(type(item) is not int for item in (*integer_values, *time_base.values()))
            or clock["duration_tick"] <= 0
            or requested["out_tick"] <= requested["in_tick"]
            or response_limits["max_response_bytes"] <= 0
            or any(item <= 0 for item in cast(dict[str, int], source_byte_limits).values())
            or any(item < 0 for item in cast(dict[str, int], timing_policy).values())
            or any(item <= 0 for item in cast(dict[str, int], time_base).values())
        ):
            raise web.HTTPBadRequest(text="shadow calibration manifest clock/bounds are invalid")
        return value

    @staticmethod
    def validate_shadow_native_outputs(asr: object, vad_output: object) -> None:
        if type(asr) is not list or len(asr) != 1 or type(asr[0]) is not dict:
            raise web.HTTPUnprocessableEntity(text="shadow calibration ASR output is invalid")
        asr_item = cast(dict[str, object], asr[0])
        if set(asr_item) != {"text", "words", "timestamp"}:
            raise web.HTTPUnprocessableEntity(text="shadow calibration ASR output is not closed")
        text = asr_item["text"]
        words = asr_item["words"]
        timestamps = asr_item["timestamp"]
        if (
            type(text) is not str
            or type(words) is not list
            or type(timestamps) is not list
            or not words
            or len(words) != len(timestamps)
        ):
            raise web.HTTPUnprocessableEntity(text="shadow calibration ASR word timestamps are invalid")
        prior_end = 0
        for word, pair in zip(words, timestamps, strict=True):
            if (
                type(word) is not str
                or not word.strip()
                or type(pair) is not list
                or len(pair) != 2
                or type(pair[0]) is not int
                or type(pair[1]) is not int
                or pair[0] < 0
                or pair[0] >= pair[1]
                or prior_end > pair[0]
            ):
                raise web.HTTPUnprocessableEntity(
                    text="shadow calibration ASR word timestamps are invalid"
                )
            prior_end = pair[1]
        if type(vad_output) is not list or len(vad_output) != 1 or type(vad_output[0]) is not dict:
            raise web.HTTPUnprocessableEntity(text="shadow calibration VAD output is invalid")
        vad_item = cast(dict[str, object], vad_output[0])
        if set(vad_item) != {"value"} or type(vad_item["value"]) is not list or not vad_item["value"]:
            raise web.HTTPUnprocessableEntity(text="shadow calibration VAD output is invalid")
        prior_start = -1
        for pair in cast(list[object], vad_item["value"]):
            if (
                type(pair) is not list
                or len(pair) != 2
                or type(pair[0]) is not int
                or type(pair[1]) is not int
                or pair[0] < 0
                or pair[0] >= pair[1]
                or prior_start > pair[0]
            ):
                raise web.HTTPUnprocessableEntity(text="shadow calibration VAD output is invalid")
            prior_start = pair[0]

    async def shadow_calibration_raw(self, req: web.Request) -> web.Response:
        if not self.ready:
            raise web.HTTPServiceUnavailable()
        supplied = req.headers.get("Authorization", "")
        if not hmac.compare_digest(supplied, f"Bearer {self.shared_token}"):
            raise web.HTTPUnauthorized(text="unauthorized")
        if (
            self.measured_profile is None
            or self.measured_profile["schema_version"] != SHADOW_CALIBRATION_PROFILE_SCHEMA
        ):
            raise web.HTTPConflict(text="shadow calibration profile is unavailable")
        try:
            decoded = strict_json_loads(
                base64.b64decode(req.headers["X-Shadow-Calibration-Manifest"], validate=True)
            )
            m = self.validate_shadow_calibration_manifest_schema(decoded)
        except Exception as error:
            if isinstance(error, web.HTTPException):
                raise
            raise web.HTTPBadRequest(text="bad shadow calibration manifest") from error
        request_identity = sha(canon(m))
        if request_identity != req.headers.get("X-Shadow-Calibration-Request-SHA256"):
            raise web.HTTPBadRequest(text="identity")
        self.validate_shadow_calibration_manifest_identity(m)
        rr = cast(dict[str, int], m["requested_range"])
        clock = cast(dict[str, object], m["audio_clock"])
        if rr != {
            "in_tick": clock["origin_tick"],
            "out_tick": clock["origin_tick"] + clock["duration_tick"],
        }:
            raise web.HTTPBadRequest(text="full source only")
        limits = cast(dict[str, int], m["source_byte_limits"])
        await self.admit()
        try:
            with tempfile.TemporaryDirectory(prefix="funasr-shadow-calibration-") as directory:
                path = Path(directory) / "source.mp4"
                digest = hashlib.sha256()
                size = 0
                with path.open("xb") as output:
                    async for chunk in req.content.iter_chunked(1 << 20):
                        size += len(chunk)
                        if size > limits["effective_max_source_bytes"]:
                            raise web.HTTPRequestEntityTooLarge(
                                max_size=limits["effective_max_source_bytes"], actual_size=size
                            )
                        digest.update(chunk)
                        output.write(chunk)
                source = cast(dict[str, str], m["source"])
                if "sha256:" + digest.hexdigest() != source["source_sha256"]:
                    raise web.HTTPBadRequest(text="source hash")
                asr, vad_output = await self.run_inference(path)
            self.validate_shadow_native_outputs(asr, vad_output)
            response = {
                "schema_version": SHADOW_CALIBRATION_RESPONSE_SCHEMA,
                "request_identity_sha256": request_identity,
                "source": m["source"],
                "audio_clock": m["audio_clock"],
                "requested_range": m["requested_range"],
                "timed_speech_policy_sha256": m["timed_speech_policy_sha256"],
                "word_gap_policy_sha256": m["word_gap_policy_sha256"],
                "vad_merge_policy_sha256": m["vad_merge_policy_sha256"],
                "native_profile_identity_sha256": m["native_profile_identity_sha256"],
                "producer_identities": self.identities,
                "asr_native_output": asr,
                "vad_native_output": vad_output,
            }
            raw = canon(response)
            response_limits = cast(dict[str, int], m["response_limits"])
            if len(raw) > min(self.max_response, response_limits["max_response_bytes"]):
                raise web.HTTPInternalServerError(text="response bound")
            return web.Response(body=raw, content_type="application/json")
        finally:
            await self.release()

    async def window_evidence(self, req: web.Request) -> web.Response:
        """Raw local measurement only; binding_sha256 is opaque correlation."""
        if not self.ready:
            raise web.HTTPServiceUnavailable()
        if not hmac.compare_digest(req.headers.get("Authorization", "").encode("utf-8"),
                                   f"Bearer {self.shared_token}".encode("utf-8")):
            raise web.HTTPUnauthorized(text="unauthorized")
        if (self.measured_profile is None
                or self.measured_profile["schema_version"] != NORMAL_PROFILE_SCHEMA):
            raise web.HTTPConflict(text="normal measured profile is unavailable")
        try:
            encoded = req.headers["X-Local-Speech-Window-Manifest"]
            raw_manifest = base64.b64decode(encoded, validate=True)
            request = decode_local_speech_window_request(strict_json_loads(raw_manifest))
            if (base64.b64encode(raw_manifest).decode("ascii") != encoded
                    or canon(request.to_mapping()) != raw_manifest
                    or request.canonical_hash != req.headers.get("X-Local-Speech-Window-SHA256")):
                raise ValueError("noncanonical request identity")
        except (ValueError, TypeError, KeyError, RecursionError) as error:
            raise web.HTTPBadRequest(text="invalid local speech window manifest") from error
        roles = {item["producer_kind"]: item for item in self.identities}
        if len(self.identities) != 2 or set(roles) != {"asr", "vad"}:
            raise web.HTTPConflict(text="measured speech producers are unavailable")
        expected_policy = LocalSpeechWindowPolicy(
            service_profile_sha256=sha(canon(self.measured_profile)),
            asr_producer_id=cast(str, roles["asr"]["producer_id"]),
            asr_generation_policy_sha256=cast(str, roles["asr"]["generation_policy_sha256"]),
            vad_producer_id=cast(str, roles["vad"]["producer_id"]),
            vad_generation_policy_sha256=cast(str, roles["vad"]["generation_policy_sha256"]),
            utterance_gap_milliseconds=cast(int, self.measured_profile["utterance_gap_milliseconds"]),
            vad_merge_gap_milliseconds=cast(int, self.measured_profile["vad_merge_gap_milliseconds"]),
        )
        if request.policy != expected_policy:
            raise web.HTTPConflict(text="measured local speech policy drift")
        if request.extraction.decoder_identity_sha256 != await asyncio.to_thread(decoder_identity_sha256):
            raise web.HTTPConflict(text="local decoder identity drift")
        if (request.extraction.max_source_bytes > self.max_request
                or request.max_response_bytes > self.max_response):
            raise web.HTTPBadRequest(text="local speech request exceeds service bounds")
        if req.content_length is not None and req.content_length > request.extraction.max_source_bytes:
            raise web.HTTPRequestEntityTooLarge(
                max_size=request.extraction.max_source_bytes, actual_size=req.content_length,
            )
        try:
            await self.admit()
        except web.HTTPServiceUnavailable as error:
            # Only admission refusal proves that no upload/native work began.
            # Later inference errors must never become a not_started report.
            proof = LocalSpeechWindowBusyProof(
                request.canonical_hash, request.binding_sha256,
                request.policy.service_profile_sha256,
            )
            raw_proof = proof.to_bytes()
            retry_after = error.headers.get("Retry-After")
            headers = {} if retry_after is None else {"Retry-After": retry_after}
            if len(raw_proof) > min(self.max_response, request.max_response_bytes):
                return web.Response(status=503, body=b"", headers=headers)
            return web.Response(
                status=503, body=raw_proof, content_type="application/json", headers=headers,
            )
        try:
            with tempfile.TemporaryDirectory(prefix="funasr-window-request-") as directory:
                path = Path(directory) / "source.bin"
                digest, size = hashlib.sha256(), 0
                with path.open("xb") as output:
                    async for chunk in req.content.iter_chunked(1 << 20):
                        size += len(chunk)
                        if size > request.extraction.max_source_bytes:
                            raise web.HTTPRequestEntityTooLarge(
                                max_size=request.extraction.max_source_bytes, actual_size=size,
                            )
                        digest.update(chunk)
                        output.write(chunk)
                if not size or "sha256:" + digest.hexdigest() != request.extraction.source_sha256:
                    raise web.HTTPBadRequest(text="source hash")
                try:
                    report, asr, vad_output = await self.run_window_inference(path, request.extraction)
                    raw = encode_local_speech_window_response(request, report, asr, vad_output)
                    project_local_speech_window(decode_local_speech_window_response(raw, request))
                except ValueError as error:
                    raise web.HTTPUnprocessableEntity(text="invalid local speech measurement") from error
            if len(raw) > request.max_response_bytes:
                raise web.HTTPInternalServerError(text="response bound")
            return web.Response(body=raw, content_type="application/json")
        finally:
            # Do not leak queue ownership if a second cancellation arrives
            # while releasing it. There is no native work in this release.
            release = asyncio.create_task(self.release())
            cancelled = False
            while not release.done():
                try:
                    await asyncio.shield(release)
                except asyncio.CancelledError:
                    cancelled = True
            release.result()
            if cancelled:
                raise asyncio.CancelledError

    async def evidence(self, req: web.Request) -> web.Response:
        if not self.ready:
            raise web.HTTPServiceUnavailable()
        supplied = req.headers.get("Authorization", "")
        if not hmac.compare_digest(supplied, f"Bearer {self.shared_token}"):
            raise web.HTTPUnauthorized(text="unauthorized")
        if (
            self.measured_profile is None
            or self.measured_profile["schema_version"] != NORMAL_PROFILE_SCHEMA
        ):
            raise web.HTTPConflict(text="timed speech evidence profile is unavailable")
        try:
            decoded = strict_json_loads(
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
            web.post("/v1/shadow-calibration-funasr-raw", s.shadow_calibration_raw),
            web.post("/v2/timed-speech-window", s.window_evidence),
        ]
    )
    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    return app


def main() -> None:
    host = os.environ.get("FUNASR_BIND_HOST", "127.0.0.1")
    if host not in {"127.0.0.1", "0.0.0.0"}:
        raise RuntimeError("FUNASR_BIND_HOST must be 127.0.0.1 or 0.0.0.0")
    s = Service()
    app = create_app(s)
    web.run_app(app, host=host, port=int(os.environ.get("FUNASR_PORT", "8765")))


if __name__ == "__main__":
    main()
